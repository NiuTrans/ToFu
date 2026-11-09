"""Lazy imports load the application source this process pinned near boot.

Responsibility: a long-running server keeps the modules it imported at boot in
memory while merges keep rewriting the checkout beneath it. Most imports in
this codebase are function-local, so a module first imported days later used
to compile the checkout's newer source against boot-era modules: one commit
that changed a provider and its lazily imported consumer split across two
revisions inside one process. On 2026-10-30, 26 hours after boot, the first L2
compaction summary compiled the post-merge
``lib/tasks_pkg/compaction/_summary_accounting.py``; it asked the boot-loaded
``lib.project_dispatch.admission`` for ``stop_on_authority_failure`` (added by
d95ceee52), so every later summary in that worker crashed its task.

Entry point: ``PinnedImportSource(roots).install()`` places one meta-path
finder just before PathFinder. A background thread pins the modules directly
inside each root and the tree of every top-level package of those roots that
this process imports: packages already imported at installation, any other
package at its first import. The first read of a file wins; a module imported
before the thread reached it was loaded from that same checkout state. Later
imports of a pinned module compile the pinned bytes, reuse bytecode only when
its header names the pinned revision, and write bytecode only while the
checkout still holds that revision. ``importlib.reload`` deliberately reads the
checkout (the vendored MCP registry hot reload depends on it). Files added
after pinning load from the checkout; modules deleted after pinning still
import from their pins.

Pins are zlib-compressed bytes in memory. Only regular packages (directories
holding ``__init__.py``) are entered: runtime data directories inside packages
hold millions of files and are never listed. Dependencies: standard library
and ``lib.log``.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
import importlib.machinery
import importlib.util
import hashlib
import itertools
import linecache
import os
import sys
import threading
import time
import zlib

from lib.log import get_logger

logger = get_logger(__name__)

_PACKAGE_INIT = '__init__.py'


def source_identity(sources: Iterable[tuple[str, bytes]]) -> dict:
    """Hash path-ordered executable sources for preparation and retained pins."""
    identity = hashlib.sha256()
    count = 0
    for path, content in sources:
        name = path.encode()
        identity.update(len(name).to_bytes(8, 'little'))
        identity.update(name)
        identity.update(len(content).to_bytes(8, 'little'))
        identity.update(content)
        count += 1
    return {'sourceSha256': identity.hexdigest(), 'sourceFiles': count}


class _Pin:
    """One source file as this process first read it."""

    __slots__ = ('mtime', 'mtime_ns', 'size', '_compressed')

    def __init__(self, status: os.stat_result, source: bytes) -> None:
        # The float CPython's own path_stats reports, so bytecode headers
        # compare exactly as they would against the file itself.
        self.mtime = status.st_mtime
        self.mtime_ns = status.st_mtime_ns
        self.size = status.st_size
        self._compressed = zlib.compress(source)

    @property
    def compressed_size(self) -> int:
        return len(self._compressed)

    def source(self) -> bytes:
        return zlib.decompress(self._compressed)

    def on_disk(self, path: str) -> bool:
        try:
            status = os.stat(path)
        except OSError:
            # Deleted or unreadable: the checkout no longer holds this revision.
            return False
        return (status.st_mtime_ns, status.st_size) == (self.mtime_ns, self.size)


def _read_pin(path: str) -> _Pin | None:
    """Pin ``path``; None when it is unreadable or was rewritten while read."""
    try:
        with open(path, 'rb') as handle:
            before = os.fstat(handle.fileno())
            source = handle.read()
            after = os.fstat(handle.fileno())
    except OSError:
        # Removed, unreadable or a directory named *.py: the caller counts it
        # and the module keeps loading from the checkout.
        return None
    if ((before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size)
            or len(source) != after.st_size):
        return None
    return _Pin(after, source)


def _importable_sources(directory: str, *, recurse: bool, ignore_errors: bool = True) -> Iterator[str]:
    """Yield the ``.py`` files the import system can load from ``directory``.

    With ``recurse``, a subdirectory is entered only when it holds
    ``__init__.py``, so data kept inside a package is never listed.
    """
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                names = {entry.name for entry in entries}
        except OSError:
            if not ignore_errors:
                raise
            # Removed or unreadable since it was found: it contributes no pins.
            continue
        for name in names:
            stem, suffix = os.path.splitext(name)
            if suffix == '.py':
                # An extension module of the same name takes import precedence.
                if stem.isidentifier() and not any(
                        stem + extension in names
                        for extension in importlib.machinery.EXTENSION_SUFFIXES):
                    yield os.path.join(current, name)
            elif recurse and name.isidentifier() and name != '__pycache__':
                child = os.path.join(current, name)
                if os.path.isfile(os.path.join(child, _PACKAGE_INIT)):
                    pending.append(child)


class _PinnedSourceLoader(importlib.machinery.SourceFileLoader):
    """A SourceFileLoader whose module source is the pinned revision."""

    def __init__(self, fullname: str, path: str, pin: _Pin) -> None:
        super().__init__(fullname, path)
        self._pin = pin

    def path_stats(self, path):
        if path == self.path:
            return {'mtime': self._pin.mtime, 'size': self._pin.size}
        return super().path_stats(path)

    def get_data(self, path):
        if path == self.path:
            return self._pin.source()
        return super().get_data(path)

    def set_data(self, path, data, *, _mode=0o666):
        # Every process reading the newer checkout would reject and rewrite
        # bytecode compiled from an older revision.
        if self._pin.on_disk(self.path):
            super().set_data(path, data, _mode=_mode)

    def exec_module(self, module):
        if not self._pin.on_disk(self.path):
            # Tracebacks read linecache, which would show the checkout's lines.
            source = importlib.util.decode_source(self._pin.source())
            linecache.cache[self.path] = (
                self._pin.size, None, source.splitlines(True), self.path)
        super().exec_module(module)


class PinnedImportSource:
    """Meta-path finder serving application modules from their pinned source."""

    def __init__(self, roots: Iterable[str]) -> None:
        self._roots = frozenset(os.path.abspath(root) for root in roots)
        self._pins: dict[str, _Pin] = {}
        self._packages: set[str] = set()
        self._sweeps: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._frozen = False

    def install(self) -> None:
        """Insert the finder ahead of PathFinder and pin the source in use."""
        trees = []
        for name, module in list(sys.modules.items()):
            directory = self._application_package(name, getattr(module, '__spec__', None))
            if directory is not None:
                self._packages.add(name)
                trees.append(directory)
        position = next((index for index, finder in enumerate(sys.meta_path)
                         if finder is importlib.machinery.PathFinder), len(sys.meta_path))
        sys.meta_path.insert(position, self)
        self._sweep(sorted(self._roots), trees)

    def uninstall(self) -> None:
        """Remove the finder; modules already imported keep their code."""
        if self in sys.meta_path:
            sys.meta_path.remove(self)

    def join(self) -> None:
        """Wait until every pinning sweep started so far has finished."""
        with self._lock:
            sweeps = list(self._sweeps)
        for sweep in sweeps:
            sweep.join()

    def freeze(self) -> dict:
        """Pin the complete executable generation before resident admission.

        Ordinary API users retain lazy package adoption and explicit reload.
        The independent execution owner cannot acquire code from a later API
        checkout, including packages it has never imported. The returned digest
        describes retained bytes, so composition can compare its prepared plan.
        """
        self.join()
        sources = sorted({os.path.abspath(path) for root in self._roots
                          for path in _importable_sources(root, recurse=True, ignore_errors=False)})
        for path in sources:
            if path not in self._pins:
                pin = _read_pin(path)
                if pin is None:
                    raise RuntimeError(f'Could not pin execution source: {path}')
                self._pins.setdefault(path, pin)
            for root in self._roots:
                relative = os.path.relpath(path, root)
                parts = relative.split(os.sep)
                if len(parts) > 1 and parts[0] != '..':
                    self._packages.add(parts[0])
        self._frozen = True
        return source_identity((path, pin.source()) for path, pin in sorted(self._pins.items()))

    def find_spec(self, fullname, path=None, target=None):
        if self._frozen:
            return self._frozen_spec(fullname, path)
        if target is not None:
            # importlib.reload asks for the checkout's current source.
            return None
        top = fullname.partition('.')[0]
        if top == fullname:
            return self._top_level(
                fullname, importlib.machinery.PathFinder.find_spec(fullname, path))
        if top not in self._packages:
            return None
        tail = fullname.rpartition('.')[2]
        for entry in path or ():
            location = os.path.join(entry, tail)
            for origin, locations in ((os.path.join(location, _PACKAGE_INIT), [location]),
                                      (location + '.py', None)):
                pin = self._pins.get(os.path.abspath(origin))
                if pin is not None:
                    return self._spec(fullname, origin, pin, locations)
        return None

    def _frozen_spec(self, fullname, path):
        top, _, tail = fullname.partition('.')
        if tail and top not in self._packages:
            return None
        entries = path if path is not None else sys.path
        name = fullname.rpartition('.')[2]
        for entry in entries:
            directory = os.path.abspath(entry or os.getcwd())
            if path is None and directory not in self._roots:
                continue
            location = os.path.join(directory, name)
            for origin, locations in ((os.path.join(location, _PACKAGE_INIT), [location]),
                                      (location + '.py', None)):
                pin = self._pins.get(origin)
                if pin is not None:
                    return self._spec(fullname, origin, pin, locations)
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.origin:
            # A virtualenv under the checkout has its own import root. Its
            # installed dependencies belong to the interpreter fingerprint,
            # not the application's pins. Classify by module/package topology
            # rather than every descendant of the checkout's directory.
            import_root = os.path.dirname(os.path.abspath(spec.origin))
            parents = fullname.count('.') + (spec.submodule_search_locations is not None)
            for _ in range(parents):
                import_root = os.path.dirname(import_root)
            if import_root in self._roots:
                raise ModuleNotFoundError(f'Module {fullname} is outside the prepared execution generation')
        return None

    def _top_level(self, name, spec):
        if spec is None or not spec.has_location:
            return spec
        directory = self._application_package(name, spec)
        if directory is not None:
            with self._lock:
                adopted = name not in self._packages
                self._packages.add(name)
            if adopted:
                self._sweep((), [directory])
        pin = self._pins.get(os.path.abspath(spec.origin))
        if pin is None:
            return spec
        return self._spec(name, spec.origin, pin, spec.submodule_search_locations)

    def _application_package(self, name, spec) -> str | None:
        """The directory of ``name`` when it is a regular package in a root."""
        if '.' in name or spec is None or not spec.origin or not spec.submodule_search_locations:
            return None
        directory, base = os.path.split(os.path.abspath(spec.origin))
        if base == _PACKAGE_INIT and os.path.dirname(directory) in self._roots:
            return directory
        return None

    @staticmethod
    def _spec(fullname, origin, pin, locations):
        return importlib.util.spec_from_file_location(
            fullname, origin, loader=_PinnedSourceLoader(fullname, origin, pin),
            submodule_search_locations=locations)

    def _sweep(self, roots: Iterable[str], packages: Iterable[str]) -> None:
        sweep = threading.Thread(
            target=self._pin_sources, args=(tuple(roots), tuple(packages)),
            name='tofu-pinned-import-source', daemon=True)
        with self._lock:
            self._sweeps.append(sweep)
        sweep.start()

    def _pin_sources(self, roots: tuple[str, ...], packages: tuple[str, ...]) -> None:
        started = time.monotonic()
        pinned = unpinned = source_bytes = compressed_bytes = 0
        sources = itertools.chain(
            *(_importable_sources(root, recurse=False) for root in roots),
            *(_importable_sources(package, recurse=True) for package in packages))
        for path in sources:
            key = os.path.abspath(path)
            if key in self._pins:
                continue
            pin = _read_pin(path)
            if pin is None:
                unpinned += 1
            elif self._pins.setdefault(key, pin) is pin:
                pinned += 1
                source_bytes += pin.size
                compressed_bytes += pin.compressed_size
        scope = [os.path.basename(package) for package in packages]
        if roots:
            scope.append('root modules')
        logger.info(
            '[PinnedImportSource] pinned %d source files of %s (%.1f MiB, %.1f MiB '
            'compressed) in %.1fs; %d unreadable or rewritten while read load from '
            'the checkout', pinned, ', '.join(scope), source_bytes / 2**20,
            compressed_bytes / 2**20, time.monotonic() - started, unpinned)
