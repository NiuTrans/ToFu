"""Anonymous overflow bodies shared by v1/v2 storage transports.

Owns only reconstructible frame bytes. A received body is decoded inside this
context; callers must return owned decoded values. Temporary files are private,
never durable authority state, and close on read/decode/checksum failures.
"""
from contextlib import contextmanager
import mmap
import tempfile


@contextmanager
def received_frame_body(read_exact, size: int, reservation=None):
    if not getattr(reservation, 'spilled', False):
        yield read_exact(size)
        return
    with tempfile.TemporaryFile(mode='w+b') as file:
        remaining = size
        chunk_bytes = getattr(reservation, 'chunk_bytes', 64 * 1024)
        while remaining:
            count = min(remaining, chunk_bytes)
            data = read_exact(count)
            if len(data) != count:
                raise EOFError('short storage overflow frame read')
            file.write(data)
            remaining -= count
        file.flush()
        with mmap.mmap(file.fileno(), size, access=mmap.ACCESS_READ) as body:
            yield body
