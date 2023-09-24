import logging
import socket
import time

import click
from PIL import Image, ImageOps, UnidentifiedImageError


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


@click.command(context_settings={'show_default': True})
@click.option('--density', default=3, type=click.IntRange(0, 6), help='0 = Highest density, 6 = Lowest density')
@click.option('--dither', default='NONE', type=click.Choice(['NONE', 'FLOYDSTEINBERG'], case_sensitive=False))
@click.option('--margin-top', default=0)
@click.option('--margin-bottom', default=9)
@click.option('--resize-width', type=int, help='Resizes input image to the given width while preserving aspect ratio')
@click.option('--speed', default=2, type=click.IntRange(0, 2), help='0 = Fastest, 2 = Slowest')
@click.argument('printer-ip')
@click.argument('input', type=click.File('rb'))
def print_image(printer_ip, input, density, dither, margin_top, margin_bottom, resize_width, speed):
    '''
    This is a small utility for sending raster images to Star Micronics TSP100 / TSP143 receipt printers.

    The program expects bilevel (black and white) images, at most 576 pixels wide. Wider images will be cropped.
    '''

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
        connection = socket.create_connection((printer_ip, 9100), timeout=1)
    except TimeoutError:
        raise click.ClickException(f'Timed out while trying to connect to {printer_ip}, make sure that the printer is online')
    except OSError as e:
        raise click.ClickException(f'Could not connect to {printer_ip}: {e}')

    # Read printer status
    data = connection.recv(1024)
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
    connection.sendall(bytes([0x1b, ord(b'*'), ord(b'r'), ord(b'Q'), 2, 0x00])) # Raster quality, not sure if this does anything
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
            connection = socket.create_connection((printer_ip, 9100), timeout=1)
        except (TimeoutError, OSError):
            time.sleep(1)
            continue
        
        data = connection.recv(1024)
        log.debug(f'Print verification ASB: {repr([hex(x) for x in data])}')
    
        if any(data[2:]):
            raise click.ClickException('Print might have failed, check printer')

        connection.close()
        time.sleep(1) # Wait for the cutter to do its thing before exiting
        break

    else:
        raise click.ClickException('Could not verify print, check printer')
