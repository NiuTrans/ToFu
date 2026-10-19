"""One loop-owned immutable release image, independent of client principals.

Archive and installed-executable owners lazily seal and self-check once. Each
client receives its own descriptor, not another image allocation. Close joins
acquisition and prevents future borrowing; existing client descriptors retain
their own lifetime. No client registry, task state, worker or timer is added.
"""
import asyncio
import os
from pathlib import Path
import threading

from lib.agent_core.native_process import Budget, NativeConfigurationError, _joined

from lib.log import get_logger

logger = get_logger(__name__)


class _NativeImage:
    def __init__(self, *, source: Path, sha256: str, application_version: str, budget: Budget):
        if (not isinstance(source, Path) or not source.is_absolute() or not isinstance(budget, Budget)
                or not isinstance(sha256, str) or len(sha256) != 64
                or any(ch not in '0123456789abcdef' for ch in sha256)
                or not isinstance(application_version, str) or not application_version):
            raise NativeConfigurationError('shared native image requires pinned source and bounds')
        self.sha256, self.application_version = sha256, application_version
        self.budget = budget
        self._loop = None
        self._image = None
        self._closing = None
        self._closed = False

    def _bind_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise NativeConfigurationError('shared native image belongs to another event loop')
        self._loop = loop

    async def _load(self):
        worker = asyncio.get_running_loop().run_in_executor(None, self._seal)
        return await _joined(worker)

    @property
    def admission_started(self):
        return self._image is not None

    def prepare(self):
        """Start immutable admission on the owning loop; borrowing still awaits it."""
        self._bind_loop()
        if self._closed:raise NativeConfigurationError('shared native image is closed')
        if self._image is None:
            self._image=asyncio.create_task(self._load())
            # Preparation has no borrower yet. Observe a failed task without
            # replacing it; duplicate still raises that exact admission failure.
            self._image.add_done_callback(lambda task: None if task.cancelled() else task.exception())

    async def duplicate(self, binary_bytes: int):
        self._bind_loop()
        if self._closed or type(binary_bytes) is not int or binary_bytes <= 0:
            raise NativeConfigurationError('shared native image is closed or client bound is invalid')
        self.prepare()
        image = await asyncio.shield(self._image)
        if self._closed:
            raise NativeConfigurationError('shared native image is closing')
        if os.fstat(image.fileno()).st_size > binary_bytes:
            raise NativeConfigurationError('shared native image exceeds client binary bound')
        # dup shares the sealed kernel object while keeping close ownership separate.
        descriptor = os.dup(image.fileno())
        try:
            return os.fdopen(descriptor, 'rb')
        except BaseException:
            os.close(descriptor)
            raise

    async def _close(self):
        if self._image is None:
            return
        try:
            image = await _joined(self._image)
        except Exception as diagnostic_error:
            logger.debug('_close failure handled: %s', type(diagnostic_error).__name__)
            return  # Failed admission owns no image; its error remains on acquisition.
        image.close()

    async def close(self):
        self._bind_loop()
        self._closed = True
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await _joined(self._closing)


class NativeArchiveImage(_NativeImage):
    """One full release archive, including any declared optional libraries."""
    def __init__(self, *, archive: Path, sha256: str, application_version: str, budget: Budget):
        super().__init__(source=archive, sha256=sha256,
                         application_version=application_version, budget=budget)
        self.archive = archive
        # Optional ABIs may name the same library in the generated contract.
        # The synchronous preparation lane owns acquisition; shutdown joins it
        # off-loop. No optional bytes or code load until the first library user.
        self._library_lock = threading.Lock()
        self._libraries = {}

    def duplicate_library(self, extension_name: str):
        """Borrow sealed bytes for one declared ABI on the preparation worker.

        A successful full inspection can serve another ABI on the same member,
        but never grants an extension absent from that checked archive. Borrowers
        own descriptors and independently bind/check/unload their ABI handles.
        """
        from .native_artifact import SPEC, _inspect_library_archive
        if extension_name not in ('optional_token_count', 'optional_flow', 'optional_html_projection'):
            raise NativeConfigurationError('shared native library extension unsupported')
        extension = SPEC[extension_name]
        with self._library_lock:
            if self._closed:
                raise NativeConfigurationError('shared native image is closed')
            member = extension['member']
            if member not in self._libraries:
                manifest, image = _inspect_library_archive(
                    self.archive, self.sha256, self.application_version,
                    self.budget.binary_bytes, extension)
                if self._closed:
                    image.close()
                    raise NativeConfigurationError('shared native image is closing')
                declared = frozenset(SPEC[name]['contract_member'] for name in
                    ('optional_token_count', 'optional_flow', 'optional_html_projection')
                    if SPEC[name]['contract_member'] in manifest['files'])
                self._libraries[member] = (declared, image)
            declared, image = self._libraries[member]
            if extension['contract_member'] not in declared:
                raise NativeConfigurationError('native archive selected library extension missing')
            descriptor = os.dup(image.fileno())
            try:
                return os.fdopen(descriptor, 'rb', buffering=0)
            except BaseException:
                os.close(descriptor)
                raise

    def _close_libraries(self):
        with self._library_lock:
            failures = []
            for member, (_, image) in tuple(self._libraries.items()):
                try:
                    image.close()
                    del self._libraries[member]
                except BaseException as error:
                    failures.append(error)
            if failures:
                raise BaseExceptionGroup('shared native libraries did not close cleanly', failures)

    async def _close(self):
        failures = []
        try:
            await super()._close()
        except BaseException as error:
            failures.append(error)
        if self._libraries or self._library_lock.locked():
            try:
                await _joined(asyncio.to_thread(self._close_libraries))
            except BaseException as error:
                failures.append(error)
        if failures:
            raise BaseExceptionGroup('shared native archive did not close cleanly', failures)

    def _seal(self):
        from lib.agent_core.native_artifact import seal_archive
        return seal_archive(self.archive, self.sha256, self.application_version,
                            self.budget.binary_bytes, self.budget.shutdown_seconds,
                            self.budget.frame_bytes)


class NativeExecutableImage(_NativeImage):
    """Pinned executable and optional separately pinned pure token library."""
    def __init__(self, *, binary: Path, sha256: str, application_version: str, budget: Budget,
                 token_library: Path | None = None, token_library_sha256: str | None = None):
        super().__init__(source=binary, sha256=sha256,
                         application_version=application_version, budget=budget)
        self.binary = binary
        if token_library is not None or token_library_sha256 is not None:
            if (not isinstance(token_library, Path) or not token_library.is_absolute()
                    or not isinstance(token_library_sha256, str) or len(token_library_sha256) != 64
                    or any(ch not in '0123456789abcdef' for ch in token_library_sha256)):
                raise NativeConfigurationError('installed token library requires its own path and pin')
        self.token_library, self.token_library_sha256 = token_library, token_library_sha256

    def _seal(self):
        from lib.agent_core.native_artifact import seal_executable
        return seal_executable(self.binary, self.sha256, self.application_version,
                               self.budget.binary_bytes, self.budget.shutdown_seconds,
                               self.budget.frame_bytes)
