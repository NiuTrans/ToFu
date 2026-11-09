"""Content-addressed test discovery for immutable verification snapshots.

Inputs are a Git revision and its loaded selector. Git enumerates the complete
test inventory; the selector remains the sole import/reference policy. Only
requested, changed blobs are read, in one cat-file transaction. One atomically
replaced cache retains entries for the current inventory, shared across private
checkouts. Cache loss/concurrent replacement costs work, never coverage.

Entry points: build_index, select_paths. Dependencies: standard library, Git.
Contract: docs/modules/project_integration.md, "Landing verification".
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import time


class UnsupportedIndex(ValueError):
    """A selector/filesystem feature needs the selector's ordinary discovery."""


@dataclasses.dataclass
class IndexResult:
    index: dict
    parsed: int
    reused: int
    seconds: float
    cache_error: str = ''


def select_paths(inventory, paths=None) -> list[str]:
    """Resolve complete file/directory scopes; a typo never yields a partial run."""
    wanted = []
    for value in paths or ():
        path = str(PurePosixPath(value))
        if path.startswith('/') or '..' in PurePosixPath(path).parts or '::' in path:
            raise ValueError(f'invalid test path: {value}')
        wanted.append(path)
    selected = sorted(test for test in inventory if not wanted or any(
        path == '.' or test == path or test.startswith(path + '/') for path in wanted))
    missing = [path for path in wanted if not any(
        path == '.' or test == path or test.startswith(path + '/') for test in selected)]
    if missing or not selected:
        raise ValueError('no test files matched requested paths: ' + ', '.join(missing or wanted))
    return selected


def _git(root, *args, input=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(LC_ALL='C', GIT_OPTIONAL_LOCKS='0')
    return subprocess.run(['git', *args], cwd=root, env=env, input=input,
                          capture_output=True, check=True).stdout


def test_inventory(root, revision):
    entries = {}
    for entry in _git(root, 'ls-tree', '-r', '-z', '--full-tree', revision, '--', 'tests').split(b'\0'):
        if not entry:
            continue
        meta, raw_path = entry.split(b'\t', 1)
        path = os.fsdecode(raw_path)
        if not (PurePosixPath(path).name.startswith('test_') and path.endswith('.py')):
            continue
        mode, kind, blob = meta.split()
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise UnsupportedIndex(f'test path requires ordinary discovery: {path}')
        entries[path] = blob.decode('ascii')
    if not entries:
        raise ValueError('test census is empty; check the revision and tests directory')
    return entries


def _sources(root, blobs):
    if not blobs:
        return {}
    output = _git(root, 'cat-file', '--batch', input=('\n'.join(blobs) + '\n').encode('ascii'))
    sources, offset = {}, 0
    for blob in blobs:
        end = output.index(b'\n', offset)
        oid, kind, size = output[offset:end].split()
        start, stop = end + 1, end + 1 + int(size)
        if oid.decode('ascii') != blob or kind != b'blob' or output[stop:stop + 1] != b'\n':
            raise ValueError(f'incomplete Git blob response: {blob}')
        sources[blob] = output[start:stop].decode('utf-8')
        offset = stop + 1
    if offset != len(output):
        raise ValueError('unexpected trailing Git blob response')
    return sources


def _read_cache(path, extractor):
    try:
        saved = json.loads(Path(path).read_text(encoding='utf-8'))
        if (isinstance(saved, dict) and saved.get('version') == 1
                and saved.get('extractor') == extractor and isinstance(saved.get('entries'), dict)
                and saved.get('checksum') == _checksum(saved['entries'])):
            return {name: entry for name, entry in saved['entries'].items()
                    if isinstance(entry, dict) and isinstance(entry.get('blob'), str)
                    and isinstance(entry.get('refs'), list)
                    and all(isinstance(ref, str) for ref in entry['refs'])}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _checksum(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _publish(path, payload):
    temporary = None
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         delete=False) as stream:
            temporary = stream.name
            json.dump(payload, stream, separators=(',', ':'))
        os.replace(temporary, path)
        return ''
    except OSError as error:
        return str(error)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def build_index(root, revision, selector, cache_path, paths=None) -> IndexResult:
    """Index exactly PATHS at REVISION, with byte-identical sources parsed once."""
    started = time.perf_counter()
    if not callable(getattr(selector, 'refs_of_test_file', None)):
        raise UnsupportedIndex('selector has no pure reference extractor')
    inventory = test_inventory(root, revision)
    selected = select_paths(inventory, paths)
    extractor = hashlib.sha256(Path(selector.__file__).read_bytes()
                               + sys.implementation.cache_tag.encode()).hexdigest()
    saved = _read_cache(cache_path, extractor) if cache_path else {}
    by_blob = {entry['blob']: entry['refs'] for entry in saved.values()}
    missing = sorted({inventory[path] for path in selected} - by_blob.keys())
    for blob, source in _sources(root, missing).items():
        by_blob[blob] = sorted(selector.refs_of_test_file(source))
    entries = {path: {'blob': blob, 'refs': by_blob[blob]}
               for path, blob in inventory.items() if blob in by_blob}
    error = ''
    if cache_path and entries != saved:
        error = _publish(cache_path, {'version': 1, 'extractor': extractor, 'entries': entries,
                                      'checksum': _checksum(entries)})
    return IndexResult({path: {'refs': entries[path]['refs']} for path in selected},
                       len(missing), len(selected) - len(missing),
                       time.perf_counter() - started, error)
