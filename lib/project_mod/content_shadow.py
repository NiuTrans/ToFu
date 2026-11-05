"""Demand-filled local file cache for indexed project search.

Entry points: candidate_view (a leased, freshness-checked mixed source/cache
view), warm, invalidate, shadow_metrics. Only requested candidates are copied;
large projects and large individual files always remain searchable in place.
Source files are the authority. A cache miss, full volume, copy race or missing
cache is transparent. Reads validate nanosecond identities before AND after
search. The shared placement policy chooses local storage; flock protects
readers/builders across processes and eviction only removes private cache data.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import OrderedDict
import bisect
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time

from serverctl_pkg import local_placement
from bootstrap_pkg.resource_policy_generated import RESOURCE_FALLBACKS


def _limit(value, default):
    try:
        return max(0, int(value))
    except ValueError:
        return default


_ENABLED = os.environ.get('TOFU_CONTENT_SHADOW', '1').lower() not in ('0', 'false', 'off')
_DEFAULTS = RESOURCE_FALLBACKS['personal']
_MAX_FILE_BYTES = _limit(os.environ.get('TOFU_CONTENT_SHADOW_MAX_FILE_BYTES', ''),
                         _DEFAULTS['TOFU_CONTENT_SHADOW_MAX_FILE_BYTES'])
# For tiny files, two freshness observations cost as much as reading the
# source. Their directory/index acceleration already has a separate owner.
_MIN_FILE_BYTES = 64 * 1024
_ROOT_MAX_FILES = _limit(os.environ.get('TOFU_CONTENT_SHADOW_ROOT_MAX_FILES', ''),
                         _DEFAULTS['TOFU_CONTENT_SHADOW_ROOT_MAX_FILES'])
_ROOT_MAX_BYTES = _limit(os.environ.get('TOFU_CONTENT_SHADOW_ROOT_MAX_BYTES', ''),
                         _DEFAULTS['TOFU_CONTENT_SHADOW_ROOT_MAX_BYTES'])
_GLOBAL_MAX_BYTES = _limit(os.environ.get('TOFU_CONTENT_SHADOW_GLOBAL_MAX_BYTES', ''),
                           _DEFAULTS['TOFU_CONTENT_SHADOW_GLOBAL_MAX_BYTES'])
_SYNC_ROUND_FILES = 2000
_SYNC_ROUND_BYTES = 64 * 1024 * 1024
_SYNC_ROUND_SECONDS = 0.25
_TTL_SECONDS = 7 * 24 * 60 * 60
_FORMAT = 'tofu.content-shadow/v2'
_HOT_REQUESTS = 3
_HEAT_TTL_SECONDS = 10 * 60
_HEAT_MAX_ENTRIES = 8192
_UNSTABLE_BACKOFF_SECONDS = 60
_lock = threading.Lock()
_workers: dict[str, threading.Thread] = {}
_heat: OrderedDict[tuple[str, str], tuple[int, float]] = OrderedDict()
_unstable: OrderedDict[str, float] = OrderedDict()


def _home(root):
    if os.name != 'posix':
        return None
    override = os.environ.get('TOFU_CONTENT_SHADOW_DIR', '').strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            return None
    else:
        placement = local_placement.index_cache_placement(root)
        if not placement.selected:
            return None
        path = Path(placement.root) / 'content'
    source = os.path.realpath(root)
    if os.path.commonpath((source, os.path.realpath(path))) == source:
        return None  # a mirror inside its source would index/copy itself
    local_placement.ensure_private_directory(path)
    return path


def _root_dir(root, home):
    return home / hashlib.sha256(os.fsencode(root)).hexdigest()


def _relative(root, path):
    prefix = root.rstrip(os.sep) + os.sep
    if not path.startswith(prefix):
        raise ValueError('candidate outside source root')
    relative = path[len(prefix):]
    if not relative or any(part in ('', '.', '..') for part in relative.split(os.sep)):
        raise ValueError('non-canonical candidate path')
    return relative


def _identity(path):
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError('not a regular file')
    return [metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns, stat.S_IMODE(metadata.st_mode)]


@contextmanager
def _lease(directory, exclusive=False):
    import fcntl
    fd = None
    try:
        # Fixed cache-lock shards avoid leaving one permanent inode per project.
        # Contention is a cache miss, never a wait or refusal of user work.
        name = (directory.parent / ('.lease-' + directory.name[:2])
                if len(directory.name) == 64 else Path(str(directory) + '.lock'))
        fd = os.open(name,
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise OSError('unsafe cache lease')
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except OSError:
        if fd is not None:
            os.close(fd)
        fd = None
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def _read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError('unsafe cache metadata')
        return json.load(stream)


def _manifest(directory):
    try:
        if directory.is_symlink():
            return {}
        data = _read_json(directory / 'manifest.json')
        if (data.get('format') == _FORMAT and isinstance(data.get('entries'), dict)
                and all(isinstance(value, dict)
                        and type(value.get('storageBytes')) is int
                        and value['storageBytes'] >= 0
                        and isinstance(value.get('identity'), list)
                        and len(value['identity']) == 6
                        and all(type(item) is int and item >= 0 for item in value['identity'])
                        for value in data['entries'].values())):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def _owned(directory, root=None):
    try:
        if directory.is_symlink():
            return False
        owner = _read_json(directory / '.owner.json')
        source = owner['root']
        return (owner.get('format') == _FORMAT
                and hashlib.sha256(os.fsencode(source)).hexdigest() == directory.name
                and (root is None or source == root))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _claim_root(directory, root):
    if _owned(directory, root):
        return True
    if any(directory.iterdir()):
        return False
    # Persist ownership before any payload. A lost/corrupt disposable manifest
    # can then be reclaimed without guessing that arbitrary files are a cache.
    with (directory / '.owner.json').open('x') as stream:
        json.dump({'format': _FORMAT, 'root': root}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _publish(directory, manifest):
    fd, name = tempfile.mkstemp(prefix='.manifest-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(manifest, stream, separators=(',', ':'))
        os.replace(name, directory / 'manifest.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _cached_path(directory, relative):
    # Flat names prevent parent symlinks, traversal and deep directory overhead.
    return directory / hashlib.sha256(os.fsencode(relative)).hexdigest()


def _entry_bytes(size, relative, *, allocated=0):
    # Include allocation rounding and conservative JSON metadata overhead, so
    # empty files and very long names cannot bypass the disk retention budget.
    return max(allocated, ((size + 4095) // 4096) * 4096) + len(os.fsencode(relative)) * 6 + 512


def _hot_candidates(root, candidates):
    now = time.monotonic()
    selected = []
    with _lock:
        for path in candidates:
            key = (root, path)
            count, seen = _heat.pop(key, (0, now))
            count = count + 1 if now - seen <= _HEAT_TTL_SECONDS else 1
            if count >= _HOT_REQUESTS:
                selected.append(path)
            else:
                _heat[key] = (count, now)
        while len(_heat) > _HEAT_MAX_ENTRIES:
            _heat.popitem(last=False)
    return selected


def _valid_cached(directory, relative, state):
    try:
        path = _cached_path(directory, relative)
        metadata = path.lstat()
        current = (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
                and metadata.st_size == state['identity'][2]
                and [metadata.st_mtime_ns, metadata.st_ctime_ns] == state['cachedTimes'])
        if not current:
            return False
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return False
            return hashlib.file_digest(stream, 'sha256').hexdigest() == state['sha256']
    except (OSError, KeyError, TypeError, IndexError):
        return False


@dataclass
class CandidateView:
    paths: list[str]
    root: str = ''
    replacements: dict[str, str] = field(default_factory=dict)
    identities: dict[str, list] = field(default_factory=dict)
    executor: object = None

    def current(self):
        try:
            current = all(_validate_groups(
                self.executor, list(self.identities.items()),
                lambda item: _identity(item[0]) == item[1]))
        except (OSError, ValueError):
            current = False
        if not current:
            # Moving sources (including delayed network metadata) should not
            # repeatedly pay for a cached pass followed by a live retry.
            with _lock:
                _unstable[self.root] = time.monotonic() + _UNSTABLE_BACKOFF_SECONDS
                _unstable.move_to_end(self.root)
                while len(_unstable) > 256:
                    _unstable.popitem(last=False)
        return current

    def rewrite(self, raw):
        # Only rewrite the filename field. Matched source content is opaque.
        if not self.replacements:
            return raw
        replacements = {os.fsencode(k): os.fsencode(v) for k, v in self.replacements.items()}
        lines = []
        for line in raw.splitlines(keepends=True):
            if b'\0' in line:
                path, separator, tail = line.partition(b'\0')
            else:  # rg count output uses the last colon before the integer.
                path, separator, tail = line.rpartition(b':')
            lines.append(replacements.get(path, path) + separator + tail)
        return b''.join(lines)

    def rewrite_withheld(self, withheld):
        if not withheld or not self.replacements:
            return withheld
        return {**withheld, 'files': tuple(sorted((
            (self.replacements.get(row[0], row[0]), *row[1:])
            for row in withheld.get('files', ())), key=lambda row: (-row[1], row[0])))}


def _validate_groups(executor, items, function):
    """Eight physical I/O groups; reuse the project's demand executor owner.

    The grouping is hardware-independent and covers every candidate. It keeps
    metadata validation from serializing network round trips or creating one
    carrier per file. Every submitted group starts through the demand executor.
    """
    if executor is None or len(items) < 128:
        return [function(item) for item in items]
    width = (len(items) + 7) // 8
    groups = [items[start:start + width] for start in range(0, len(items), width)]
    futures = [executor.submit(lambda group=group: [function(item) for item in group])
               for group in groups]
    return [value for future in futures for value in future.result()]


def _candidate_hit(directory, candidate):
    _index, path, relative, state = candidate
    try:
        return (_identity(path) == state['identity']
                and _valid_cached(directory, relative, state))
    except (OSError, ValueError, KeyError, TypeError):
        return False


@contextmanager
def candidate_view(root, candidates, *, entry=None):
    """Keep hits leased until search and freshness verification have finished."""
    root = os.path.abspath(root)
    view = CandidateView(candidates, root=root)
    with _lock:
        cooling = _unstable.get(root, 0) > time.monotonic()
    if cooling:
        yield view
        return
    pending, used = [], []
    home = None
    try:
        home = _home(root) if _ENABLED else None
    except OSError:
        pass
    if home is None:
        yield view
        return
    directory = _root_dir(root, home)
    try:
        with _lease(directory) as held:
            if held:
                entries = _manifest(directory).get('entries', {})
                candidates_to_check = []
                for index, path in enumerate(candidates):
                    if not entries and len(pending) >= _SYNC_ROUND_FILES:
                        break
                    relative = None
                    try:
                        relative = _relative(root, path)
                        state = entries.get(relative)
                        if state:
                            if state['identity'][2] < _MIN_FILE_BYTES:
                                continue
                            candidates_to_check.append((index, path, relative, state))
                            continue
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                    if relative is None:
                        continue
                    if len(pending) < _SYNC_ROUND_FILES:
                        if entry is not None:
                            position = bisect.bisect_left(entry.paths, relative)
                            if (position >= len(entry.paths) or entry.paths[position] != relative
                                    or not _MIN_FILE_BYTES <= entry.sizes[position] <= _MAX_FILE_BYTES):
                                continue
                        pending.append(path)
                if len(candidates_to_check) >= 128:
                    from lib.observability import InstrumentedThreadPoolExecutor
                    view.executor = InstrumentedThreadPoolExecutor(
                        max_workers=4, metric_pool='project-read',
                        thread_name_prefix='content-validation')
                hits = _validate_groups(view.executor, candidates_to_check,
                                        lambda candidate: _candidate_hit(directory, candidate))
                for (index, path, relative, state), hit in zip(candidates_to_check, hits):
                    if not hit:
                        if len(pending) < _SYNC_ROUND_FILES:
                            pending.append(path)
                        continue
                    cached = str(_cached_path(directory, relative))
                    if not view.replacements:
                        view.paths = list(candidates)
                    view.paths[index] = cached
                    view.replacements[cached] = path
                    view.identities[path] = state['identity']
                    used.append(relative)
                if view.replacements:
                    try:
                        os.utime(directory / 'manifest.json')
                    except OSError:
                        pass
            yield view
    finally:
        if view.executor is not None:
            view.executor.shutdown(wait=True, cancel_futures=False)
        # Start only after releasing our read lease. Work is bounded to the
        # requested hot set; a cold query never waits for copying.
        warm(root, _hot_candidates(root, pending), home=home, used=used)


def _copy(root, path, directory, relative, identity):
    if (os.path.realpath(path) != path
            or not local_placement.source_timestamp_is_stable(*identity[3:5])):
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    temporary = None
    try:
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns, stat.S_IMODE(before.st_mode)] != identity:
                return None
            out_fd, temporary = tempfile.mkstemp(prefix='.copy-', dir=directory)
            with os.fdopen(out_fd, 'wb') as output:
                remaining = identity[2]
                digest = hashlib.sha256()
                while remaining:
                    data = source.read(min(256 * 1024, remaining))
                    if not data:
                        return None
                    output.write(data)
                    digest.update(data)
                    remaining -= len(data)
                if source.read(1) or _identity(path) != identity:
                    return None
        destination = _cached_path(directory, relative)
        os.replace(temporary, destination)
        metadata = destination.stat()
        return {'identity': identity,
                'sha256': digest.hexdigest(),
                'cachedTimes': [metadata.st_mtime_ns, metadata.st_ctime_ns],
                'storageBytes': _entry_bytes(identity[2], relative,
                                            allocated=getattr(metadata, 'st_blocks', 0) * 512),
                'used': time.time()}
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _prune(home, keep=None):
    """Evict idle roots under byte/inode pressure; active readers always win."""
    with _lease(home / '.retention', exclusive=True) as held:
        if not held:
            return
        roots = []
        for directory in home.iterdir():
            if len(directory.name) != 64 or any(c not in '0123456789abcdef' for c in directory.name):
                continue
            manifest = _manifest(directory)
            if not _owned(directory):
                continue
            try:
                if manifest:
                    size = sum(state['storageBytes'] for state in manifest['entries'].values())
                    metadata = (directory / 'manifest.json').stat()
                    size += getattr(metadata, 'st_blocks', 0) * 512
                else:
                    # Exclusive collection still fences readers/builders below.
                    # Missing manifests must not create an unaccounted disk leak.
                    metadata = (directory / '.owner.json').stat()
                    size = sum(max(path.stat().st_size, getattr(path.stat(), 'st_blocks', 0) * 512)
                               for path in directory.iterdir() if path.is_file() and not path.is_symlink())
                size += (directory / '.owner.json').stat().st_size
                roots.append((metadata.st_mtime, directory, size))
            except (OSError, TypeError, KeyError, IndexError):
                continue
        total = sum(row[2] for row in roots)
        for used, directory, size in sorted(roots):
            if directory == keep:
                continue
            if (total <= _GLOBAL_MAX_BYTES and time.time() - used < _TTL_SECONDS
                    and local_placement.has_free_space(home)):
                continue
            with _lease(directory, exclusive=True) as claimed:
                if claimed and not directory.is_symlink():
                    shutil.rmtree(directory)
                    total -= size


def _sync_worker(root, candidates, home, used_paths=()):
    directory = _root_dir(root, home)
    try:
        _prune(home)
        with _lease(directory, exclusive=True) as held:
            if not held:
                return
            local_placement.ensure_private_directory(directory)
            if not _claim_root(directory, root):
                return
            manifest = _manifest(directory)
            if not manifest:
                manifest = {'format': _FORMAT, 'root': root, 'entries': {}}
                _publish(directory, manifest)
            if manifest.get('root') != root:
                return
            entries = manifest.get('entries', {})
            for relative in used_paths:
                if relative in entries:
                    entries[relative]['used'] = time.time()
            owned = {_cached_path(directory, relative).name for relative in entries}
            for path in directory.iterdir():
                if path.name.startswith(('.copy-', '.manifest-')) or (
                        len(path.name) == 64 and all(c in '0123456789abcdef' for c in path.name)
                        and path.name not in owned):
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
            used = sum(state['storageBytes'] for state in entries.values())
            copied = copied_bytes = 0
            deadline = time.monotonic() + _SYNC_ROUND_SECONDS
            for path in candidates:
                if (copied >= _SYNC_ROUND_FILES or copied_bytes >= _SYNC_ROUND_BYTES
                        or time.monotonic() >= deadline):
                    break
                relative = _relative(root, path)
                try:
                    identity = _identity(path)
                    size = identity[2]
                    required = _entry_bytes(size, relative)
                    if not _MIN_FILE_BYTES <= size <= min(_MAX_FILE_BYTES, _ROOT_MAX_BYTES, _GLOBAL_MAX_BYTES):
                        continue
                    old = entries.get(relative)
                    if old and old['identity'] == identity and _valid_cached(directory, relative, old):
                        old['used'] = time.time()
                        continue
                    # Admission applies only to a disposable copy. Always leave
                    # space and inodes for the original command and user writes.
                    while entries and (used + required > min(_ROOT_MAX_BYTES, _GLOBAL_MAX_BYTES)
                            or len(entries) >= _ROOT_MAX_FILES
                            or not local_placement.has_free_space(home, required)):
                        victim = min(entries, key=lambda rel: entries[rel].get('used', 0))
                        state = entries.pop(victim)
                        _cached_path(directory, victim).unlink(missing_ok=True)
                        used -= state['storageBytes']
                    if (not _ROOT_MAX_FILES or used + required > min(_ROOT_MAX_BYTES, _GLOBAL_MAX_BYTES)
                            or not local_placement.has_free_space(home, required)):
                        break
                    state = _copy(root, path, directory, relative, identity)
                    if state:
                        if relative in entries:
                            used -= entries[relative]['storageBytes']
                        entries[relative] = state
                        used += state['storageBytes']
                        copied += 1
                        copied_bytes += size
                except (OSError, ValueError):
                    continue
            _publish(directory, {'format': _FORMAT, 'root': root, 'entries': entries})
        _prune(home, keep=directory)
    except (OSError, ValueError, KeyError, TypeError):
        pass  # every failed cache operation leaves the original source usable
    finally:
        with _lock:
            _workers.pop(root, None)


def warm(root, candidates, *, home=None, used=()):
    if not _ENABLED or not candidates:
        return
    try:
        home = home or _home(root)
        if home is None:
            return
        with _lock:
            if root in _workers or _unstable.get(root, 0) > time.monotonic():
                return
            worker = threading.Thread(target=_sync_worker,
                                      args=(root, list(candidates), home, tuple(used)),
                                      name='content-shadow-sync', daemon=True)
            _workers[root] = worker
            try:
                worker.start()
            except (OSError, RuntimeError):
                _workers.pop(root, None)
    except OSError:
        pass


def invalidate(root):
    """Invalidate only an idle cache. Busy readers verify freshness themselves."""
    try:
        home = _home(root)
        if home is None:
            return
        directory = _root_dir(root, home)
        with _lease(directory, exclusive=True) as held:
            if held and _owned(directory, root):
                shutil.rmtree(directory)
    except OSError:
        pass


def shadow_metrics():
    with _lock:
        return {'enabled': _ENABLED, 'workers': len(_workers)}
