import enum
import logging
import math
import socket
import time

import click
from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError


SOCKET_BUFFER_SIZE = 1024

log = logging.getLogger(__name__)


class PrinterStatus(enum.Flag):
    COVER_OPEN    = 0b00100000
    OFFLINE       = 0b00001000
    COMPULSION_SW = 0b00000100
    ETB_EXECUTED  = 0b00000010

    def __str__(self):
        if self is PrinterStatus.COVER_OPEN:
            return 'Cover is open'

        if self is PrinterStatus.OFFLINE:
            return 'Printer status is offline'

        if self is PrinterStatus.COMPULSION_SW:
            return 'Switch is being pressed'

        if self is PrinterStatus.ETB_EXECUTED:
            return 'ETB was executed'

class PrinterError(enum.Flag):
    HIGH_TEMPERATURE    = 0b01000000
    UNRECOVERABLE_ERROR = 0b00100000
    CUTTER_ERROR        = 0b00001000

    def __str__(self):
        if self is PrinterError.HIGH_TEMPERATURE:
            return 'High temperature error'

        if self is PrinterError.UNRECOVERABLE_ERROR:
            return 'Unrecoverable error'

        if self is PrinterError.CUTTER_ERROR:
            return 'Cutter error'

class PaperError(enum.Flag):
    NO_PAPER = 0b00001000

    def __str__(self):
        return 'Out of paper'


class ErrorList(list):
    def __str__(self):
        return ', '.join(str(s) for s in self)


class Status:
    def __init__(self):
        self.etb_executed = False
        self.etb_counter = 0
        self.errors = ErrorList()

    def __str__(self):
        return f'ETB Executed: {self.etb_executed}, ETB Counter: {self.etb_counter}, ErrorList: {self.errors}'

    def parse(self, status):
        self.errors = ErrorList()

        b1 = status[0]
        _status_length = ((b1 >> 2) & 0b1000) + ((b1 >> 1) & 0b111)

        # The docs are ambigious about this one.
        # We're reading 5 bits instead of 4, to count up to 31.
        # Docs state that this can be safely ignored, so we'll ignore it.
        # The 7th bit is set when getting status over LAN.
        b2 = status[1]
        _status_version = ((b2 >> 2) & 0b11000) + ((b2 >> 1) & 0b111)


        b3 = status[2]
        if b3 & PrinterStatus.COVER_OPEN.value:
            self.errors.append(PrinterStatus.COVER_OPEN)

        if b3 & PrinterStatus.OFFLINE.value:
            self.errors.append(PrinterStatus.OFFLINE)

        if b3 & PrinterStatus.COMPULSION_SW.value:
            self.errors.append(PrinterStatus.COMPULSION_SW)

        self.etb_executed = False
        if b3 & PrinterStatus.ETB_EXECUTED.value:
            self.etb_executed = True


        b4 = status[3]
        if b4 & PrinterError.HIGH_TEMPERATURE.value:
            self.errors.append(PrinterError.HIGH_TEMPERATURE)

        if b4 & PrinterError.UNRECOVERABLE_ERROR.value:
            self.errors.append(PrinterError.UNRECOVERABLE_ERROR)

        if b4 & PrinterError.CUTTER_ERROR.value:
            self.errors.append(PrinterError.CUTTER_ERROR)


        b6 = status[5]
        if b6 & PaperError.NO_PAPER.value:
            self.errors.append(PaperError.NO_PAPER)


        b8 = status[7]
        self.etb_counter = ((b8 >> 2) & 0b11000) + ((b8 >> 1) & 0b111)


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

        # Just grab the first available printer
        return sender

    except TimeoutError:
        return None

    finally:
        sock.close()


