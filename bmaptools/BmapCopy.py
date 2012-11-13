""" This module implements copying of images with bmap and provides the
following API.
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

The bmap is used for copying the image to a block device or to a regualr file.
The idea is that we copy quickly with bmap because we copy only mapped blocks
and ignore the holes, because they are useless. And if the image is generated
properly (starting with a huge hole and writing all the data), it usually
contains only little mapped blocks, comparing to the overall image size. And
such an image compresses very well (because holes are read as all zeroes), so
it is benefitial to destribute them as compressed files along with the bmap.

Here is an example. Suppose you have a 4GiB image which contains only 100MiB of
user data and you need to flash it to a slow USB stick. With bmap you end up
copying only a little bit more than 100MiB of data from the image to the USB
stick (namely, you copy only mapped blocks). This is a lot faster than copying
all 4GiB of data. We say that it is a bit more than 100MiB because things like
file-system meta-data (inode tables, superblocks, etc), partition table, etc
also contribute to the mapped blocks and are also copied. """

import os
import stat
import hashlib
from xml.etree import ElementTree
from bmaptools.BmapHelpers import human_size

# A list of supported image formats
SUPPORTED_IMAGE_FORMATS = ('bz2', 'gz', 'tar.gz', 'tgz', 'tar.bz2')

# The highest supported bmap format version
SUPPORTED_BMAP_VERSION = 1

class Error(Exception):
    """ A class for exceptions generated by the 'BmapCopy' module. We currently
    support only one type of exceptions, and we basically throw human-readable
    problem description in case of errors. """
    pass

