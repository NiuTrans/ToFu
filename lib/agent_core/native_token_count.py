"""Pure token counts from a pinned sealed archive or installed library.

NativeTokenCounter owns its descriptor, dlopen mapping and lazy encoder handles.
Calls borrow local UTF-8 buffers, release the GIL in ctypes, and retain a Python
lease until return. close rejects new calls, joins leases, closes native handles,
then unloads code before releasing the descriptor. No import-time load, fallback,
model/account/store authority, thread pool or prompt cache is introduced.
"""
import _ctypes
import ctypes
from array import array
from pathlib import Path
import threading

from .native_artifact import seal_token_count_archive
from .native_token_count_contract import TOKEN_COUNT_SPEC as SPEC


class NativeTokenCountError(RuntimeError):
    """Fixed content-free ABI/lifecycle failure."""


class _TextSpan(ctypes.Structure):
    _fields_ = [('data', ctypes.c_void_p), ('length', ctypes.c_size_t)]


class NativeTokenCounter:
    def __init__(self, *, archive, sha256, application_version, binary_bytes):
        self._initialize(lambda: seal_token_count_archive(
            archive, sha256, application_version, binary_bytes))

    @classmethod
    def from_shared_archive(cls, *, image):
        """Bind this ABI over an independent loan from the owned release image."""
        from .native_image import NativeArchiveImage
        from .native_process import NativeConfigurationError
        if not isinstance(image, NativeArchiveImage):
            raise NativeConfigurationError('native token count requires an owned archive image')
        instance = cls.__new__(cls)
        instance._initialize(lambda: image.duplicate_library('optional_token_count'))
        return instance

    @classmethod
    def from_library(cls, *, library, sha256, binary_bytes):
        """Use a caller-pinned installed pure library; ABI/vocabulary remain checked.

        The digest comes from verified installation, never the current path.
        Application version is validated by that distribution installation;
        this pure ABI has its own version and exact embedded vocabulary pins.
        """
        from .native_process import NativeConfigurationError, _seal
        if (not isinstance(library, Path) or not library.is_absolute()
                or not isinstance(sha256, str) or len(sha256) != 64
                or any(ch not in '0123456789abcdef' for ch in sha256)
                or type(binary_bytes) is not int or binary_bytes < 4):
            raise NativeConfigurationError('native token library requires pinned regular bytes and bounds')
        instance = cls.__new__(cls)
        instance._initialize(lambda: _seal(library, sha256, binary_bytes))
        return instance

    def _initialize(self, acquire_image):
        self._condition = threading.Condition()
        self._active = 0
        self._state = 'open'
        self._fault = None
        self._handles = {}
        self._library = None
        self._image = acquire_image()
        try:
            self._library = ctypes.CDLL(f'/proc/self/fd/{self._image.fileno()}')
            version = self._library.tofu_token_count_abi_version
            version.argtypes = []
            version.restype = ctypes.c_uint32
            if version() != SPEC['abi_version']:
                raise NativeTokenCountError('native token ABI version differs')
            self._open = self._library.tofu_token_count_open
            self._open.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64)]
            self._open.restype = ctypes.c_uint32
            self._batch = self._library.tofu_token_count_batch
            self._batch.argtypes = [ctypes.c_uint64, ctypes.POINTER(_TextSpan), ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t]
            self._batch.restype = ctypes.c_uint32
            self._close = self._library.tofu_token_count_close
            self._close.argtypes = [ctypes.c_uint64]
            self._close.restype = ctypes.c_uint32
        except BaseException:
            self._unload()
            self._image.close()
            raise

    def _unload(self):
        self._open = self._batch = self._close = None
        if self._library is not None:
            handle = self._library._handle
            _ctypes.dlclose(handle)
            self._library._handle = 0
            self._library = None

    @staticmethod
    def _check(status):
        if status != SPEC['status']['OK']:
            name = next((name for name, code in SPEC['status'].items() if code == status), 'UNKNOWN')
            raise NativeTokenCountError('native token count failed: ' + name)

    def count_batch(self, texts, *, encoding):
        if type(texts) not in (list, tuple):
            raise TypeError('texts must be a list or tuple of strings')
        texts = tuple(texts)
        if any(type(text) is not str for text in texts):
            raise TypeError('texts must contain strings')
        choices = {'cl100k_base': SPEC['encodings']['CL100K'],
                   'o200k_base': SPEC['encodings']['O200K']}
        if type(encoding) is not str or encoding not in choices:
            raise ValueError('unsupported native token encoding')
        with self._condition:
            if self._state != 'open':
                raise NativeTokenCountError('native token counter closed')
            handle = self._handles.get(encoding)
            if handle is None:
                output = ctypes.c_uint64()
                self._check(self._open(choices[encoding], ctypes.byref(output)))
                handle = output.value
                self._handles[encoding] = handle
            self._active += 1
        try:
            # Bytes own the immutable storage; c_char_p exposes it without another
            # text-sized copy. Span/output arrays remain call-local and alive.
            buffers = [text.encode('utf-8') for text in texts]
            pointers = [ctypes.c_char_p(raw) for raw in buffers]
            word_format = 'Q' if ctypes.sizeof(ctypes.c_size_t) == 8 else 'I'
            descriptors = array(word_format)
            for pointer, raw in zip(pointers, buffers):
                descriptors.extend((ctypes.cast(pointer, ctypes.c_void_p).value, len(raw)))
            spans = ctypes.cast(descriptors.buffer_info()[0], ctypes.POINTER(_TextSpan))
            output = array('Q', [0]) * len(texts)
            target = ctypes.cast(output.buffer_info()[0], ctypes.POINTER(ctypes.c_uint64))
            self._check(self._batch(handle, spans, len(texts), target, len(texts)))
            return output.tolist()
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def close(self):
        with self._condition:
            while self._state == 'closing':
                self._condition.wait()
            if self._state == 'closed':
                return
            if self._state == 'failed':
                raise NativeTokenCountError(self._fault)
            self._state = 'closing'
            self._condition.notify_all()
            try:
                while self._active:
                    self._condition.wait()
            except BaseException:
                self._state = 'open'
                self._condition.notify_all()
                raise
        try:
            for handle in self._handles.values():
                self._check(self._close(handle))
            self._handles.clear()
            self._unload()
            self._image.close()
        except BaseException:
            with self._condition:
                self._state = 'failed'
                self._fault = 'native token counter teardown failed; resources retained'
                self._condition.notify_all()
            raise
        with self._condition:
            self._state = 'closed'
            self._condition.notify_all()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