# This was discovered by capturing network traffic from the futurePRNT software.
# NOTE: The status bytestring received from my printer is duplicated for some reason.
# The protocol is briefly mentioned here: http://www.starasia.com/Download/Others/UsersManual_IFBD_HE0708BE07_EN.pdf
def get_printer_status(host):
    sock = socket.create_connection((host, 9101), timeout=1)
    sock.settimeout(1)
    sock.sendall(b'0' + bytes([0x00] * 50)) # '2' will also work
    response = sock.recv(SOCKET_BUFFER_SIZE)

    status = Status()
    status.parse(response)

    return status


def process_image(image_file, dither, resize_width, sharpness):
    try:
        image = Image.open(image_file)
    except UnidentifiedImageError as e:
        raise click.ClickException(f'Could not open {image_file.name} as an image, unknown format') from e

    histogram = image.histogram()
    if any(histogram[1:-1]):
        log.warning('More than 2 levels (black/white), data will be lost via thresholding/dithering')


    if resize_width:
        resize_height = resize_width * image.height // image.width
        log.info('Resizing image to width / height: %d / %d', resize_width, resize_height)
        image = image.resize((resize_width, resize_height))

    image = ImageEnhance.Sharpness(image)
    image = image.enhance(sharpness)

    image = image.convert("1", dither=getattr(Image.Dither, dither.upper()))
    image = ImageOps.invert(image)

    # Crop the image if needed, try to be minimally destructive by only cropping "empty" image data
    if image.width > 576:
        log.info('Image is wider than 576 pixels, cropping will occur')

        (left, _upper, right, _lower) = image.getbbox() or (0, 0, image.width, image.height)
        cropped_width = right - left

        if cropped_width > 576:
            log.warning('Cropping image content, data will be lost')
            image = image.crop((0, 0, 576, image.height))
        else:
            if right > 576:
                log.warning('Cropping empty image content only, image will be shifted to the left')
                image = image.crop((left, 0, right, image.height))
            else:
                log.info('Cropping empty image content only, no data loss')
                image = image.crop((0, 0, right, image.height))

    return image