class BmapCopy:
    """ This class implements the bmap-based copying functionality. To copy an
    image with bmap you should create an instance of this class, which requires
    the following:

    * full path to the image to copy
    * full path to the destination file copy the image to
    * full path to the bmap file (optional)

    Although the main purpose of this class is to use bmap, the bmap is not
    required, and if it was not provided then the entire image will be copied
    to the destination file.

    The image file may either be an uncompressed raw image or a compressed
    image. Compression type is defined by the image file extension.  Supported
    types are listed by 'SUPPORTED_IMAGE_FORMATS'.

    Once an instance of 'BmapCopy' is created, all the 'bmap_*' attributes are
    initialized and available. They are read from the bmap.

    However, if bmap was not provided, this is not always the case and some of
    the 'bmap_*' attributes are not initialize by the class constructore.
    Instead, they are initialized only in the 'copy()' method. The reason for
    this is that when bmap is absent, 'BmapCopy' uses sensible fall-back values
    for the 'bmap_*' attributes assuming the entire image is "mapped". And if
    the image is compressed, it annot easily find out the image size. Thus,
    this is postponed until the 'copy()' method decompresses the image for the
    first time.

    The 'copy()' method implements the copying. You may choose whether to
    verify the SHA1 checksum while copying or not.  Note, this is done only in
    case of bmap-based copying and only if bmap contains the SHA1 checksums
    (e.g., bmap version 1.0 did not have SHA1 checksums).

    You may choose whether to synchronize the destination file after writing or
    not. To explicitly synchronize it, use the 'sync()' method.

    This class supports all the bmap format versions up version
    'SUPPORTED_BMAP_VERSION'. """

    def _initialize_sizes(self, image_size):
        """ This function is only used when the there is no bmap. It
        initializes attributes like 'bmap_blocks_cnt', 'bmap_mapped_cnt', etc.
        Normally, the values are read from the bmap file, but in this case they
        are just set to something reasonable. """

        self.bmap_image_size = image_size
        self.bmap_image_size_human = human_size(image_size)
        self.bmap_blocks_cnt = self.bmap_image_size + self.bmap_block_size - 1
        self.bmap_blocks_cnt /= self.bmap_block_size
        self.bmap_mapped_cnt = self.bmap_blocks_cnt
        self.bmap_mapped_size = self.bmap_image_size
        self.bmap_mapped_size_human = self.bmap_image_size_human


    def _parse_bmap(self):
        """ Parse the bmap file and initialize the 'bmap_*' attributes. """

        try:
            self._xml = ElementTree.parse(self._f_bmap)
        except  ElementTree.ParseError as err:
            raise Error("cannot parse the bmap file '%s' which should be a " \
                        "proper XML file: %s" % (self._bmap_path, err))

        xml = self._xml
        self.bmap_version = xml.getroot().attrib.get('version')

        # Make sure we support this version
        major = int(self.bmap_version.split('.', 1)[0])
        if major > SUPPORTED_BMAP_VERSION:
            raise Error("only bmap format version up to %d is supported, " \
                        "version %d is not supported" \
                        % (SUPPORTED_BMAP_VERSION, major))

        # Fetch interesting data from the bmap XML file
        self.bmap_block_size = int(xml.find("BlockSize").text.strip())
        self.bmap_blocks_cnt = int(xml.find("BlocksCount").text.strip())
        self.bmap_mapped_cnt = int(xml.find("MappedBlocksCount").text.strip())
        self.bmap_image_size = self.bmap_blocks_cnt * self.bmap_block_size
        self.bmap_image_size_human = human_size(self.bmap_image_size)
        self.bmap_mapped_size = self.bmap_mapped_cnt * self.bmap_block_size
        self.bmap_mapped_size_human = human_size(self.bmap_mapped_size)
        self.bmap_mapped_percent = self.bmap_mapped_cnt * 100.0
        self.bmap_mapped_percent /= self.bmap_blocks_cnt

    def _open_image_file(self):
        """ Open the image file which may be compressed or not. The compression
        type is recognized by the file extension. Supported types are defined
        by 'SUPPORTED_IMAGE_FORMATS'. """

        try:
            is_regular_file = stat.S_ISREG(os.stat(self._image_path).st_mode)
        except OSError as err:
            raise Error("cannot access image file '%s': %s" \
                        % (self._image_path, err.strerror))

        if not is_regular_file:
            raise Error("image file '%s' is not a regular file" \
                        % self._image_path)

        try:
            if self._image_path.endswith('.tar.gz') \
               or self._image_path.endswith('.tar.bz2') \
               or self._image_path.endswith('.tgz'):
                import tarfile

                tar = tarfile.open(self._image_path, 'r')
                # The tarball is supposed to contain only one single member
                members = tar.getnames()
                if len(members) > 1:
                    raise Error("the image tarball '%s' contains more than " \
                                "one file" % self._image_path)
                elif len(members) == 0:
                    raise Error("the image tarball '%s' is empty (no files)" \
                                % self._image_path)
                self._f_image = tar.extractfile(members[0])
            if self._image_path.endswith('.gz'):
                import gzip
                self._f_image = gzip.GzipFile(self._image_path, 'rb')
            elif self._image_path.endswith('.bz2'):
                import bz2
                self._f_image = bz2.BZ2File(self._image_path, 'rb')
            else:
                self._image_is_compressed = False
                self._f_image = open(self._image_path, 'rb')
        except IOError as err:
            raise Error("cannot open image file '%s': %s" \
                        % (self._image_path, err))

    def _open_destination_file(self):
        """ Open the destination file. """

        try:
            self._f_dest = open(self._dest_path, 'w+')
        except IOError as err:
            raise Error("cannot open destination file '%s': %s" \
                        % (self._dest_path, err))

    def __init__(self, image_path, dest_path, bmap_path = None):
        """ The class constructor. The parameters are:
            image_path - full path to the image which should be copied
            dest_path  - full path to the destination file to copy the image to
            bmap_path  - full path to the bmap file to use for copying """

        self._image_path = image_path
        self._dest_path  = dest_path
        self._bmap_path  = bmap_path

        self._f_dest  = None
        self._f_image = None
        self._f_bmap  = None

        self._xml = None
        self._image_is_compressed = True

        self._blocks_written = None
        self._dest_fsync_watermark = None
        self._dest_fsync_last = None

        self._batch_blocks = None
        self._batch_bytes = 1024 * 1024

        self.bmap_version = None
        self.bmap_block_size = None
        self.bmap_blocks_cnt = None
        self.bmap_mapped_cnt = None
        self.bmap_image_size = None
        self.bmap_image_size_human = None
        self.bmap_mapped_size = None
        self.bmap_mapped_size_human = None
        self.bmap_mapped_percent = None

        self._open_destination_file()
        self._open_image_file()

        if bmap_path:
            try:
                self._f_bmap = open(bmap_path, 'r')
            except IOError as err:
                raise Error("cannot open bmap file '%s': %s" \
                            % (bmap_path, err.strerror))
            self._parse_bmap()
        else:
            # There is no bmap. Initialize user-visible attributes to something
            # sensible with an assumption that we just have all blocks mapped.
            self.bmap_version = 0
            self.bmap_block_size = 4096
            self.bmap_mapped_percent = 100

            # We can initialize size-related attributes only if we the image is
            # uncompressed.
            if not self._image_is_compressed:
                image_size = os.fstat(self._f_image.fileno()).st_size
                self._initialize_sizes(image_size)

        self._batch_blocks = self._batch_bytes / self.bmap_block_size

    def __del__(self):
        """ The class destructor which closes the opened files. """

        if self._f_image:
            self._f_image.close()
        if self._f_dest:
            self._f_dest.close()
        if self._f_bmap:
            self._f_bmap.close()

    def _fsync_dest(self):
        """ Internal helper function which synchronizes the destination file if
        we wrote more than '_dest_fsync_watermark' blocks of data there. """

        size = self._dest_fsync_last + self._dest_fsync_watermark
        if self._dest_fsync_watermark and self._blocks_written >= size:
            self._dest_fsync_last = self._blocks_written
            self.sync()

    def _get_batches(self, first, last):
        """ This is a helper iterator which splits block ranges from the bmap
        file to smaller batches. Indeed, we cannot read and write entire block
        ranges from the image file, because a range can be very large. So we
        perform the I/O in batches. Batch size is defined by the
        '_batch_blocks' attribute. Thus, for each (first, last) block range,
        the iterator returns smaller (start, end, length) batch ranges, where:
          * 'start' is the starting batch block number;
          * 'last' is the ending batch block numger;
          * 'length' is the batch length in blocks (same as
             'end' - 'start' + 1). """

        batch_blocks = self._batch_blocks

        while first + batch_blocks - 1 <= last:
            yield (first, first + batch_blocks - 1, batch_blocks)
            first += batch_blocks

        batch_blocks = last - first + 1
        if batch_blocks:
            yield (first, first + batch_blocks - 1, batch_blocks)

    def _copy_data(self, first, last, sha1, verify):
        """ Internal helper function which copies the ['first'-'last'] region
        of the image file to the same region of the destination file. The
        'first' and 'last' arguments are the block numbers, not byte offsets.

        If the 'verify' argument is not 'None', calculate the SHA1 checksum for
        the region and make sure it is equivalent to 'sha1'. """

        if verify and sha1:
            hash_obj = hashlib.sha1()

        position = first * self.bmap_block_size
        self._f_image.seek(position)
        self._f_dest.seek(position)

        iterator = self._get_batches(first, last)
        for (start, end, length) in iterator:
            try:
                chunk = self._f_image.read(length * self.bmap_block_size)
            except IOError as err:
                raise Error("error while reading blocks %d-%d of the image " \
                            "file '%s': %s" \
                            % (start, end, self._image_path, err))

            if not chunk:
                raise Error("cannot read block %d, the image file '%s' is " \
                            "too short" % (start, self._image_path))

            if verify and sha1:
                hash_obj.update(chunk)

            # Synchronize the destination file if we reached the watermark
            if self._dest_fsync_watermark:
                self._fsync_dest()

            try:
                self._f_dest.write(chunk)
            except IOError as err:
                raise Error("error while writing block %d to '%s': %s" \
                            % (start, self._dest_path, err))

            self._blocks_written += length

        if verify and sha1 and hash_obj.hexdigest() != sha1:
            raise Error("checksum mismatch for blocks range %d-%d: " \
                        "calculated %s, should be %s" \
                        % (first, last, hash_obj.hexdigest(), sha1))

    def _copy_entire_image(self, sync = True):
        """ Internal helper function which copies the entire image file to the
        destination file, and only used when the bmap was not provided. The
        sync argument defines whether the destination file has to be
        synchronized upon return. """

        self._f_image.seek(0)
        self._f_dest.seek(0)
        image_size = 0

        while True:
            try:
                chunk = self._f_image.read(self._batch_bytes)
            except IOError as err:
                raise Error("cannot read %d bytes from '%s': %s" \
                            % (self._batch_bytes, self._image_path, err))

            if not chunk:
                break

            try:
                self._f_dest.write(chunk)
            except IOError as err:
                raise Error("cannot write %d bytes to '%s': %s" \
                            % (len(chunk), self._dest_path, err))

            image_size += len(chunk)

        if self._image_is_compressed:
            self._initialize_sizes(image_size)

        if sync:
            self.sync()

    def _get_block_ranges(self):
        """ This is a helper iterator that parses the bmap XML file and for
        each block range in the XML file it generates a
        ('first', 'last', 'sha1') triplet, where:
          * 'first' is the first block of the range;
          * 'last' is the last block of the range;
          * 'sha1' is the SHA1 checksum of the range ('None' is used if it is
            missing. """

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
                first = last

            if 'sha1' in xml_element.attrib:
                sha1 = xml_element.attrib['sha1']
            else:
                sha1 = None

            yield (first, last, sha1)

    def copy(self, sync = True, verify = True):
        """ Copy the image to the destination file using bmap. The sync
        argument defines whether the destination file has to be synchronized
        upon return.  The 'verify' argument defines whether the SHA1 checksum
        has to be verified while copying. """

        if not self._f_bmap:
            self._copy_entire_image(sync)
            return

        self._blocks_written = 0
        self._dest_fsync_last = 0

        # Copy the mapped blocks
        for (first, last, sha1) in self._get_block_ranges():
            self._copy_data(first, last, sha1, verify)

        # This is just a sanity check - we should have written exactly
        # 'mapped_cnt' blocks.
        if self._blocks_written != self.bmap_mapped_cnt:
            raise Error("wrote %u blocks, but should have %u - inconsistent " \
                       "bmap file" \
                       % (self._blocks_written, self.bmap_mapped_cnt))

        if sync:
            self.sync()

    def sync(self):
        """ Synchronize the destination file to make sure all the data are
        actually written to the disk. """

        try:
            self._f_dest.flush()
        except IOError as err:
            raise Error("cannot flush '%s': %s" % (self._dest_path, err))

        try:
            os.fsync(self._f_dest.fileno()),
        except OSError as err:
            raise Error("cannot synchronize '%s': %s " \
                        % (self._dest_path, err.strerror))


