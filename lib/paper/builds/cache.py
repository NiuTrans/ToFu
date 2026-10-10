"""Bounded reconstructible build-artifact repository with cross-process leases.

LocalBuildCache is the filesystem adapter. Services name owner/document/build,
never raw paths. Stage and read leases exclude active data from reclamation;
immutable publication and latest-success replacement are separate operations.
User-saved or research-pinned artifacts belong to durable storage, not here.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .budget import BuildBudget, build_budget
from .compiler import CompiledBuild, artifact_metadata
from .contract import limit, validate
from .errors import BuildError
from .viewers import active_viewers, write_viewers


@contextlib.contextmanager
def _file_lock(path: Path, *, wait: bool = True, shared: bool = False):
    stream = path.open('a+b')
    acquired = False
    deadline = time.monotonic() + (5 if wait else 0)
    try:
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    if path.stat().st_size == 0:
                        stream.write(b'\0')
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                                | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        yield acquired
    finally:
        if acquired:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _size(directory: Path) -> int:
    # Count actual artifacts and lifecycle-owned reader metadata. Reader growth
    # must not depend on a pre-reserved fixed number of metadata bytes.
    return sum(path.stat().st_size for path in directory.rglob('*')
               if path.is_file() and not path.is_symlink())


def default_spool_root(cache_root: Path) -> Path:
    """Fast local scratch for staging, warm state and toolchain caches.

    Compilation is metadata-heavy; on a network cache volume every stat/open
    round trip multiplies compile time. TOFU_PAPER_BUILD_SPOOL_DIR overrides
    the per-user system-temp default. Any resolution failure keeps the
    historical behaviour of staging inside the (possibly remote) cache root.
    """
    configured = os.environ.get('TOFU_PAPER_BUILD_SPOOL_DIR', '').strip()
    candidates = [Path(configured).expanduser()] if configured else []
    name = 'TofuPaperBuilds' if os.name == 'nt' else f'tofu-paper-builds-{os.getuid()}'
    candidates.append(Path(tempfile.gettempdir()) / name)
    for candidate in candidates:
        try:
            candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != 'nt':
                os.chmod(candidate, 0o700)
            probe = candidate / '.probe'
            probe.write_bytes(b'')
            probe.unlink()
            return candidate
        except OSError:
            continue
    return Path(cache_root)


class LocalBuildCache:
    """One cache volume with finite global bytes/records, explicit caller identity."""

    def __init__(self, root: Path, budget: BuildBudget | None = None,
                 spool_root: Path | None = None):
        self.root = Path(root)
        self.budget = budget or build_budget()
        self.spool_root = self.root if spool_root is None else Path(spool_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.spool_root.mkdir(parents=True, exist_ok=True)
        self.toolchain_state = self.spool_root / 'toolchain-state'
        self.toolchain_state.mkdir(parents=True, exist_ok=True)
        self._state_check = 0.0

    @contextlib.contextmanager
    def _guard(self):
        with _file_lock(self.root / '.cache.lock') as acquired:
            if not acquired:
                raise BuildError('cache_busy', 'Paper artifact cache is busy; retry')
            yield

    def _document(self, owner_user_id: int, document_id: str) -> Path:
        if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int) or owner_user_id < 1:
            raise BuildError('owner', 'Paper artifact requires an explicit owner')
        if not re.fullmatch(r'[0-9a-f]{64}', document_id or ''):
            raise BuildError('build_identity', 'Invalid paper document identity')
        return self.root / str(owner_user_id) / document_id

    def _build(self, owner_user_id: int, document_id: str, build_id: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{32}', build_id or ''):
            raise BuildError('build_identity', 'Invalid paper build identity')
        return self._document(owner_user_id, document_id) / build_id

    def _work(self, owner_user_id: int, document_id: str, build_id: str) -> Path:
        final = self._build(owner_user_id, document_id, build_id)
        if self.spool_root == self.root:
            return final.with_name(build_id + '.work')
        return self.spool_root / str(owner_user_id) / document_id / (build_id + '.work')

    @contextlib.contextmanager
    def stage(self, owner_user_id: int, document_id: str, build_id: str):
        """Lease a fresh build's source/work directories; reclaim them on exit."""
        path = self._work(owner_user_id, document_id, build_id)
        with self._guard():
            self._prune_locked()
            path.mkdir(parents=True, exist_ok=False)
            lease = _file_lock(path / '.lease')
            acquired = lease.__enter__()
            if not acquired:
                lease.__exit__(None, None, None)
                raise BuildError('cache_busy', 'Paper staging directory is busy')
        try:
            yield path
        finally:
            with self._guard():
                lease.__exit__(None, None, None)
                shutil.rmtree(path, ignore_errors=True)
                self._clean_document(path.parent, path.name)

    @contextlib.contextmanager
    def warm_lease(self, owner_user_id: int, document_id: str):
        """Exclusive access to one document's incremental compile state.

        Warm state is reconstructible cache: a contended or missing lease
        yields None and the caller simply runs a cold build. The compiler
        treats seeded state as a hint: a failed warm run wipes it and retries
        once cold, so a stale aux can never keep a document broken.
        """
        self._document(owner_user_id, document_id)  # same identity validation
        path = self.spool_root / 'warm' / str(owner_user_id) / document_id
        with self._guard():
            path.mkdir(parents=True, exist_ok=True)
            lease = _file_lock(path / '.lease', wait=False)
            acquired = lease.__enter__()
            if not acquired:
                lease.__exit__(None, None, None)
        if not acquired:
            yield None
            return
        try:
            os.utime(path, None)
            yield path
        finally:
            lease.__exit__(None, None, None)

    def _prune_state_locked(self) -> None:
        """Bound warm state (LRU) and toolchain caches; both are pure caches."""
        now = time.monotonic()
        if now - self._state_check < 60:
            return
        self._state_check = now
        warm_root = self.spool_root / 'warm'
        records = []
        total = 0
        try:
            for owner in warm_root.iterdir():
                if not owner.is_dir() or not owner.name.isdecimal():
                    continue
                for document in owner.iterdir():
                    if document.is_dir() and re.fullmatch(r'[0-9a-f]{64}', document.name):
                        records.append((document.stat().st_mtime_ns, document))
                        total += _size(document)
        except OSError:
            pass
        for _, path in sorted(records):
            if total <= limit('warm_state_total_bytes'):
                break
            with _file_lock(path / '.lease', wait=False) as acquired:
                if not acquired:
                    continue
                size = _size(path)
            shutil.rmtree(path, ignore_errors=True)
            total -= size
            try:
                path.parent.rmdir()
            except OSError:
                pass
        try:
            if _size(self.toolchain_state) > limit('toolchain_state_bytes'):
                shutil.rmtree(self.toolchain_state, ignore_errors=True)
                self.toolchain_state.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def publish(self, build: CompiledBuild, *, make_current: bool = True) -> dict:
        """Copy only declared artifacts, verify digests, then publish atomically.

        make_current is chosen by the task owner under its revision fence.
        Failed builds remain diagnostic records and never replace latest.json.
        """
        receipt = validate(build.receipt)
        final = self._build(receipt['owner_user_id'], receipt['document_id'], receipt['build_id'])
        declared_bytes = sum(row['size_bytes'] for row in receipt['artifacts'])
        if declared_bytes > self.budget.cache_bytes:
            raise BuildError('cache_limit', 'Build artifacts exceed the cache byte budget')
        temp = final.with_name(receipt['build_id'] + '.publish')
        with self._guard():
            self._reclaim_orphans_locked()
            if final.exists():
                existing = self._receipt(final)
                if existing != receipt:
                    raise BuildError('build_conflict', 'An immutable build cannot be replaced')
                return existing
            if make_current and not receipt['version_verified']:
                try:
                    prior_id = json.loads((final.parent / 'latest.json').read_text())['build_id']
                    prior = self._receipt(self._build(receipt['owner_user_id'], receipt['document_id'], prior_id))
                    if prior['status'] == 'success' and prior['version_verified']:
                        make_current = False
                except (OSError, ValueError, KeyError, BuildError):
                    pass
            temp.mkdir(parents=True, exist_ok=False)
            try:
                for row in receipt['artifacts']:
                    source = build.directory / row['name']
                    if artifact_metadata(source) != row:
                        raise BuildError('artifact_changed', 'Build artifact changed before publication')
                    shutil.copyfile(source, temp / row['name'])
                    if artifact_metadata(temp / row['name']) != row:
                        raise BuildError('artifact_changed', 'Build artifact changed during publication')
                (temp / 'receipt.json').write_text(json.dumps(receipt, separators=(',', ':')))
                (temp / '.lease').touch()
                protected = set()
                if receipt['status'] != 'success' or not make_current:
                    try:
                        prior = json.loads((final.parent / 'latest.json').read_text())['build_id']
                        protected.add(final.parent / prior)
                    except (OSError, ValueError, KeyError):
                        pass
                removals = self._eviction_plan(_size(temp), 1, protected)
                # No old artifact is removed until both new immutable data and
                # its success pointer have committed. Failed publication can
                # therefore never destroy the last successful preview.
                os.replace(temp, final)
                if make_current and receipt['status'] == 'success':
                    pointer = final.parent / ('.latest-' + uuid.uuid4().hex)
                    try:
                        pointer.write_text(json.dumps({'build_id': receipt['build_id']}))
                        os.replace(pointer, final.parent / 'latest.json')
                    except OSError:
                        shutil.rmtree(final)
                        raise
                    finally:
                        pointer.unlink(missing_ok=True)
                self._delete_records(removals)
            finally:
                shutil.rmtree(temp, ignore_errors=True)
        return receipt

    def _receipt(self, directory: Path) -> dict:
        try:
            path = directory / 'receipt.json'
            if path.stat().st_size > 256 * 1024:
                raise BuildError('build_contract', 'Oversized build receipt')
            return validate(json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            raise BuildError('build_not_found', 'Paper build is unavailable') from exc

    @contextlib.contextmanager
    def lease(self, owner_user_id: int, document_id: str, build_id: str):
        """Keep a verified build alive through an entire HTTP read or analysis."""
        path = self._build(owner_user_id, document_id, build_id)
        with self._guard():
            if not path.is_dir():
                raise BuildError('build_not_found', 'Paper build is unavailable')
            receipt = self._receipt(path)
            if (receipt['owner_user_id'], receipt['document_id'], receipt['build_id']) != (
                    owner_user_id, document_id, build_id):
                raise BuildError('build_not_found', 'Paper build is unavailable')
            lease = _file_lock(path / '.lease', shared=True)
            if not lease.__enter__():
                lease.__exit__(None, None, None)
                raise BuildError('cache_busy', 'Paper build is busy; retry')
            os.utime(path, None)
        try:
            yield CompiledBuild(receipt, path)
        finally:
            lease.__exit__(None, None, None)

    @contextlib.contextmanager
    def open_artifact(self, owner_user_id: int, document_id: str, build_id: str, name: str):
        from .artifact_reader import FileBuildArtifacts
        with self.lease(owner_user_id, document_id, build_id) as build:
            with FileBuildArtifacts(build).open_artifact(name) as artifact:
                yield artifact

    def latest(self, owner_user_id: int, document_id: str) -> dict | None:
        with self._guard():
            path = self._document(owner_user_id, document_id) / 'latest.json'
            try:
                build_id = json.loads(path.read_text())['build_id']
                receipt = self._receipt(self._build(owner_user_id, document_id, build_id))
            except (OSError, ValueError, KeyError, BuildError):
                return None
            return receipt if (receipt['status'] == 'success' and (
                receipt['owner_user_id'], receipt['document_id'], receipt['build_id']) == (
                owner_user_id, document_id, build_id)) else None

    def promote(self, owner_user_id: int, document_id: str, build_id: str) -> bool:
        """Advance a prepared cache entry after its durable publication commits."""
        with self._guard():
            final = self._build(owner_user_id, document_id, build_id)
            receipt = self._receipt(final)
            if (receipt['owner_user_id'], receipt['document_id'], receipt['build_id']) != (
                    owner_user_id, document_id, build_id):
                raise BuildError('build_not_found', 'Paper build is unavailable')
            if receipt['status'] != 'success':
                return False
            if not receipt['version_verified']:
                try:
                    prior_id = json.loads((final.parent / 'latest.json').read_text())['build_id']
                    prior = self._receipt(self._build(owner_user_id, document_id, prior_id))
                    if prior['status'] == 'success' and prior['version_verified']:
                        return False
                except (OSError, ValueError, KeyError, BuildError):
                    pass
            pointer = final.parent / ('.latest-' + uuid.uuid4().hex)
            try:
                pointer.write_text(json.dumps({'build_id': build_id}))
                os.replace(pointer, final.parent / 'latest.json')
            finally:
                pointer.unlink(missing_ok=True)
            return True

    def viewer(self, owner_user_id: int, document_id: str, build_id: str, payload: dict) -> dict:
        """Retain, renew or release one reader independently of compilation."""
        request = validate(payload, 'PaperBuildViewerRequestV1')
        path = self._build(owner_user_id, document_id, build_id)
        with self._guard():
            receipt = self._receipt(path)
            if (receipt['owner_user_id'], receipt['document_id'], receipt['build_id']) != (
                    owner_user_id, document_id, build_id):
                raise BuildError('build_not_found', 'Paper build is unavailable')
            now = int(time.time())
            viewers = active_viewers(path, now)
            identity, operation = request['viewer_id'], request['operation']
            if operation == 'retain' or (operation == 'renew' and identity in viewers):
                viewers[identity] = now + limit('viewer_lease_seconds')
            elif operation == 'release':
                viewers.pop(identity, None)
            write_viewers(path, viewers)
            retained = identity in viewers
            if retained:
                os.utime(path, None)
            return validate({'retained': retained, 'expires_in_seconds':
                             limit('viewer_lease_seconds') if retained else 0}, 'PaperBuildViewerResultV1')

    def _records(self):
        for owner in self.root.iterdir():
            if not owner.is_dir() or not owner.name.isdecimal():
                continue
            for document in owner.iterdir():
                if document.is_dir() and re.fullmatch(r'[0-9a-f]{64}', document.name):
                    for build in document.iterdir():
                        if build.is_dir() and re.fullmatch(r'[0-9a-f]{32}', build.name):
                            yield build

    def _occupancy(self) -> tuple[int, int]:
        records = list(self._records())
        return len(records), sum(_size(path) for path in records)

    def _prune_locked(self, *, reserve_bytes: int = 0, reserve_records: int = 0):
        self._reclaim_orphans_locked()
        try:
            removals = self._eviction_plan(reserve_bytes, reserve_records, set())
        except BuildError:
            return  # live leases can delay reclamation after a budget reduction
        self._delete_records(removals)

    def _eviction_plan(self, reserve_bytes: int, reserve_records: int, protected: set) -> list[Path]:
        records = sorted(self._records(), key=lambda p: p.stat().st_mtime_ns)
        total = sum(_size(path) for path in records)
        count = len(records)
        removals = []
        for path in records:
            if total + reserve_bytes <= self.budget.cache_bytes and count + reserve_records <= self.budget.cache_records:
                break
            if path in protected:
                continue
            try:
                if active_viewers(path):
                    continue
            except BuildError:
                continue  # corrupt but not yet expired reader metadata is protected
            with _file_lock(path / '.lease', wait=False) as acquired:
                if not acquired:
                    continue
                size = _size(path)
            removals.append(path)
            total -= size
            count -= 1
        if total + reserve_bytes > self.budget.cache_bytes or count + reserve_records > self.budget.cache_records:
            raise BuildError('cache_limit', 'Paper cache is in use; close an inactive preview and retry')
        return removals

    def _delete_records(self, records: list[Path]) -> None:
        # Caller still holds the cache-wide guard: no new lease can start
        # between checking eligibility and deleting a record, on any platform.
        for path in records:
            shutil.rmtree(path)
            self._clean_document(path.parent, path.name)

    def _clean_document(self, document: Path, removed_build_id: str) -> None:
        pointer = document / 'latest.json'
        try:
            if json.loads(pointer.read_text()).get('build_id') == removed_build_id:
                pointer.unlink()
        except (OSError, ValueError):
            pass
        try:
            document.rmdir()
            document.parent.rmdir()
        except OSError:
            pass  # another build/staging directory still owns this document

    def _reclaim_orphans_locked(self) -> None:
        for base in {self.root, self.spool_root}:
            self._reclaim_orphans_in(base)
        self._prune_state_locked()

    def _reclaim_orphans_in(self, base: Path) -> None:
        try:
            owners = list(base.iterdir())
        except OSError:
            return
        for owner in owners:
            if not owner.is_dir() or not owner.name.isdecimal():
                continue
            for document in owner.iterdir():
                if not document.is_dir() or not re.fullmatch(r'[0-9a-f]{64}', document.name):
                    continue
                for path in list(document.iterdir()):
                    if not path.is_dir() or not re.fullmatch(r'[0-9a-f]{32}\.(work|reclaim|publish)', path.name):
                        continue
                    with _file_lock(path / '.lease', wait=False) as acquired:
                        if not acquired:
                            continue
                    shutil.rmtree(path)
                    self._clean_document(document, path.name.split('.')[0])

    def prune(self) -> dict:
        with self._guard():
            self._prune_locked()
            count, size = self._occupancy()
            return {'records': count, 'bytes': size,
                    'max_records': self.budget.cache_records, 'max_bytes': self.budget.cache_bytes}
