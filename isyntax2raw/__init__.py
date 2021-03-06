#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2019 Glencoe Software, Inc. All rights reserved.
#
# This software is distributed under the terms described by the LICENSE.txt
# file you can find at the root of the distribution bundle.  If the file is
# missing please request a copy by contacting info@glencoesoftware.com

import json
import math
import os

import numpy as np
import pixelengine
import softwarerendercontext
import softwarerenderbackend
import zarr

from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, wait
from threading import BoundedSemaphore

from PIL import Image
from tifffile import imwrite


class MaxQueuePool(object):
    """This Class wraps a concurrent.futures.Executor
    limiting the size of its task queue.
    If `max_queue_size` tasks are submitted, the next call to submit will
    block until a previously submitted one is completed.

    Brought in from:
      * https://gist.github.com/noxdafox/4150eff0059ea43f6adbdd66e5d5e87e

    See also:
      * https://www.bettercodebytes.com/
            theadpoolexecutor-with-a-bounded-queue-in-python/
      * https://pypi.org/project/bounded-pool-executor/
      * https://bugs.python.org/issue14119
      * https://bugs.python.org/issue29595
      * https://github.com/python/cpython/pull/143
    """
    def __init__(self, executor, max_queue_size, max_workers=None):
        self.pool = executor(max_workers=max_workers)
        self.pool_queue = BoundedSemaphore(max_queue_size)

    def submit(self, function, *args, **kwargs):
        """Submits a new task to the pool, blocks if Pool queue is full."""
        self.pool_queue.acquire()

        future = self.pool.submit(function, *args, **kwargs)
        future.add_done_callback(self.pool_queue_callback)

        return future

    def pool_queue_callback(self, _):
        """Called once task is done, releases one queue slot."""
        self.pool_queue.release()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.pool.__exit__(exception_type, exception_value, traceback)


