# pylint: disable-all

# Copyright (c) 2012-2013 Intel, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License, version 2,
# as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.

"""
This module implements copying of images with bmap and provides the following
API.
  1. BmapCopy class - implements copying to any kind of file, be that a block
     device or a regular file.
  2. BmapBdevCopy class - based on BmapCopy and specializes on copying to block
     devices. It does some more sanity checks and some block device performance
     tuning.

The bmap file is an XML file which contains a list of mapped blocks of the
image. Mapped blocks are the blocks which have disk sectors associated with
them, as opposed to holes, which are blocks with no associated disk sectors. In
other words, the image is considered to be a sparse file, and bmap basically
contains a list of mapped blocks of this sparse file. The bmap additionally
contains some useful information like block size (usually 4KiB), image size,
mapped blocks count, etc.

The bmap is used for copying the image to a block device or to a regular file.
The idea is that we copy quickly with bmap because we copy only mapped blocks
and ignore the holes, because they are useless. And if the image is generated
properly (starting with a huge hole and writing all the data), it usually
contains only little mapped blocks, comparing to the overall image size. And
such an image compresses very well (because holes are read as all zeroes), so
it is beneficial to distributor them as compressed files along with the bmap.

Here is an example. Suppose you have a 4GiB image which contains only 100MiB of
user data and you need to flash it to a slow USB stick. With bmap you end up
copying only a little bit more than 100MiB of data from the image to the USB
stick (namely, you copy only mapped blocks). This is a lot faster than copying
all 4GiB of data. We say that it is a bit more than 100MiB because things like
file-system meta-data (inode tables, superblocks, etc), partition table, etc
also contribute to the mapped blocks and are also copied.
"""

# Disable the following pylint recommendations:
#   * Too many instance attributes (R0902)
# pylint: disable=R0902

import os
import stat
import sys
import hashlib
import logging
import datetime
from six import reraise

if sys.version[0] == '2':
    import Queue
    import thread
else:
    import queue as  Queue
    import _thread as thread

from xml.etree import ElementTree
from bmaptools.BmapHelpers import human_size

# The highest supported bmap format version
SUPPORTED_BMAP_VERSION = "1.0"

class Error(Exception):
    """
    A class for exceptions generated by the 'BmapCopy' module. We currently
    support only one type of exceptions, and we basically throw human-readable
    problem description in case of errors.
    """
    pass

