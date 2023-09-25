# tsp100-print

A small utility for sending raster images to LAN-connected Star Micronics TSP100LAN / TSP143 receipt printers.

The program expects bilevel images, at most 576 pixels wide, and it will complain if you try to feed it anything else.

See the [STAR Graphic Mode Command Specifications, Rev. 2.32](https://starmicronics.com/support/Mannualfolder/star_graphic_cm_en.pdf) for detailed information on how to talk to these printers.


## Usage example

    $ tsp100-print <printer-ip> <input.png>


# Installation instructions

Clone the repository:

    $ git clone https://github.com/dalpil/tsp100-print.git
    $ cd tsp100-print

And then install with pip:

    $ pip install .

Or with poetry:

    $ poetry shell
    $ poetry install

The `tsp100-print` command should now be available.


## Usage instructions

    Usage: tsp100-print [OPTIONS] PRINTER_IP INPUT
    
      This is a small utility for sending raster images to Star Micronics TSP100 /
      TSP143 receipt printers.
    
      The program expects bilevel (black and white) images, at most 576 pixels
      wide. Wider images will be cropped.
    
    Options:
      --cut / --no-cut                Whether or not to cut receipt after printing
                                      [default: cut]
      --density INTEGER RANGE         0 = Highest density, 6 = Lowest density
                                      [default: 3; 0<=x<=6]
      --dither [NONE|FLOYDSTEINBERG]  [default: NONE]
      --margin-top INTEGER            [default: 0]
      --margin-bottom INTEGER         [default: 9]
      --resize-width INTEGER          Resizes input image to the given width while
                                      preserving aspect ratio
      --speed INTEGER RANGE           0 = Fastest, 2 = Slowest  [default: 2;
                                      0<=x<=2]
      --help                          Show this message and exit.

