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
def discover_printers():
    msg = b'STR_BCAST' + bytes([0x00] * 7) + b'RQ1.0.0' + bytes([0x00, 0x00, 0x1c, 0x64, 0x31])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    # NOTE: Ideally we should send to the broadcast address of each connected network instead.
    sock.sendto(msg, ("255.255.255.255", 22222))

    try:
        _data, sender = sock.recvfrom(SOCKET_BUFFER_SIZE)
        return sender

    except TimeoutError:
        return None

    finally:
        sock.close()


# This was discovered by capturing network traffic from the futurePRNT software.
# NOTE: The status bytestring received from the printer is duplicated for some reason.
def get_printer_status(host):
    sock = socket.create_connection((host, 9101), timeout=1)
    sock.settimeout(1)
    sock.sendall(b'0' + bytes([0x00] * 50))
    response = sock.recv(1024)
    return response


@click.command(context_settings={'show_default': True})
@click.option('--autodiscover', is_flag=True, default=False, help='Try to autodiscover the printer on the network via broadcasting')
@click.option('--cut/--no-cut', default=True, help='Whether or not to cut receipt after printing')
@click.option('-d', '--density', default=3, type=click.IntRange(0, 6), help='0 = Highest density, 6 = Lowest density')
@click.option('--dither', default='NONE', type=click.Choice(['NONE', 'FLOYDSTEINBERG'], case_sensitive=False))
@click.option('-h', '--host', help='Hostname or IP-address of the printer')
@click.option('--margin-top', default=0)
@click.option('--margin-bottom', default=9)
@click.option('--resize-width', type=int, help='Resizes input image to the given width while preserving aspect ratio')
@click.option('-s', '--speed', default=2, type=click.IntRange(0, 2), help='0 = Fastest, 2 = Slowest')
@click.argument('input', type=click.File('rb'))
def print_image(input, autodiscover, cut, density, dither, host, margin_top, margin_bottom, resize_width, speed):
    '''
    This is a small utility for sending raster images to Star Micronics TSP100 / TSP143 receipt printers.

    The program expects bilevel (black and white) images, at most 576 pixels wide. Wider images will be cropped.
    '''

    if host:
        pass

    elif autodiscover:
        host = discover_printers()

        if not host:
            raise click.ClickException('Could not autodiscover any printer')

    else:
        raise click.UsageError('You need to specify either --autodiscover or -h/--host <printer-hostname-or-ip>')

    try:
        image = Image.open(input)
    except UnidentifiedImageError:
        raise click.ClickException(f'Could not open {input.name} as an image, unknown format')

    histogram = image.histogram()
    if any(histogram[1:-1]):
        log.warning('More than 2 levels (black/white), data will be lost via thresholding')

    image = image.convert("1", dither=getattr(Image.Dither, dither.upper()))
    image = ImageOps.invert(image)

    if resize_width:
        resize_height = resize_width * image.height // image.width
        log.info(f'Resizing image to width / height: {resize_width} / {resize_height}')
        image = image.resize((resize_width, resize_height))

    # Crop the image if needed, try to be minimally destructive by only cropping "empty" image data
    if image.width > 576:
        log.warning('Image is wider than 576 pixels')

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

    # Connect to the printer
    try:
        connection = socket.create_connection((host, 9100), timeout=1)
    except TimeoutError:
        raise click.ClickException(f'Timed out while trying to connect to {host}, make sure that the printer is online')
    except OSError as e:
        raise click.ClickException(f'Could not connect to {host}: {e}')

    # Read printer status
    data = connection.recv(SOCKET_BUFFER_SIZE)
    log.debug(f'ASB: {repr([hex(x) for x in data])}')

    if any(data[2:]):
        if data[2] & 1 << 3:
            click.echo('Printer status is offline')

        if data[2] & 1 << 5:
            click.echo('Printer cover is open')

        if data[3] & 1 << 3:
            click.echo('Auto cutter error')

        if data[3] & 1 << 5:
            click.echo('Unrecoverable printer error')

        if data[3] & 1 << 6:
            click.echo('High temperature error')

        if data[5] & 1 << 3:
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

    # Try to reconnect to the printer to check status
    for _ in range(10):
        try:
            connection = socket.create_connection((host, 9100), timeout=1)
        except (TimeoutError, OSError):
            time.sleep(1)
            continue

        data = connection.recv(SOCKET_BUFFER_SIZE)
        log.debug(f'Print verification ASB: {repr([hex(x) for x in data])}')

        if any(data[2:]):
            raise click.ClickException('Print might have failed, check printer')

        connection.close()
        time.sleep(1) # Wait for the cutter to do its thing before exiting
        break

    else:
        raise click.ClickException('Could not verify print, check printer')