class BmapCopy:
    """
    This class implements the bmap-based copying functionality. To copy an
    image with bmap you should create an instance of this class, which requires
    the following:

    * full path or a file-like object of the image to copy
    * full path or a file object of the destination file copy the image to
    * full path or a file object of the bmap file (optional)
    * image size in bytes (optional)

    Although the main purpose of this class is to use bmap, the bmap is not
    required, and if it was not provided then the entire image will be copied
    to the destination file.

    When the bmap is provided, it is not necessary to specify image size,
    because the size is contained in the bmap. Otherwise, it is benefitial to
    specify the size because it enables extra sanity checks and makes it
    possible to provide the progress bar.

    When the image size is known either from the bmap or the caller specified
    it to the class constructor, all the image geometry description attributes
    ('blocks_cnt', etc) are initialized by the class constructor and available
    for the user.

    However, when the size is not known, some of  the image geometry
    description attributes are not initialized by the class constructor.
    Instead, they are initialized only by the 'copy()' method.

    The 'copy()' method implements image copying. You may choose whether to
    verify the SHA1 checksum while copying or not. Note, this is done only in
    case of bmap-based copying and only if bmap contains the SHA1 checksums
    (e.g., bmap version 1.0 did not have SHA1 checksums).

    You may choose whether to synchronize the destination file after writing or
    not. To explicitly synchronize it, use the 'sync()' method.

    This class supports all the bmap format versions up version
    'SUPPORTED_BMAP_VERSION'.

    It is possible to have a simple progress indicator while copying the image.
    Use the 'set_progress_indicator()' method.

    You can copy only once with an instance of this class. This means that in
    order to copy the image for the second time, you have to create a new class
    instance.
    """

    def __init__(self, image, dest, bmap=None, image_size=None, logger=None):
        """
        The class constructor. The parameters are:
            image      - file-like object of the image which should be copied,
                         should only support 'read()' and 'seek()' methods,
                         and only seeking forward has to be supported.
            dest       - file object of the destination file to copy the image
                         to.
            bmap       - file object of the bmap file to use for copying.
            image_size - size of the image in bytes.
            logger     - the logger object to use for printing messages.
        """

        self._logger = logger
        if self._logger is None:
            self._logger = logging.getLogger(__name__)

        self._xml = None

        self._dest_fsync_watermark = None
        self._batch_blocks = None
        self._batch_queue = None
        self._batch_bytes = 1024 * 1024
        self._batch_queue_len = 2

        self.bmap_version = None
        self.bmap_version_major = None
        self.bmap_version_minor = None
        self.block_size = None
        self.blocks_cnt = None
        self.mapped_cnt = None
        self.image_size = None
        self.image_size_human = None
        self.mapped_size = None
        self.mapped_size_human = None
        self.mapped_percent = None

        self._f_bmap = None
        self._f_bmap_path = None

        self._progress_started = None
        self._progress_index = None
        self._progress_time = None
        self._progress_file = None
        self._progress_format = None
        self.set_progress_indicator(None, None)

        self._f_image = image
        self._image_path = image.name

        self._f_dest = dest
        self._dest_path = dest.name
        st_data = os.fstat(self._f_dest.fileno())
        self._dest_is_regfile = stat.S_ISREG(st_data.st_mode)

        # Special quirk for /dev/null which does not support fsync()
        if stat.S_ISCHR(st_data.st_mode) and \
           os.major(st_data.st_rdev) == 1 and \
           os.minor(st_data.st_rdev) == 3:
            self._dest_supports_fsync = False
        else:
            self._dest_supports_fsync = True

        if bmap:
            self._f_bmap = bmap
            self._bmap_path = bmap.name
            self._parse_bmap()
        else:
            # There is no bmap. Initialize user-visible attributes to something
            # sensible with an assumption that we just have all blocks mapped.
            self.bmap_version = 0
            self.block_size = 4096
            self.mapped_percent = 100

        if image_size:
            self._set_image_size(image_size)

        self._batch_blocks = self._batch_bytes / self.block_size

    def set_progress_indicator(self, file_obj, format_string):
        """
        Setup the progress indicator which shows how much data has been copied
        in percent.

        The 'file_obj' argument is the console file object where the progress
        has to be printed to. Pass 'None' to disable the progress indicator.

        The 'format_string' argument is the format string for the progress
        indicator. It has to contain a single '%d' placeholder which will be
        substitutes with copied data in percent.
        """

        self._progress_file = file_obj
        if format_string:
            self._progress_format = format_string
        else:
            self._progress_format = "Copied %d%%"

    def _set_image_size(self, image_size):
        """
        Set image size and initialize various other geometry-related attributes.
        """

        if self.image_size is not None and self.image_size != image_size:
            raise Error("cannot set image size to %d bytes, it is known to "
                        "be %d bytes (%s)" % (image_size, self.image_size,
                                              self.image_size_human))

        self.image_size = image_size
        self.image_size_human = human_size(image_size)
        self.blocks_cnt = self.image_size + self.block_size - 1
        self.blocks_cnt /= self.block_size

        if self.mapped_cnt is None:
            self.mapped_cnt = self.blocks_cnt
            self.mapped_size = self.image_size
            self.mapped_size_human = self.image_size_human

    def _verify_bmap_checksum(self):
        """
        This is a helper function which verifies SHA1 checksum of the bmap file.
        """

        import mmap

        correct_sha1 = self._xml.find("BmapFileSHA1").text.strip()

        # Before verifying the shecksum, we have to substitute the SHA1 value
        # stored in the file with all zeroes. For these purposes we create
        # private memory mapping of the bmap file.
        mapped_bmap = mmap.mmap(self._f_bmap.fileno(), 0,
                                access = mmap.ACCESS_COPY)

        sha1_pos = mapped_bmap.find(correct_sha1)
        assert sha1_pos != -1

        mapped_bmap[sha1_pos:sha1_pos + 40] = '0' * 40
        calculated_sha1 = hashlib.sha1(mapped_bmap).hexdigest()

        mapped_bmap.close()

        if calculated_sha1 != correct_sha1:
            raise Error("checksum mismatch for bmap file '%s': calculated "
                        "'%s', should be '%s'"
                        % (self._bmap_path, calculated_sha1, correct_sha1))

    def _parse_bmap(self):
        """
        Parse the bmap file and initialize corresponding class instance attributs.
        """

        try:
            self._xml = ElementTree.parse(self._f_bmap)
        except  ElementTree.ParseError as err:
            raise Error("cannot parse the bmap file '%s' which should be a "
                        "proper XML file: %s" % (self._bmap_path, err))

        xml = self._xml
        self.bmap_version = str(xml.getroot().attrib.get('version'))

        # Make sure we support this version
        self.bmap_version_major = int(self.bmap_version.split('.', 1)[0])
        self.bmap_version_minor = int(self.bmap_version.split('.', 1)[1])
        if self.bmap_version_major > SUPPORTED_BMAP_VERSION:
            raise Error("only bmap format version up to %d is supported, "
                        "version %d is not supported"
                        % (SUPPORTED_BMAP_VERSION, self.bmap_version_major))

        # Fetch interesting data from the bmap XML file
        self.block_size = int(xml.find("BlockSize").text.strip())
        self.blocks_cnt = int(xml.find("BlocksCount").text.strip())
        self.mapped_cnt = int(xml.find("MappedBlocksCount").text.strip())
        self.image_size = int(xml.find("ImageSize").text.strip())
        self.image_size_human = human_size(self.image_size)
        self.mapped_size = self.mapped_cnt * self.block_size
        self.mapped_size_human = human_size(self.mapped_size)
        self.mapped_percent = (self.mapped_cnt * 100.0) / self.blocks_cnt

        blocks_cnt = (self.image_size + self.block_size - 1) / self.block_size
        if self.blocks_cnt != blocks_cnt:
            raise Error("Inconsistent bmap - image size does not match "
                        "blocks count (%d bytes != %d blocks * %d bytes)"
                        % (self.image_size, self.blocks_cnt, self.block_size))

        if self.bmap_version_major >= 1 and self.bmap_version_minor >= 3:
            # Bmap file checksum appeard in format 1.3
            self._verify_bmap_checksum()

    def _update_progress(self, blocks_written):
        """
        Print the progress indicator if the mapped area size is known and if
        the indicator has been enabled by assigning a console file object to
        the 'progress_file' attribute.
        """

        if not self._progress_file:
            return

        if self.mapped_cnt:
            assert blocks_written <= self.mapped_cnt
            percent = int((float(blocks_written) / self.mapped_cnt) * 100)
            progress = '\r' + self._progress_format % percent + '\n'
        else:
            # Do not rotate the wheel too fast
            now = datetime.datetime.now()
            min_delta = datetime.timedelta(milliseconds=250)
            if now - self._progress_time < min_delta:
                return
            self._progress_time = now

            progress_wheel = ('-', '\\', '|', '/')
            progress = '\r' + progress_wheel[self._progress_index % 4] + '\n'
            self._progress_index += 1

        # This is a little trick we do in order to make sure that the next
        # message will always start from a new line - we switch to the new
        # line after each progress update and move the cursor up. As an
        # example, this is useful when the copying is interrupted by an
        # exception - the error message will start form new line.
        if self._progress_started:
            # The "move cursor up" escape sequence
            self._progress_file.write('\033[1A') # pylint: disable=W1401
        else:
            self._progress_started = True

        self._progress_file.write(progress)
        self._progress_file.flush()

    def _get_block_ranges(self):
        """
        This is a helper generator that parses the bmap XML file and for each
        block range in the XML file it yields ('first', 'last', 'sha1') tuples,
        where:
          * 'first' is the first block of the range;
          * 'last' is the last block of the range;
          * 'sha1' is the SHA1 checksum of the range ('None' is used if it is
            missing.

        If there is no bmap file, the generator just yields a single range
        for entire image file. If the image size is unknown, the generator
        infinitely yields continuous ranges of size '_batch_blocks'.
        """

        if not self._f_bmap:
            # We do not have the bmap, yield a tuple with all blocks
            if self.blocks_cnt:
                yield (0, self.blocks_cnt - 1, None)
            else:
                # We do not know image size, keep yielding tuples with many
                # blocks infinitely.
                first = 0
                while True:
                    yield (first, first + self._batch_blocks - 1, None)
                    first += self._batch_blocks
            return

        # We have the bmap, just read it and yield block ranges
        xml = self._xml
        xml_bmap = xml.find("BlockMap")

        for xml_element in xml_bmap.findall("Range"):
            blocks_range = xml_element.text.strip()
            # The range of blocks has the "X - Y" format, or it can be just "X"
            # in old bmap format versions. First, split the blocks range string
            # and strip white-spaces.
            split = [x.strip() for x in blocks_range.split('-', 1)]

            first = int(split[0])
            if len(split) > 1:
                last = int(split[1])
                if first > last:
                    raise Error("bad range (first > last): '%s'" % blocks_range)
            else:
                last = first

            if 'sha1' in xml_element.attrib:
                sha1 = xml_element.attrib['sha1']
            else:
                sha1 = None

            yield (first, last, sha1)

    def _get_batches(self, first, last):
        """
        This is a helper generator which splits block ranges from the bmap file
        to smaller batches. Indeed, we cannot read and write entire block
        ranges from the image file, because a range can be very large. So we
        perform the I/O in batches. Batch size is defined by the
        '_batch_blocks' attribute. Thus, for each (first, last) block range,
        the generator yields smaller (start, end, length) batch ranges, where:
          * 'start' is the starting batch block number;
          * 'last' is the ending batch block number;
          * 'length' is the batch length in blocks (same as
             'end' - 'start' + 1).
        """

        batch_blocks = self._batch_blocks

        while first + batch_blocks - 1 <= last:
            yield (first, first + batch_blocks - 1, batch_blocks)
            first += batch_blocks

        batch_blocks = last - first + 1
        if batch_blocks:
            yield (first, first + batch_blocks - 1, batch_blocks)

    def _get_data(self, verify):
        """
        This is generator  which reads the image file in '_batch_blocks' chunks
        and yields ('type', 'start', 'end',  'buf) tuples, where:
          * 'start' is the starting block number of the batch;
          * 'end' is the last block of the batch;
          * 'buf' a buffer containing the batch data.
        """

        try:
            for (first, last, sha1) in self._get_block_ranges():
                if verify and sha1:
                    hash_obj = hashlib.new('sha1')

                self._f_image.seek(first * self.block_size)

                iterator = self._get_batches(first, last)
                for (start, end, length) in iterator:
                    try:
                        buf = self._f_image.read(length * self.block_size)
                    except IOError as err:
                        raise Error("error while reading blocks %d-%d of the "
                                    "image file '%s': %s"
                                    % (start, end, self._image_path, err))

                    if not buf:
                        self._batch_queue.put(None)
                        return

                    if verify and sha1:
                        hash_obj.update(buf)

                    blocks = (len(buf) + self.block_size - 1) / self.block_size
                    self._batch_queue.put(("range", start, start + blocks - 1,
                                           buf))

                if verify and sha1 and hash_obj.hexdigest() != sha1:
                    raise Error("checksum mismatch for blocks range %d-%d: "
                                "calculated %s, should be %s (image file %s)"
                                % (first, last, hash_obj.hexdigest(),
                                   sha1, self._image_path))
        # Silence pylint warning about catching too general exception
        # pylint: disable=W0703
        except Exception:
            # pylint: enable=W0703
            # In case of any exception - just pass it to the main thread
            # through the queue.
            reraise(exc_info[0], exc_info[1], exc_info[2])

        self._batch_queue.put(None)

    def copy(self, sync=True, verify=True):
        """
        Copy the image to the destination file using bmap. The 'sync' argument
        defines whether the destination file has to be synchronized upon
        return.  The 'verify' argument defines whether the SHA1 checksum has to
        be verified while copying.
        """

        # Create the queue for block batches and start the reader thread, which
        # will read the image in batches and put the results to '_batch_queue'.
        self._batch_queue = Queue.Queue(self._batch_queue_len)
        thread.start_new_thread(self._get_data, (verify, ))

        blocks_written = 0
        bytes_written = 0
        fsync_last = 0

        self._progress_started = False
        self._progress_index = 0
        self._progress_time = datetime.datetime.now()

        # Read the image in '_batch_blocks' chunks and write them to the
        # destination file
        while True:
            batch = self._batch_queue.get()
            if batch is None:
                # No more data, the image is written
                break
            elif batch[0] == "error":
                # The reader thread encountered an error and passed us the
                # exception.
                exc_info = batch[1]
                raise exc_info[1].with_traceback(exc_info[2])

            (start, end, buf) = batch[1:4]

            assert len(buf) <= (end - start + 1) * self.block_size
            assert len(buf) > (end - start) * self.block_size

            self._f_dest.seek(start * self.block_size)

            # Synchronize the destination file if we reached the watermark
            if self._dest_fsync_watermark:
                if blocks_written >= fsync_last + self._dest_fsync_watermark:
                    fsync_last = blocks_written
                    self.sync()

            try:
                self._f_dest.write(buf)
            except IOError as err:
                raise Error("error while writing blocks %d-%d of '%s': %s"
                            % (start, end, self._dest_path, err))

            self._batch_queue.task_done()
            blocks_written += (end - start + 1)
            bytes_written += len(buf)

            self._update_progress(blocks_written)

        if not self.image_size:
            # The image size was unknown up until now, set it
            self._set_image_size(bytes_written)

        # This is just a sanity check - we should have written exactly
        # 'mapped_cnt' blocks.
        if blocks_written != self.mapped_cnt:
            raise Error("wrote %u blocks from image '%s' to '%s', but should "
                        "have %u - bmap file '%s' does not belong to this "
                        "image"
                        % (blocks_written, self._image_path, self._dest_path,
                           self.mapped_cnt, self._bmap_path))

        if self._dest_is_regfile:
            # Make sure the destination file has the same size as the image
            try:
                os.ftruncate(self._f_dest.fileno(), self.image_size)
            except OSError as err:
                raise Error("cannot truncate file '%s': %s"
                            % (self._dest_path, err))

        try:
            self._f_dest.flush()
        except IOError as err:
            raise Error("cannot flush '%s': %s" % (self._dest_path, err))

        if sync:
            self.sync()

    def sync(self):
        """
        Synchronize the destination file to make sure all the data are actually
        written to the disk.
        """

        if self._dest_supports_fsync:
            try:
                os.fsync(self._f_dest.fileno()),
            except OSError as err:
                raise Error("cannot synchronize '%s': %s "
                            % (self._dest_path, err.strerror))


