import logging
import socket
import time

import click
from PIL import Image, ImageOps, UnidentifiedImageError


SOCKET_BUFFER_SIZE = 1024

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# This was discovered by capturing network traffic from the futurePRNT software.
# We can discover printers on the local network by broadcasting a certain bytestring on port 22222.
# Printers will respond back to us, with a similar bytestring, plus some additional data about the
# printer, such as model name, mac address, how it's configured (DHCP/STATIC), et.c.
#
# This is apparently called SDP, or "Star Discovery Protocol", and it's briefly mentioned in this
# document: http://www.starasia.com/Download/Others/UsersManual_IFBD_HE0708BE07_EN.pdf
def discover_printers():
    msg = b'STR_BCAST' + bytes([0x00] * 7) + b'RQ1.0.0' + bytes([0x00, 0x00, 0x1c, 0x64, 0x31])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    # NOTE: Ideally we should send to the broadcast address of each connected network instead.
    sock.sendto(msg, ("255.255.255.255", 22222))

    try:
        _data, (sender, _port) = sock.recvfrom(SOCKET_BUFFER_SIZE)
        return sender

    except TimeoutError:
        return None

    finally:
        sock.close()


# This was discovered by capturing network traffic from the futurePRNT software.
# NOTE: The status bytestring received from the printer is duplicated for some reason.
# The protocol is briefly mentioned here: http://www.starasia.com/Download/Others/UsersManual_IFBD_HE0708BE07_EN.pdf
def get_printer_status(host):
    sock = socket.create_connection((host, 9101), timeout=1)
    sock.settimeout(1)
    sock.sendall(b'0' + bytes([0x00] * 50)) # '2' will also work
    response = sock.recv(SOCKET_BUFFER_SIZE)

    # The first byte contains the status length
    h1 = response[0]
    status_length = ((h1 >> 2) & 0b1000) + ((h1 >> 1) & 0b111)

    # The docs are very ambiguous about the second byte. First it claims to only use 4 bits, just like
    # the first byte, but then there's a table that counts up to 31, which would require 5 bits.
    #
    # Appendix-2 mentions something about bit 7 being set to 1 instead of 0.
    #
    # The docs does state that this byte can be safely ignored however, so I'll do just that.
    h2 = response[1]
    _status_version = ((h2 >> 2) & 0b11000) + ((h2 >> 1) & 0b111) # NOTE: Use 5 bits, instead of 4

    etb = response[5]
    etb_counter = ((etb >> 2) & 0b11000) + ((etb >> 1) & 0b111)

    return response[2:status_length + 2]