class BmapBdevCopy(BmapCopy):
    """ This class is a specialized version of 'BmapCopy' which copies the
    image to a block device. Unlike the base 'BmapCopy' class, this class does
    various optimizations specific to block devices, e.g., switchint to the
    'noop' I/O scheduler. """

    def _open_destination_file(self):
        """ Open the block device in exclusive mode. """

        try:
            self._f_dest = os.open(self._dest_path, os.O_WRONLY | os.O_EXCL)
        except OSError as err:
            raise Error("cannot open block device '%s' in exclusive mode: %s" \
                        % (self._dest_path, err.strerror))

        try:
            os.fstat(self._f_dest).st_mode
        except OSError as err:
            raise Error("cannot access block device '%s': %s" \
                        % (self._dest_path, err.strerror))

        # Turn the block device file descriptor into a file object
        try:
            self._f_dest = os.fdopen(self._f_dest, "wb")
        except OSError as err:
            os.close(self._f_dest)
            raise Error("cannot open block device '%s': %s" \
                        % (self._dest_path, err))

    def _tune_block_device(self):
        """" Tune the block device for better performance:
        1. Switch to the 'noop' I/O scheduler if it is available - sequential
           write to the block device becomes a lot faster comparing to CFQ.
        2. Limit the write buffering - we do not need the kernel to buffer a
           lot of the data we send to the block device, because we write
           sequentially. Limit the buffering."""

        # Construct the path to the sysfs directory of our block device
        st_rdev = os.fstat(self._f_dest.fileno()).st_rdev
        sysfs_base = "/sys/dev/block/%s:%s/" \
                      % (os.major(st_rdev), os.minor(st_rdev))

        # Switch to the 'noop' I/O scheduler
        scheduler_path = sysfs_base + "queue/scheduler"
        try:
            f_scheduler = open(scheduler_path, "w")
        except IOError:
            # If we can't find the file, no problem, this stuff is just an
            # optimization.
            f_scheduler = None

        if f_scheduler:
            try:
                f_scheduler.write("noop")
            except IOError:
                pass
            f_scheduler.close()

        # Limit the write buffering
        ratio_path = sysfs_base + "bdi/max_ratio"
        try:
            f_ratio = open(ratio_path, "w")
        except IOError:
            f_ratio = None

        if f_ratio:
            try:
                f_ratio.write("1")
            except IOError:
                pass
            f_ratio.close()

    def copy(self, sync = True, verify = True):
        """ The same as in the base class but tunes the block device for better
        performance before starting writing. Additionally, it forces block
        device synchronization from time to time in order to make sure we do
        not get stuck in 'fsync()' for too long time. The problem is that the
        kernel synchronizes block devices when the file is closed. And the
        result is that if the user interrupts us while we are copying the data,
        the program will be blocked in 'close()' waiting for the block device
        synchronization, which may last minutes for slow USB stick. This is
        very bad user experience, and we work around this effect by
        synchronizing from time to time. """

        self._tune_block_device()
        self._dest_fsync_watermark = (6 * 1024 * 1024) / self.bmap_block_size

        BmapCopy.copy(self, sync, verify)

    def __init__(self, image_path, dest_path, bmap_path = None):
        """ The same as the constructur of the 'BmapCopy' base class, but adds
        useful guard-checks specific to block devices. """

        # Call the base class construcor first
        BmapCopy.__init__(self, image_path, dest_path, bmap_path)

        # If the image size is known (i.e., it is not compressed) - check that
        # itfits the block device.
        if self.bmap_image_size:
            try:
                bdev_size = os.lseek(self._f_dest.fileno(), 0, os.SEEK_END)
                os.lseek(self._f_dest.fileno(), 0, os.SEEK_SET)
            except OSError as err:
                raise Error("cannot seed block device '%s': %s " \
                            % (self._dest_path, err.strerror))

            if bdev_size < self.bmap_image_size:
                raise Error("the image file '%s' has size %s and it will not " \
                            "fit the block device '%s' which has %s capacity" \
                            % (self._image_path, self.bmap_image_size_human,
                               self._dest_path, human_size(bdev_size)))
