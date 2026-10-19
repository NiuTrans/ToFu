"""Pinned optional grammar bundles for builds and explicit installed selection.

Only the owned Rust child loads library code. This standard-library module
validates metadata, copies build inputs, and verifies installed package bytes.
No downloads, cache discovery, native bindings, threads or global image owners.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

_SCHEMA_PATH = Path(__file__).with_name('grammar_bundle.schema.json')
_SCHEMA = json.loads(_SCHEMA_PATH.read_text())
FORMAT = _SCHEMA['properties']['format']['const']
REGISTRY_VERSION = _SCHEMA['properties']['registry_version']['const']


def _digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _size(value):
    return type(value) is int and 0 < value < sys.maxsize


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('duplicate native grammar manifest field')
        result[name] = value
    return result


def _copy_verified(source, expected, expected_bytes, target=None):
    if not _digest(expected) or not _size(expected_bytes):
        raise ValueError('invalid native grammar artifact pin')
    descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb', buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_bytes:
            raise ValueError('native grammar artifact size or type differs')
        digest = hashlib.sha256()
        remaining = expected_bytes
        while remaining:
            block = stream.read(min(65536, remaining))
            if not block:
                raise ValueError('native grammar artifact was truncated')
            remaining -= len(block)
            digest.update(block)
            if target is not None:
                target.write(block)
        if stream.read(1) or digest.hexdigest() != expected:
            raise ValueError('native grammar artifact digest differs')


def _manifest(root, expected, expected_bytes):
    import io
    data = io.BytesIO()
    _copy_verified(root / 'grammars.json', expected, expected_bytes, data)
    record = json.loads(data.getvalue(), object_pairs_hook=_unique_object)
    if (not isinstance(record, dict) or set(record) != set(_SCHEMA['required'])
            or record['format'] != FORMAT or record['registry_version'] != REGISTRY_VERSION
            or not isinstance(record['libraries'], list)):
        raise ValueError('unsupported native grammar manifest')
    names = set()
    for entry in record['libraries']:
        if (not isinstance(entry, dict) or set(entry) != set(_SCHEMA['$defs']['library']['required'])
                or not isinstance(entry['file'], str)
                or re.fullmatch(_SCHEMA['$defs']['library']['properties']['file']['pattern'], entry['file']) is None
                or entry['file'] in names or not _size(entry['bytes']) or not _digest(entry['sha256'])):
            raise ValueError('invalid native grammar library entry')
        names.add(entry['file'])
    return record, data.getvalue()


def validate_contract(expected):
    if not _digest(expected) or hashlib.sha256(_SCHEMA_PATH.read_bytes()).hexdigest() != expected:
        raise ValueError('native grammar contract digest differs')


def installed_bundle(directory, record):
    """Validate frozen package metadata without reading a grammar or manifest."""
    fields = {'format', 'directory', 'manifest', 'sha256', 'bytes', 'notices_sha256', 'notices_bytes'}
    if (not isinstance(record, dict) or set(record) != fields
            or record['format'] != FORMAT or record['directory'] != 'grammars'
            or record['manifest'] != 'grammars.json'
            or not all(_digest(record[key]) for key in ('sha256', 'notices_sha256'))
            or not all(_size(record[key]) for key in ('bytes', 'notices_bytes'))):
        raise ValueError('invalid installed native grammar bundle')
    return Path(directory) / 'grammars', record['sha256'], record['bytes']


def _notice_snapshot(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb', buffering=0) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not _size(before.st_size):
            raise ValueError('native grammar bundle requires regular nonempty notices')
        remaining = before.st_size
        result = bytearray()
        while remaining:
            block = stream.read(min(65536, remaining))
            if not block:
                raise ValueError('native grammar notices changed while copying')
            result.extend(block)
            remaining -= len(block)
        after = os.fstat(stream.fileno())
        if (stream.read(1) or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('native grammar notices changed while copying')
        return bytes(result)


def package_bundle(source, destination, expected_manifest_sha256):
    """Copy explicit pinned code plus supplied notices into a fresh build tree.

    The release assembler supplies NOTICES.txt for its grammar distribution.
    Copying it does not assert completeness of third-party licensing evidence.
    """
    if sys.platform != 'linux':
        raise RuntimeError('sealed native grammar bundles require Linux')
    source, destination = Path(source), Path(destination)
    size = (source / 'grammars.json').stat().st_size
    record, manifest_bytes = _manifest(source, expected_manifest_sha256, size)
    notices_bytes = _notice_snapshot(source / 'NOTICES.txt')
    # Build products are immutable profiles. Refuse a pre-existing target instead
    # of replacing an unrelated bundle or silently retaining stale libraries.
    target = destination / 'grammars'
    if target.exists() or target.is_symlink():
        raise ValueError('native grammar bundle requires a fresh build directory')
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.grammars-', dir=destination) as temporary:
        staging = Path(temporary)
        (staging / 'grammars.json').write_bytes(manifest_bytes)
        (staging / 'NOTICES.txt').write_bytes(notices_bytes)
        for entry in record['libraries']:
            with (staging / entry['file']).open('xb') as output:
                _copy_verified(source / entry['file'], entry['sha256'], entry['bytes'], output)
        target.mkdir()  # Exclusive creation also refuses a concurrent build.
        try:
            for path in staging.iterdir():
                path.rename(target / path.name)
        except BaseException:
            shutil.rmtree(target)  # Only the directory created by this call.
            raise
    return dict(format=FORMAT, directory='grammars', manifest='grammars.json',
                sha256=expected_manifest_sha256, bytes=size,
                notices_sha256=hashlib.sha256(notices_bytes).hexdigest(), notices_bytes=len(notices_bytes))


def check_bundle(directory, record):
    root, digest, size = installed_bundle(directory, record)
    manifest, _ = _manifest(root, digest, size)
    for entry in manifest['libraries']:
        _copy_verified(root / entry['file'], entry['sha256'], entry['bytes'])
    _copy_verified(root / 'NOTICES.txt', record['notices_sha256'], record['notices_bytes'])
    return {'libraries': len(manifest['libraries']), 'bytes': sum(entry['bytes'] for entry in manifest['libraries'])}