@click.command(context_settings={'show_default': True})
@click.option('--cut/--no-cut', default=True, help='Whether or not to cut receipt after printing')
@click.option('-d', '--density', default=3, type=click.IntRange(0, 6), help='0 = Highest density, 6 = Lowest density')
@click.option('--dither', default='NONE', type=click.Choice(['NONE', 'FLOYDSTEINBERG'], case_sensitive=False))
@click.option('--log-level', type=click.Choice(['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'], case_sensitive=False), default='WARNING')
@click.option('--margin-top', default=0)
@click.option('--margin-bottom', default=9)
@click.option('--print-timeout', default=10, help='Maximum time in seconds to wait for a print to finish')
@click.option('--resize-width', type=int, help='Resizes input image to the given width while preserving aspect ratio')
@click.option('-s', '--speed', default=2, type=click.IntRange(0, 2), help='0 = Fastest, 2 = Slowest')
@click.option('--sharpness', default=0.0, help='Sharpen the image, higher numbers gives a sharper image')
@click.argument('printer', nargs=-1)
@click.argument('image_file', type=click.File('rb'))
def print_image(printer, image_file, cut, density, dither, log_level, margin_top, margin_bottom, print_timeout, resize_width, speed, sharpness):
    '''
    This is a small utility for sending raster images to Star Micronics TSP100 / TSP143 receipt printers.

    The program expects bilevel (black and white) images, at most 576 pixels wide. Wider images will be cropped.
    '''

    logging.basicConfig(level=getattr(logging, log_level))
    logging.getLogger('PIL').setLevel(logging.WARNING)

    if len(printer) > 1:
        raise click.UsageError('Multiple printers specified, please specify a single printer')

    image = process_image(image_file, dither, resize_width, sharpness)
    raw_bytes = image.tobytes()

    if not any(raw_bytes):
        log.critical('Image is blank, refusing to print')
        raise click.ClickException('Image contains no printable data')

    if not printer:
        host = discover_printers()

        if not host:
            raise click.ClickException('Could not autodetect printer, and no printer was given')
    else:
        host = printer[0]

    # Connect to the printer
    try:
        connection = socket.create_connection((host, 9100), timeout=1)
    except TimeoutError as e:
        raise click.ClickException(f'Timed out while trying to connect to {host}, make sure that the printer is online') from e
    except OSError as e:
        raise click.ClickException(f'Could not connect to {host}: {e}') from e

    printer_status = Status()

    # Read printer status
    asb_status = connection.recv(SOCKET_BUFFER_SIZE)
    log.debug('First ASB: %s', repr([hex(x) for x in asb_status]))
    printer_status.parse(asb_status)
    if printer_status.errors:
        raise click.ClickException(f'Printer errors: {printer_status.errors}')

    # This is where we must clear all previous failures, if any
    # We might have to send junk data first, if the printer is still waiting for image data.
    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'B')])) # Quit raster mode
    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'C')])) # Clear raster data
    connection.sendall(bytes([0x1b, 0x1d, 0x03, 4, 0, 0])) # End document
    # We cannot proceed unless everything has been cleared and reinitialized by now

    # Reset ETB counter
    connection.sendall(bytes([0x1b, 0x1e, 0x45, 0]))
    time.sleep(0.1)
    printer_status = get_printer_status(host)
    if printer_status.etb_counter != 0:
        raise click.ClickException('Could not reset ETB counter')
    if printer_status.errors:
        raise click.ClickException(f'Printer errors: {printer_status.errors}')

    # Increase ETB
    connection.sendall(bytes([0x17]))
    time.sleep(0.1)
    new_printer_status = get_printer_status(host)
    if new_printer_status.etb_counter <= printer_status.etb_counter:
        raise click.ClickException('ETB counter did not increase')
    if new_printer_status.errors:
        raise click.ClickException(f'Printer errors: {printer_status.errors}')

    # Initialize printer

    # Start Document
    connection.sendall(bytes([0x1b, 0x1d, 0x03, 3, 0, 0]))

    connection.sendall(bytes([0x1b, 0x1e, 0x72, speed])) # Speed
    connection.sendall(bytes([0x1b, 0x1e, 0x64, density])) # Set print density
    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'R')])) # Init raster mode, this will clear the input buffer if theres any stray data
    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'A')])) # Enter raster mode
    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'Q'), 2, 0x00])) # Set raster print quality

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
        line_length = min(math.ceil(image.width / 8), BYTES_PER_LINE)
        connection.sendall(bytes([ord(b'b'), line_length, 0x00]))

        for _row in range(line_length):
            connection.sendall(bytes([raw_bytes[index]]))
            index += 1

    # Send bottom margin, 8 dots per millimeter
    for _line in range(8 * margin_bottom):
        connection.sendall(bytes([ord(b'b'), BYTES_PER_LINE, 0x00]))
        connection.sendall(bytes([0x00] * BYTES_PER_LINE))

    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'B')])) # Quit raster mode

    # Increase ETB
    connection.sendall(bytes([0x17]))

    # End document
    connection.sendall(bytes([0x1b, 0x1d, 0x03, 4, 0, 0]))

    # Wait for print to finish by waiting for the ETB counter to increase
    iteration_delay = 0.1
    for _iteration in range(int(print_timeout / iteration_delay)):
        time.sleep(iteration_delay)
        new_printer_status = get_printer_status(host)

        if new_printer_status.errors:
            raise click.ClickException(f'Printer errors: {new_printer_status.errors}')

        if new_printer_status.etb_counter > printer_status.etb_counter:
            log.debug("ETB increased from %d to %d, print finished!", printer_status.etb_counter, new_printer_status.etb_counter)
            break
    else:
        raise click.ClickException('Print failed, check printer')

    # Reset ETB counter
    connection.sendall(bytes([0x1b, 0x1e, 0x45, 0]))

    connection.close()
