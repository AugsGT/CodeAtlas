# This file handles streaming telemetry records in a way that ensures durability even if the process is killed.
# Each record is written immediately to the output file, so any completed records before the kill are saved.

import struct

_HEADER = struct.Struct(">I")

def append_record(fileobj, data: bytes) -> None:
    # Write the length of the data followed by the data itself
    fileobj.write(_HEADER.pack(len(data)))
    fileobj.write(data)
    fileobj.flush()
    try:
        import os
        os.fsync(fileobj.fileno())
    except (OSError, ValueError):
        pass  # Best-effort durability; not fatal if fsync is not supported

def read_records(fileobj):
    while True:
        header = fileobj.read(_HEADER.size)
        if not header:
            return
        if len(header) < _HEADER.size:
            return  # Truncated record (process killed mid-write) - stop, don't error
        (length,) = _HEADER.unpack(header)
        data = fileobj.read(length)
        if len(data) < length:
            return  # Truncated record - same as above
        yield data