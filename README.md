[![AppVeyor status](https://ci.appveyor.com/api/projects/status/github/isyntax2raw)](https://ci.appveyor.com/project/gs-jenkins/isyntax2raw)

# iSyntax Converter

Python tool that uses Philips' SDK to write slides in an intermediary raw format.

## Requirements

* Philips iSyntax SDK (https://openpathology.philips.com)

## Usage

Basic usage is:

    isyntax2raw write_tiles /path/to/input.isyntax /path/to/tile/directory

Please see `isyntax2raw write_tiles --help` for detailed information.

Output tile width and height can optionally be specified; default values are
detailed in `--help`.

A directory structure containing the pyramid tiles at all resolutions and
macro/label images will be created.  Additional metadata is written to a
JSON file.  The root directory is in the same directory as the .isyntax file.
Be mindful of available disk space, as larger .isyntax files can result
in >20 GB of tiles.

Use of a n5 or zarr `--file_type` will result in losslessly compressed output.
Both of these formats are supported by the downstream `raw-to-ome-tiff`.

## Areas to improve

* Currently assumes brightfield (RGB, 8 bits per channel) without really
  checking the metadata.  Probably should check bit depths etc.