class WriteTiles(object):

    def __init__(
        self, tile_width, tile_height, resolutions, file_type, max_workers,
        input_path, output_path
    ):
        self.tile_width = tile_width
        self.tile_height = tile_height
        self.resolutions = resolutions
        self.file_type = file_type
        self.max_workers = max_workers
        self.input_path = input_path
        self.slide_directory = output_path

        os.mkdir(self.slide_directory)

        render_context = softwarerendercontext.SoftwareRenderContext()
        render_backend = softwarerenderbackend.SoftwareRenderBackend()

        self.pixel_engine = pixelengine.PixelEngine(
            render_backend, render_context
        )
        self.pixel_engine["in"].open(input_path, "ficom")

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.pixel_engine["in"].close()

    def write_metadata(self):
        '''write metadata to a JSON file'''
        pe_in = self.pixel_engine["in"]
        metadata_file = os.path.join(self.slide_directory, "METADATA.json")
        with open(metadata_file, "w", encoding="utf-8") as f:
            metadata = {
                "Barcode":
                    pe_in.BARCODE,
                "DICOM acquisition date":
                    pe_in.DICOM_ACQUISITION_DATETIME,
                "DICOM last calibration date":
                    pe_in.DICOM_DATE_OF_LAST_CALIBRATION,
                "DICOM time of last calibration":
                    pe_in.DICOM_TIME_OF_LAST_CALIBRATION,
                "DICOM manufacturer":
                pe_in.DICOM_MANUFACTURER,
                "DICOM manufacturer model name":
                    pe_in.DICOM_MANUFACTURERS_MODEL_NAME,
                "DICOM device serial number":
                    pe_in.DICOM_DEVICE_SERIAL_NUMBER,
                "Color space transform":
                    pe_in.colorspaceTransform(),
                "Block size":
                    pe_in.blockSize(),
                "Number of tiles":
                    pe_in.numTiles(),
                "Bits stored":
                    pe_in.bitsStored(),
                "Derivation description":
                    pe_in.DICOM_DERIVATION_DESCRIPTION,
                "DICOM software version":
                    pe_in.DICOM_SOFTWARE_VERSIONS,
                "Number of images":
                    pe_in.numImages()
            }

            for image in range(pe_in.numImages()):
                img = pe_in[image]
                image_metadata = {
                    "Image type": img.IMAGE_TYPE,
                    "DICOM lossy image compression method":
                        img.DICOM_LOSSY_IMAGE_COMPRESSION_METHOD,
                    "DICOM lossy image compression ratio":
                        img.DICOM_LOSSY_IMAGE_COMPRESSION_RATIO,
                    "DICOM derivation description":
                        img.DICOM_DERIVATION_DESCRIPTION,
                    "Image dimension names":
                        img.IMAGE_DIMENSION_NAMES,
                    "Image dimension types":
                        img.IMAGE_DIMENSION_TYPES,
                    "Image dimension units":
                        img.IMAGE_DIMENSION_UNITS,
                    "Image dimension ranges":
                        img.IMAGE_DIMENSION_RANGES,
                    "Image dimension discrete values":
                        img.IMAGE_DIMENSION_DISCRETE_VALUES_STRING,
                    "Image scale factor":
                        img.IMAGE_SCALE_FACTOR
                }

                if img.IMAGE_TYPE == "WSI":
                    view = pe_in.SourceView()
                    image_metadata["Bits allocated"] = view.bitsAllocated()
                    image_metadata["Bits stored"] = view.bitsStored()
                    image_metadata["High bit"] = view.highBit()
                    image_metadata["Pixel representation"] = \
                        view.pixelRepresentation()
                    image_metadata["Planar configuration"] = \
                        view.planarConfiguration()
                    image_metadata["Samples per pixel"] = \
                        view.samplesPerPixel()
                    image_metadata["Number of levels"] = \
                        pe_in.numLevels()

                    for resolution in range(pe_in.numLevels()):
                        dim_ranges = view.dimensionRanges(resolution)
                        image_metadata["Level sizes #%s" % resolution] = {
                            "X": self.get_size(dim_ranges[0]),
                            "Y": self.get_size(dim_ranges[1])
                        }

                metadata["Image #" + str(image)] = image_metadata

            json.dump(metadata, f)

    def get_size(self, dim_range):
        '''calculate the length in pixels of a dimension'''
        return (dim_range[2] - dim_range[0]) / dim_range[1]

    def write_label_image(self):
        '''write the label image (if present) as a JPEG file'''
        self.write_image_type("LABELIMAGE")

    def write_macro_image(self):
        '''write the macro image (if present) as a JPEG file'''
        self.write_image_type("MACROIMAGE")

    def find_image_type(self, image_type):
        '''look up a given image type in the pixel engine'''
        pe_in = self.pixel_engine["in"]
        for index in range(pe_in.numImages()):
            if image_type == pe_in[index].IMAGE_TYPE:
                return pe_in[index]
        return None

    def write_image_type(self, image_type):
        '''write an image of the specified type'''
        image_container = self.find_image_type(image_type)
        if image_container is not None:
            pixels = image_container.IMAGE_DATA
            image_file = os.path.join(
                self.slide_directory, '%s.jpg' % image_type
            )
            with open(image_file, "wb") as image:
                image.write(pixels)
            print("wrote %s image" % image_type)

    def create_tile_directory(self, resolution, width, height):
        tile_directory = os.path.join(
            self.slide_directory, str(resolution)
        )
        if self.file_type in ("n5", "zarr"):
            tile_directory = os.path.join(
                self.slide_directory, "pyramid.%s" % self.file_type
            )
            store = zarr.DirectoryStore(tile_directory)
            if self.file_type == "n5":
                store = zarr.N5Store(tile_directory)
            group = zarr.group(store=store)
            dataset = group.create_dataset(
                str(resolution), shape=(3, height, width),
                chunks=(None, self.tile_height, self.tile_width), dtype='B'
            )
        else:
            os.mkdir(tile_directory)
        return tile_directory

    def get_tile_filename(self, tile_directory, x_start, y_start):
        filename = os.path.join(
            os.path.join(tile_directory, str(x_start)),
            "%s.%s" % (y_start, self.file_type)
        )
        if self.file_type in ("n5", "zarr"):
            filename = tile_directory
        return filename

    def make_planar(self, pixels, tile_width, tile_height):
        r = pixels[0::3]
        g = pixels[1::3]
        b = pixels[2::3]
        for v in (r, g, b):
            v.shape = (tile_height, tile_width)
        return np.array([r, g, b])

    def write_pyramid(self):
        '''write the slide's pyramid as a set of tiles'''
        pe_in = self.pixel_engine["in"]
        image_container = self.find_image_type("WSI")

        scanned_areas = image_container.IMAGE_VALID_DATA_ENVELOPES
        if scanned_areas is None:
            raise RuntimeError("No valid data envelopes")

        if self.resolutions is None:
            resolutions = range(pe_in.numLevels())
        else:
            resolutions = range(self.resolutions)

        def write_tile(
            pixels, resolution, x_start, y_start, tile_width, tile_height,
            filename
        ):
            x_end = x_start + tile_width
            y_end = y_start + tile_height
            try:
                if self.file_type in ("n5", "zarr"):
                    # Special case for N5/Zarr which has a single n-dimensional
                    # array representation on disk
                    pixels = self.make_planar(pixels, tile_width, tile_height)
                    z = zarr.open(filename)[str(resolution)]
                    z[:, y_start:y_end, x_start:x_end] = pixels
                elif self.file_type == 'tiff':
                    # Special case for TIFF to save in planar mode using
                    # deinterleaving and the tifffile library; planar data
                    # is much more performant with the Bio-Formats API
                    pixels = self.make_planar(pixels, tile_width, tile_height)
                    with open(filename, 'wb') as destination:
                        imwrite(destination, pixels, planarconfig='SEPARATE')
                else:
                    with Image.frombuffer(
                        'RGB', (int(tile_width), int(tile_height)),
                        pixels, 'raw', 'RGB', 0, 1
                    ) as source, open(filename, 'wb') as destination:
                        source.save(destination)
            except Exception:
                import traceback
                traceback.print_exc()
                print(
                    "Failed to write tile [:, %d:%d, %d:%d] to %s" % (
                        x_start, x_end, y_start, y_end, filename
                    )
                )

        source_view = pe_in.SourceView()
        for resolution in resolutions:
            # assemble data envelopes (== scanned areas) to extract for
            # this level
            dim_ranges = source_view.dimensionRanges(resolution)
            print("dimension ranges = " + str(dim_ranges))
            resolution_x_end = math.ceil(self.get_size(dim_ranges[0]))
            resolution_y_end = math.ceil(self.get_size(dim_ranges[1]))

            x_tiles = math.ceil(resolution_x_end / self.tile_width)
            y_tiles = math.ceil(resolution_y_end / self.tile_height)

            print("# of X tiles = %s" % x_tiles)
            print("# of Y tiles = %s" % y_tiles)

            # create one tile directory per resolution level if required
            tile_directory = self.create_tile_directory(
                resolution, resolution_x_end + 1, resolution_y_end + 1
            )

            patches, patch_identifier = self.create_patch_list(
                dim_ranges[0][2], dim_ranges[1][2], [x_tiles, y_tiles],
                [self.tile_width, self.tile_height],
                [dim_ranges[0][0], dim_ranges[1][0]],
                resolution, tile_directory
            )

            envelopes = source_view.dataEnvelopes(resolution)
            regions = source_view.requestRegions(
                patches, envelopes, True, [0, 0, 0])

            jobs = ()
            with MaxQueuePool(ThreadPoolExecutor, self.max_workers) as pool:
                while regions:
                    regions_ready = self.pixel_engine.waitAny(regions)
                    for region_index, region in enumerate(regions_ready):
                        view_range = region.range
                        print("processing tile %s" % view_range)
                        x_start, x_end, y_start, y_end, level = view_range
                        width = int(1 + (x_end - x_start) / dim_ranges[0][1])
                        height = int(1 + (y_end - y_start) / dim_ranges[1][1])
                        pixel_buffer_size = width * height * 3
                        pixels = np.empty(int(pixel_buffer_size), dtype='B')
                        patch_id = patch_identifier[regions.index(region)]
                        x_start, y_start = patch_id
                        x_start *= self.tile_width
                        y_start *= self.tile_height
                        patch_identifier.remove(patch_id)

                        region.get(pixels)
                        regions.remove(region)

                        filename = self.get_tile_filename(
                            tile_directory, x_start, y_start
                        )
                        jobs = jobs + (pool.submit(
                            write_tile, pixels, resolution,
                            x_start, y_start, width, height,
                            filename
                        ),)
            wait(jobs, return_when=ALL_COMPLETED)

    def create_x_directory(self, tile_directory, x_start):
        if self.file_type in ("n5", "zarr"):
            return

        x_directory = os.path.join(tile_directory, str(x_start))
        if not os.path.exists(x_directory):
            os.mkdir(x_directory)

    def create_patch_list(
        self, image_x_end, image_y_end, tiles, tile_size, origin, level,
        tile_directory
    ):
        patches = []
        patch_identifier = []
        scale = 2 ** level
        tile_size[0] = tile_size[0] * scale
        tile_size[1] = tile_size[1] * scale
        for y in range(tiles[1]):
            y_start = origin[1] + (y * tile_size[1])
            y_end = min((y_start + tile_size[1]) - scale, image_y_end)
            for x in range(tiles[0]):
                x_start = origin[0] + (x * tile_size[0])
                x_end = min((x_start + tile_size[0]) - scale, image_x_end)
                patch = [x_start, x_end, y_start, y_end, level]
                patches.append(patch)
                # Associating spatial information (tile X and Y offset) in
                # order to identify the patches returned asynchronously
                patch_identifier.append((x, y))

                self.create_x_directory(tile_directory, x * self.tile_width)
        return patches, patch_identifier
