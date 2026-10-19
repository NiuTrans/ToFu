"""Explicitly owned native HTML projection over pinned sealed release bytes.

Entry NativeHtmlProjector. The caller owns selection and close; imports perform
no native loading. Each synchronous call leases code through UTF-8 decoding.
Close refuses new work and joins every lease before unload and descriptor release.
No parser fallback, process-global instance, worker, or storage authority.
"""
import json
from pathlib import Path
import threading

from lib.agent_core.native_html_projection_contract import SPEC
from .generated_native_html_profile import PROFILE
from .generated_native_html_cffi import ffi

_SIZE_MAX = (1 << (8 * ffi.sizeof('size_t'))) - 1
_load_library = ffi.dlopen
_close_library = ffi.dlclose


class NativeHtmlProjectionError(RuntimeError):
    """Content-free native status or lifetime refusal; never implicit fallback."""


class NativeHtmlProjector:
    @classmethod
    def from_archive(cls, *, archive, sha256, application_version, binary_bytes):
        from lib.agent_core.native_artifact import seal_html_projection_archive
        instance = cls.__new__(cls)
        instance._initialize(lambda: seal_html_projection_archive(
            archive, sha256, application_version, binary_bytes))
        return instance

    @classmethod
    def from_shared_archive(cls, *, image):
        from lib.agent_core.native_image import NativeArchiveImage
        from lib.agent_core.native_binary import NativeConfigurationError
        if not isinstance(image, NativeArchiveImage):
            raise NativeConfigurationError('native HTML requires an owned archive image')
        instance = cls.__new__(cls)
        instance._initialize(lambda: image.duplicate_library('optional_html_projection'))
        return instance

    @classmethod
    def from_library(cls, *, library, sha256, binary_bytes):
        """Use the trusted pin retained by the installation owner, not a new hash."""
        from lib.agent_core.native_binary import NativeConfigurationError, _seal
        if (not isinstance(library, Path) or not library.is_absolute()
                or not isinstance(sha256, str) or len(sha256) != 64
                or any(ch not in '0123456789abcdef' for ch in sha256)
                or type(binary_bytes) is not int or binary_bytes < 4):
            raise NativeConfigurationError('native HTML library requires pinned bytes and bounds')
        instance = cls.__new__(cls)
        instance._initialize(lambda: _seal(library, sha256, binary_bytes, ffi=ffi))
        return instance

    def _initialize(self, acquire):
        self._condition = threading.Condition()
        self._active = 0
        self._state = 'open'
        self._library = None
        self._image = acquire()
        try:
            self._library = _load_library(f'/proc/self/fd/{self._image.fileno()}')
            version = self._library.tofu_html_projection_abi_version
            if version() != SPEC['abi_version']:
                raise NativeHtmlProjectionError('native HTML ABI version differs')
            profile = self._library.tofu_html_projection_profile
            self.profile = json.loads(self._receive(lambda sink: profile(sink, ffi.NULL)))
            if (not isinstance(self.profile, dict)
                    or any(self.profile.get(key) != value for key, value in PROFILE.items())):
                raise NativeHtmlProjectionError('native HTML tokenizer profile differs')
            self._project = self._library.tofu_html_projection_project
        except BaseException as primary:
            # If unload fails, retain its descriptor and code; do not hide either
            # failure behind a subsequent close or silently discard ownership.
            try:
                self._unload()
                self._image.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup('native HTML initialization and cleanup failed',
                                         [primary, cleanup]) from None
            raise

    @staticmethod
    def _consume(pointer, length):
        return bytes(ffi.buffer(pointer, length)).decode('utf-8')

    def _receive(self, invoke):
        values, failures = [], []

        @ffi.callback('tofu_html_sink', error=1)
        def sink(_context, pointer, length):
            try:
                if values or failures:
                    raise NativeHtmlProjectionError('native HTML sink invoked more than once')
                values.append(self._consume(pointer, length))
                return 0
            except BaseException as error:
                failures.append(error)
                return 1

        status = invoke(sink)
        if failures:
            raise failures[0]
        if status != SPEC['status']['OK']:
            name = next((name for name, code in SPEC['status'].items() if code == status), 'UNKNOWN')
            raise NativeHtmlProjectionError('native HTML projection failed: ' + name)
        if len(values) != 1:
            raise NativeHtmlProjectionError('native HTML output missing')
        return values[0]

    def project(self, text, *, projection_budget=None):
        if type(text) is not str:
            raise TypeError('HTML input must be a string')
        if projection_budget is None:
            projection_budget = _SIZE_MAX
        if type(projection_budget) is not int or not 0 <= projection_budget <= _SIZE_MAX:
            raise ValueError('projection_budget must be an unsigned size_t or None')
        with self._condition:
            if self._state != 'open':
                raise NativeHtmlProjectionError('native HTML projector closed')
            self._active += 1
        try:
            raw = text.encode('utf-8')
            return self._receive(lambda sink: self._project(
                raw, len(raw), projection_budget, sink, ffi.NULL))
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def _unload(self):
        self._project = None
        if self._library is not None:
            _close_library(self._library)
            self._library = None

    def close(self):
        with self._condition:
            while self._state == 'closing':
                self._condition.wait()
            if self._state == 'closed':
                return
            if self._state == 'failed':
                raise NativeHtmlProjectionError('native HTML teardown failed; resources retained')
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
            self._unload()
            self._image.close()
        except BaseException:
            with self._condition:
                self._state = 'failed'
                self._condition.notify_all()
            raise
        with self._condition:
            self._state = 'closed'
            self._condition.notify_all()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