@click.command(context_settings={'show_default': True})
@click.option('--autodiscover', is_flag=True, default=False, help='Try to autodiscover the printer on the network via broadcasting')
@click.option('--cut/--no-cut', default=True, help='Whether or not to cut receipt after printing')
@click.option('-d', '--density', default=3, type=click.IntRange(0, 6), help='0 = Highest density, 6 = Lowest density')
@click.option('--dither', default='NONE', type=click.Choice(['NONE', 'FLOYDSTEINBERG'], case_sensitive=False))
@click.option('--margin-top', default=0)
@click.option('--margin-bottom', default=9)
@click.option('--resize-width', type=int, help='Resizes input image to the given width while preserving aspect ratio')
@click.option('-s', '--speed', default=2, type=click.IntRange(0, 2), help='0 = Fastest, 2 = Slowest')
@click.option('--log-level', type=click.Choice(['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'], case_sensitive=False, default='WARNING'))
@click.argument('printer', required=False)
@click.argument('input', type=click.File('rb'))
def print_image(printer, input, autodiscover, cut, density, dither, margin_top, margin_bottom, resize_width, speed):
    '''
    This is a small utility for sending raster images to Star Micronics TSP100 / TSP143 receipt printers.

    The program expects bilevel (black and white) images, at most 576 pixels wide. Wider images will be cropped.
    '''

    try:
        image = Image.open(input)
    except UnidentifiedImageError as e:
        raise click.ClickException(f'Could not open {input.name} as an image, unknown format') from e

    histogram = image.histogram()
    if any(histogram[1:-1]):
        log.warning('More than 2 levels (black/white), data will be lost via thresholding')

    image = image.convert("1", dither=getattr(Image.Dither, dither.upper()))
    image = ImageOps.invert(image)

    if resize_width:
        resize_height = resize_width * image.height // image.width
        log.info('Resizing image to width / height: %d / %d', resize_width, resize_height)
        image = image.resize((resize_width, resize_height))

    # Crop the image if needed, try to be minimally destructive by only cropping "empty" image data
    if image.width > 576:
        log.warning('Image is wider than 576 pixels, cropping will occur')

        (left, _upper, right, _lower) = image.getbbox()
        cropped_width = right - left

        if cropped_width > 576:
            log.warning('Cropping image content, data will be lost')
            image = image.crop((0, 0, 576, image.height))
        else:
            if right > 576:
                log.info('Cropping empty image content only, image will be shifted to the left')
                image = image.crop((left, 0, right, image.height))
            else:
                log.info('Cropping empty image content only, no data loss')
                image = image.crop((0, 0, right, image.height))

    raw_bytes = image.tobytes()

    if not any(raw_bytes):
        log.critical('Image is blank, refusing to print')
        raise click.ClickException('Nothing to print')

    if not printer:
        host = discover_printers()
        if not host:
            raise click.ClickException('Could not autodetect printer, and no printer was given')
    else:
        host = printer

    # Connect to the printer
    try:
        connection = socket.create_connection((host, 9100), timeout=1)
    except TimeoutError as e:
        raise click.ClickException(f'Timed out while trying to connect to {host}, make sure that the printer is online') from e
    except OSError as e:
        raise click.ClickException(f'Could not connect to {host}: {e}') from e

    # Read printer status, NSB
    status = connection.recv(SOCKET_BUFFER_SIZE)
    log.debug('ASB: %s', repr([hex(x) for x in status]))

    if any(status[2:]):
        if status[2] & 1 << 3:
            click.echo('Printer status is offline')

        if status[2] & 1 << 5:
            click.echo('Printer cover is open')

        if status[3] & 1 << 3:
            click.echo('Auto cutter error')

        if status[3] & 1 << 5:
            click.echo('Unrecoverable printer error')

        if status[3] & 1 << 6:
            click.echo('High temperature error')

        if status[5] & 1 << 3:
            click.echo('Printer is out of paper')

        raise click.ClickException('Please check printer')

    # Initialize printer
    connection.sendall(bytes([0x1b, 0x1e, 0x72, speed])) # Speed
    connection.sendall(bytes([0x1b, 0x1e, 0x64, density])) # Set print density
    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'R')])) # Init raster mode, this will clear the input buffer if theres any stray data
    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'A')])) # Enter raster mode

    if not cut:
        connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'E'), 1, 0x00])) # End of Transmission cut behaviour

    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'm'), ord(b'l'), 0, 0x00])) # No left margin
    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'm'), ord(b'r'), 0, 0x00])) # No right margin
    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'P'), ord(b'0'), 0x00])) # Set raster length to continuous
    BYTES_PER_LINE = 72

    # Send top margin, 8 dots per millimeter
    for _line in range(8 * margin_top):
        connection.sendall(bytes([ord(b'b'), BYTES_PER_LINE, 0x00]))
        connection.sendall(bytes([0x00] * BYTES_PER_LINE))

    # Send image
    index = 0
    for _line in range(image.height):
        connection.sendall(bytes([ord(b'b'), BYTES_PER_LINE, 0x00]))

        for _row in range(BYTES_PER_LINE):
            connection.sendall(bytes([raw_bytes[index]]))
            index += 1

    # Send bottom margin, 8 dots per millimeter
    for _line in range(8 * margin_bottom):
        connection.sendall(bytes([ord(b'b'), BYTES_PER_LINE, 0x00]))
        connection.sendall(bytes([0x00] * BYTES_PER_LINE))

    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'B')])) # Quit raster mode
    connection.close()

    status = get_printer_status(host)
    log.debug('Print verification ASB: %s', repr([hex(x) for x in status]))

    if any(status):
        raise click.ClickException('Print might have failed, check printer')

    connection.close()
    time.sleep(1) # Wait for the cutter to do its thing before exiting
