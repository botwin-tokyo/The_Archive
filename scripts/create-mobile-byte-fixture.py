#!/usr/bin/env python3
"""Create a deterministic, sparse test input with markers across integer boundaries.

Only creates a new explicitly named file (O_EXCL/O_NOFOLLOW). The default is
4 GiB + 123 bytes. This is an input generator, not transfer qualification.
The digest reads every logical byte with bounded memory. Generated files stay
outside Git and must only be used in disposable test roots.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

BLOCK = 1024 * 1024
DEFAULT_SIZE = 2**32 + 123


def markers(size):
    """Distinct data around 2/4 GiB detects truncation and wrong-offset reads."""
    positions = {0, max(0, size - 4096)}
    positions.update(n for n in (2**31 - 2048, 2**31 + 17, 2**32 - 2048, 2**32 + 17) if n < size)
    return [(position, min(1024, size - position)) for position in sorted(positions) if size]


def create(path, size):
    if size < 0 or size > 2**63 - 1:
        raise ValueError("Size must fit a nonnegative signed 64-bit integer")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.ftruncate(fd, size)
        for position, length in markers(size):
            block = hashlib.shake_256(f"archive-large-byte-fixture-v1:{position}".encode()).digest(length)
            if os.pwrite(fd, block, position) != len(block):
                raise OSError("Short fixture marker write")
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        read = 0
        while True:
            data = os.read(fd, BLOCK)
            if not data:
                break
            digest.update(data)
            read += len(data)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size or read != size:
            raise OSError("Fixture length changed during verification")
        os.fchmod(fd, 0o400)
        os.fsync(fd)
        return {"schemaVersion": 1, "algorithm": "archive_sparse_boundary_markers_v1",
                "bytes": str(size), "sha256": digest.hexdigest(),
                "allocatedBytes": str(info.st_blocks * 512),
                "markers": [{"offset": str(offset), "bytes": str(length)} for offset, length in markers(size)],
                "logicalBytesRead": str(read), "bufferBytes": BLOCK,
                "qualification": "generated_input_only"}
    finally:
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New file in a disposable test directory; never overwrites")
    parser.add_argument("--bytes", type=int, default=DEFAULT_SIZE)
    args = parser.parse_args()
    print(json.dumps(create(args.output, args.bytes), indent=2))
