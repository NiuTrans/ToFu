"""Explicit task/runtime native image ownership and canonical task cancellation.

OwnedStreamScope authorizes one owner/root, joins active calls before closing,
and owns a lazy image or borrows explicit RuntimeStreamImage code. Neither
lifetime adds a resident child or parser pool.
Artifact admission and parent AST/reference work remain cooperative. Production
composition lives in native_runtime; installed grammar authority and capacity
admission remain separate.
"""
from contextlib import contextmanager, nullcontext
import os
import sys
from threading import Condition, Event, Lock, get_ident

from lib.identity import require_user_id
from ._native_stream import NativeStreamScanner, _StreamImage, DiscoveryCancelled


class _Cancellation:
    def __init__(self, closing, task, caller, runtime=None):
        self.closing, self.task, self.caller, self.runtime = closing, task, caller, runtime

    def is_set(self):
        return (self.closing.is_set()
                or (self.task is not None and self.task.is_set())
                or (self.caller is not None and self.caller.is_set())
                or (self.runtime is not None and self.runtime.is_set()))


class _ImageLifetime:
    """One explicit image lifetime; active calls own it through child retirement."""
    def __init__(self, binary, expected_sha256, image_factory=None):
        self.binary, self.expected_sha256 = binary, expected_sha256
        self._image_factory = image_factory
        self._closing = Event()
        self._condition = Condition()
        self._image_lock = Lock()
        self._image = None
        self._image_bytes = 0
        self._active_calls = {}
        self._disposing = self._closed = False

    def _image_for_active_call(self):
        # Active-call admission keeps this image alive through child retirement.
        # A separate lock leaves close/cancellation signalling independent of a
        # slow artifact read. The read itself is cooperative, as in one-shot use.
        with self._image_lock:
            if self._image is None:
                self._image = (self._image_factory() if self._image_factory is not None
                               else _StreamImage(self.binary, self.expected_sha256))
            if self._image_bytes == 0:
                with self._condition:
                    self._image_bytes = os.fstat(self._image._image.fileno()).st_size
            return self._image

    @contextmanager
    def _active_call(self):
        lease = object()
        try:
            with self._condition:
                if self._closing.is_set() or self._closed:
                    raise RuntimeError('native repository execution is closed')
                self._active_calls[lease] = get_ident()
            yield
        finally:
            primary = sys.exc_info()[1]
            interrupted = None
            while True:
                try:
                    with self._condition:
                        self._active_calls.pop(lease, None)
                        self._condition.notify_all()
                    break
                except KeyboardInterrupt as error:
                    interrupted = error
            if interrupted is not None:
                if primary is None:
                    raise interrupted
                if hasattr(primary, 'add_note'):
                    primary.add_note('native scan lease retirement interrupted; completed before return')

    def _close_image(self):
        errors = []
        with self._condition:
            if get_ident() in self._active_calls.values():
                raise RuntimeError('cannot close native execution from its active scan')
            self._closing.set()
            while self._active_calls or self._disposing:
                try:
                    self._condition.wait()
                except KeyboardInterrupt as error:
                    errors.append(error)
            if self._closed:
                if errors:
                    raise errors[0]
                return
            self._disposing = True
        try:
            if self._image is not None:
                # This owner retries interrupted image close and reports the
                # interruption only after the descriptor is retired.
                self._image.__exit__(None, None, None)
        except BaseException as error:
            errors.append(error)
        finally:
            with self._condition:
                if self._image is None or self._image._image.closed:
                    self._image = None
                    self._image_bytes = 0
                    self._closed = True
                self._disposing = False
                self._condition.notify_all()
        if errors:
            primary = errors[0]
            if hasattr(primary, 'add_note') and len(errors) > 1:
                primary.add_note('native execution cleanup also raised: ' +
                                 ', '.join(type(error).__name__ for error in errors[1:]))
            raise primary