class BmapBdevCopy(BmapCopy):
    """
    This class is a specialized version of 'BmapCopy' which copies the image to
    a block device. Unlike the base 'BmapCopy' class, this class does various
    optimizations specific to block devices, e.g., switching to the 'noop' I/O
    scheduler.
    """

    def __init__(self, image, dest, bmap=None, image_size=None, logger=None):
        """
        The same as the constructor of the 'BmapCopy' base class, but adds
        useful guard-checks specific to block devices.
        """

        # Call the base class constructor first
        BmapCopy.__init__(self, image, dest, bmap, image_size, logger=logger)

        self._batch_bytes = 1024 * 1024
        self._batch_blocks = self._batch_bytes / self.block_size
        self._batch_queue_len = 6
        self._dest_fsync_watermark = (6 * 1024 * 1024) / self.block_size

        self._sysfs_base = None
        self._sysfs_scheduler_path = None
        self._sysfs_max_ratio_path = None
        self._old_scheduler_value = None
        self._old_max_ratio_value = None

        # If the image size is known, check that it fits the block device
        if self.image_size:
            try:
                bdev_size = os.lseek(self._f_dest.fileno(), 0, os.SEEK_END)
                os.lseek(self._f_dest.fileno(), 0, os.SEEK_SET)
            except OSError as err:
                raise Error("cannot seed block device '%s': %s "
                            % (self._dest_path, err.strerror))

            if bdev_size < self.image_size:
                raise Error("the image file '%s' has size %s and it will not "
                            "fit the block device '%s' which has %s capacity"
                            % (self._image_path, self.image_size_human,
                               self._dest_path, human_size(bdev_size)))

        # Construct the path to the sysfs directory of our block device
        st_rdev = os.fstat(self._f_dest.fileno()).st_rdev
        self._sysfs_base = "/sys/dev/block/%s:%s/" \
                           % (os.major(st_rdev), os.minor(st_rdev))

        # Check if the 'queue' sub-directory exists. If yes, then our block
        # device is entire disk. Otherwise, it is a partition, in which case we
        # need to go one level up in the sysfs hierarchy.
        if not os.path.exists(self._sysfs_base + "queue"):
            self._sysfs_base = self._sysfs_base + "../"

        self._sysfs_scheduler_path = self._sysfs_base + "queue/scheduler"
        self._sysfs_max_ratio_path = self._sysfs_base + "bdi/max_ratio"

    def _tune_block_device(self):
        """
        Tune the block device for better performance:
        1. Switch to the 'noop' I/O scheduler if it is available - sequential
           write to the block device becomes a lot faster comparing to CFQ.
        2. Limit the write buffering - we do not need the kernel to buffer a
           lot of the data we send to the block device, because we write
           sequentially. Limit the buffering.

        The old settings are saved in order to be able to restore them later.
        """
        # Switch to the 'noop' I/O scheduler
        try:
            with open(self._sysfs_scheduler_path, "r+") as f_scheduler:
                contents = f_scheduler.read()
                f_scheduler.seek(0)
                f_scheduler.write("noop")
        except IOError as err:
            self._logger.warning("failed to enable I/O optimization, expect "
                                 "suboptimal speed (reason: cannot switch "
                                 "to the 'noop' I/O scheduler: %s)" % err)
        else:
            # The file contains a list of schedulers with the current
            # scheduler in square brackets, e.g., "noop deadline [cfq]".
            # Fetch the name of the current scheduler.
            import re

            match = re.match(r'.*\[(.+)\].*', contents)
            if match:
                self._old_scheduler_value = match.group(1)

        # Limit the write buffering, because we do not need too much of it when
        # writing sequntially. Excessive buffering makes some systems not very
        # responsive, e.g., this was observed in Fedora 17.
        try:
            with open(self._sysfs_max_ratio_path, "r+") as f_ratio:
                self._old_max_ratio_value = f_ratio.read()
                f_ratio.seek(0)
                f_ratio.write("1")
        except IOError as err:
            self._logger.warning("failed to disable excessive buffering, "
                                 "expect worse system responsiveness "
                                 "(reason: cannot set max. I/O ratio to "
                                 "1: %s)" % err)

    def _restore_bdev_settings(self):
        """
        Restore old block device settings which we changed in
        '_tune_block_device()'.
        """

        if self._old_scheduler_value is not None:
            try:
                with open(self._sysfs_scheduler_path, "w") as f_scheduler:
                    f_scheduler.write(self._old_scheduler_value)
            except IOError as err:
                raise Error("cannot restore the '%s' I/O scheduler: %s"
                            % (self._old_scheduler_value, err))

        if self._old_max_ratio_value is not None:
            try:
                with open(self._sysfs_max_ratio_path, "w") as f_ratio:
                    f_ratio.write(self._old_max_ratio_value)
            except IOError as err:
                raise Error("cannot set the max. I/O ratio back to '%s': %s"
                            % (self._old_max_ratio_value, err))

    def copy(self, sync=True, verify=True):
        """
        The same as in the base class but tunes the block device for better
        performance before starting writing. Additionally, it forces block
        device synchronization from time to time in order to make sure we do
        not get stuck in 'fsync()' for too long time. The problem is that the
        kernel synchronizes block devices when the file is closed. And the
        result is that if the user interrupts us while we are copying the data,
        the program will be blocked in 'close()' waiting for the block device
        synchronization, which may last minutes for slow USB stick. This is
        very bad user experience, and we work around this effect by
        synchronizing from time to time.
        """

        self._tune_block_device()

        try:
            BmapCopy.copy(self, sync, verify)
        except:
            raise
        finally:
            self._restore_bdev_settings()
