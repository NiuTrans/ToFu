"""Isolated complete image decoding; no application storage or admission quota.

Source/frozen main owns standard streams and the caller's process lifetime.
inspect_image verifies supported formats and every frame. Pillow policy changes
are confined to this worker, never the threaded application.
"""
from __future__ import annotations
import hashlib
import io
import json
import os
import sys

WORKER_FLAG = '--artifact-image-worker'
MIMES = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp', 'GIF': 'image/gif'}


def inspect_image(data):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    if not data:
        raise ValueError('Image missing')
    with Image.open(io.BytesIO(data)) as image:
        mime = MIMES.get(image.format)
        width, height = image.size
        frames = getattr(image, 'n_frames', 1)
        if not mime or width <= 0 or height <= 0 or frames <= 0:
            raise ValueError('Unsupported image format or geometry')
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        for frame in range(frames):
            image.seek(frame)
            image.load()
    return {'mime': mime, 'width': width, 'height': height,
            'sizeBytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def binary_stream(name, mode):
    stream = getattr(sys, name)
    if stream is not None:
        return stream.buffer
    # Windowed frozen executables omit Python stream objects. Popen still
    # provides inherited Windows standard handles for this internal role.
    if os.name != 'nt':
        return os.fdopen(os.dup(0 if name == 'stdin' else 1), mode)
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel.GetStdHandle(-10 if name == 'stdin' else -11)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError('Image worker inherited handle unavailable')
    flag = os.O_RDONLY if mode == 'rb' else os.O_WRONLY
    return os.fdopen(msvcrt.open_osfhandle(int(handle), flag | os.O_BINARY), mode)


def main(arguments=None):
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) != 1:
        return 2
    try:
        from lib.paper.builds.pdf_validation_limits import bind_parent_lifetime
        bind_parent_lifetime(int(args[0]))
        result = {'ok': True, 'info': inspect_image(binary_stream('stdin', 'rb').read())}
    except Exception as diagnostic_error:
        print('main failed:', type(diagnostic_error).__name__, file=sys.stderr)
        result = {'ok': False, 'error': 'Invalid or undecodable PNG/JPEG/WebP/GIF'}
    output = binary_stream('stdout', 'wb')
    output.write(json.dumps(result, separators=(',', ':')).encode('utf-8'))
    output.flush()
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