class RuntimeStreamImage(_ImageLifetime):
    """Lazy immutable code owned by one runtime, with no repository/user state.

    Task scopes borrow only during scans. Shutdown cancels/drains all borrowers
    before image disposal; idle tasks cannot keep shutdown waiting. Callers must
    explicitly close this owner after their task runtime drains, even on failure.
    This owner retains no parser process, grammar mapping or repository cache.
    """
    def __init__(self, binary, *, expected_sha256, image_factory=None):
        super().__init__(binary, expected_sha256, image_factory)

    @classmethod
    def installed(cls):
        def open_image():
            from tofu_repo_kernel import open_stream_process
            return open_stream_process()
        return cls(None, expected_sha256=None, image_factory=open_image)

    def retained_image_bytes(self):
        with self._condition:
            return self._image_bytes

    def close(self):
        self._close_image()

    def __enter__(self):
        return self

    def __exit__(self, _kind, primary, _traceback):
        try:
            self.close()
        except BaseException as cleanup:
            if primary is None:
                raise
            if hasattr(primary, 'add_note'):
                primary.add_note('native runtime image close: ' + type(cleanup).__name__)


class OwnedStreamScope(_ImageLifetime):
    def __init__(self, root, owner, binary, grammar_directory, *, expected_sha256, limits,
                 cancellation=None, runtime_image=None, strict=True):
        if type(strict) is not bool:
            raise ValueError('native repository grammar policy must be boolean')
        self.owner = require_user_id(owner, context='native repository execution owner')
        self.root = os.path.abspath(os.fsdecode(root))
        if runtime_image is not None:
            if (not isinstance(runtime_image, RuntimeStreamImage)
                    or (os.fspath(runtime_image.binary) if runtime_image.binary is not None else None)
                    != (os.fspath(binary) if binary is not None else None)
                    or runtime_image.expected_sha256 != expected_sha256):
                raise ValueError('native runtime image does not match the task artifact')
        if binary is None and runtime_image is None:
            raise ValueError('native execution requires an artifact or runtime image')
        super().__init__(binary, expected_sha256)
        self._runtime_image = runtime_image
        self._task_cancellation = cancellation
        self._scanner = NativeStreamScanner(binary, grammar_directory,
            expected_sha256=expected_sha256, limits=limits, strict=strict, image_factory=self._borrow_image)

    def _borrow_image(self):
        return nullcontext(self._image_for_active_call())

    def _authorize(self, owner):
        if require_user_id(owner, context='native repository caller') != self.owner:
            raise PermissionError('native repository stream belongs to another owner')

    @property
    def calls(self):
        return self._scanner.calls

    @property
    def discovery_calls(self):
        return self._scanner.discovery_calls

    def retained_image_bytes(self, owner):
        """Bytes owned by this task; a borrowed image is charged to its runtime."""
        self._authorize(owner)
        with self._condition:
            return self._image_bytes

    def _image_for_active_call(self):
        if self._runtime_image is not None:
            return self._runtime_image._image_for_active_call()
        return super()._image_for_active_call()

    def scan(self, owner, **options):
        self._authorize(owner)
        runtime = self._runtime_image
        cancellation = _Cancellation(self._closing, self._task_cancellation,
                                     options.pop('cancellation', None),
                                     runtime._closing if runtime is not None else None)
        with self._active_call(), runtime._active_call() if runtime is not None else nullcontext():
            result = self._scanner(self.root, cancellation=cancellation, **options)
            with self._condition:
                if cancellation.is_set():
                    raise DiscoveryCancelled('native repository execution cancelled before return')
            return result

    def close(self, owner):
        self._authorize(owner)
        try:
            self._close_image()
        finally:
            if self._closed:
                self._scanner._image_factory = None

    def __enter__(self):
        return self

    def __exit__(self, _kind, primary, _traceback):
        try:
            self.close(self.owner)
        except BaseException as cleanup:
            if primary is None:
                raise
            if hasattr(primary, 'add_note'):
                primary.add_note('native execution close: ' + type(cleanup).__name__)
