"""Portable Python wheelhouse admission, reuse and release packaging.

Entrypoints: identity, reuse, publish, import_bundle, package, release_bundle.
Dependencies: stdlib; target pip supplies wheel tags. No application imports.
Only complete, hash-verified wheelhouses are published; failed staging leaves
the previous cache intact. The installer directory lock owns cache writers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_UNPACKED_BYTES = 4 * MAX_ARCHIVE_BYTES
MAX_MEMBERS = 2048
MAX_MANIFEST_BYTES = 1024 * 1024


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def identity(python: str) -> dict:
    """Probe the TARGET interpreter, including its actual accepted wheel tags."""
    source = '''import json, platform, sys
from pip._vendor.packaging.tags import sys_tags
machine = platform.machine().lower()
machine = {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(machine, machine)
print(json.dumps({'python': sys.implementation.cache_tag,
 'platform': sys.platform, 'machine': machine,
 'tags': [str(tag) for tag in sys_tags()]}))'''
    return json.loads(subprocess.check_output(
        [python, '-I', '-c', source], text=True, timeout=30))


def requirements_digest(requirements: Path) -> str:
    """Local first-party wheel hashes, rather than builder-specific paths."""
    normalized = []
    for line in requirements.read_text().splitlines():
        value = line.strip()
        if not value or value.startswith('#'):
            continue
        local = Path(unquote(urlsplit(value).path)) if value.startswith('file:') else Path(value)
        if value.endswith('.whl') and local.is_file():
            value = local.name + ':' + digest(local)
        normalized.append(value)
    root = _source_root(requirements)
    if root is not None:
        for name in ('VERSION', 'uv.lock'):
            authority = root / name
            if authority.is_file():
                normalized.append(name + ':' + digest(authority))
    return hashlib.sha256(('\n'.join(normalized) + '\n').encode()).hexdigest()



def _source_root(requirements: Path) -> Path | None:
    return next((p for p in (requirements.parent, *requirements.parents[:3])
                 if (p / 'VERSION').is_file() and (p / 'install.sh').is_file()), None)


def _target(runtime: dict) -> dict:
    return {name: runtime[name] for name in ('python', 'platform', 'machine')}


def asset_name(version: str, runtime: dict) -> str:
    if not re.fullmatch(r'[0-9][0-9A-Za-z.+-]*', version):
        raise ValueError('Invalid Python bundle release version')
    return f"python-wheels-{version}-{runtime['python']}-{runtime['platform']}-{runtime['machine']}.tar.gz"


def _compatible(filename: str, runtime: dict) -> bool:
    # Wheel filename tags may be compressed, e.g. cp312.cp313-manylinux_2_17.
    parts = filename[:-4].rsplit('-', 3)
    if len(parts) != 4:
        return False
    python, abi, platforms = parts[-3:]
    return any(f'{p}-{a}-{s}' in runtime['tags']
               for p in python.split('.') for a in abi.split('.')
               for s in platforms.split('.'))


def reuse(house: Path, requirements: Path, runtime: dict) -> Path | None:
    manifest = house / 'manifest.json'
    if not manifest.is_file():
        return None
    if manifest.is_symlink() or manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('Invalid Python artifact manifest')
    record = json.loads(manifest.read_text())
    if record.get('schema') != 1:
        raise ValueError('Unsupported Python artifact manifest')
    if (record['requirements_sha256'] != requirements_digest(requirements)
            or record['target'] != _target(runtime)):
        return None
    files = record['wheels']
    if not isinstance(files, dict) or len(files) > MAX_MEMBERS:
        raise ValueError('Invalid Python artifact inventory')
    for filename, expected in files.items():
        if Path(filename).name != filename or not filename.endswith('.whl'):
            raise ValueError('Unsafe Python artifact filename')
        artifact = house / filename
        if artifact.is_symlink() or not artifact.is_file() or digest(artifact) != expected:
            raise ValueError('Python artifact hash mismatch; cache preserved for diagnosis')
        if not _compatible(filename, runtime):
            return None
    lock = house / 'install.lock'
    if lock.is_symlink() or not lock.is_file() or digest(lock) != record['lock_sha256']:
        raise ValueError('Python artifact lock hash mismatch')
    # An undeclared extra wheel must never participate in --find-links selection.
    if {p.name for p in house.glob('*.whl')} != set(files):
        raise ValueError('Undeclared Python artifact in cache')
    return lock


def publish(stage: Path, house: Path, requirements: Path, runtime: dict) -> Path:
    record = {'schema': 1, 'requirements_sha256': requirements_digest(requirements),
              'target': _target(runtime), 'lock_sha256': digest(stage / 'install.lock'),
              'wheels': {p.name: digest(p) for p in sorted(stage.glob('*.whl'))}}
    (stage / 'manifest.json').write_text(json.dumps(record, sort_keys=True) + '\n')
    if reuse(stage, requirements, runtime) is None:
        raise ValueError('Python wheelhouse is incompatible with its target')
    return _replace(stage, house)


def _replace(stage: Path, house: Path) -> Path:
    if house.is_symlink():
        raise ValueError('Refusing a symlinked Python artifact cache')
    if house.exists() and (not house.is_dir()
                           or (any(house.iterdir()) and not (house / 'manifest.json').is_file())):
        raise ValueError('Refusing to replace an unowned Python artifact directory')
    # The backup has a task-owned unique name, never a user path.
    with tempfile.TemporaryDirectory(prefix='.previous-wheels-', dir=house.parent) as temporary:
        backup = Path(temporary) / 'wheelhouse'
        if house.exists():
            house.rename(backup)
        try:
            stage.rename(house)
        except BaseException:
            if backup.exists():
                backup.rename(house)
            raise
    return house / 'install.lock'


def import_bundle(archive: Path, checksum: Path, house: Path,
                  requirements: Path, runtime: dict) -> Path | None:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES or checksum.stat().st_size > 4096:
        raise ValueError('Python bundle exceeds download budget')
    match = re.fullmatch(r'([0-9a-f]{64})\s+\*?([^\r\n]+)', checksum.read_text().strip())
    if not match or match[2] != archive.name or digest(archive) != match[1]:
        raise ValueError('Python bundle checksum mismatch')
    house.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.import-wheels-', dir=house.parent) as temporary:
        stage = Path(temporary) / 'wheelhouse'
        stage.mkdir()
        seen = set()
        total = 0
        with tarfile.open(archive, 'r:gz') as bundle:
            for entry in bundle:
                if (entry.name in seen or Path(entry.name).name != entry.name
                        or '\\' in entry.name or not entry.isfile()
                        or not (entry.name.endswith('.whl')
                                or entry.name in ('manifest.json', 'install.lock'))):
                    raise ValueError('Unsafe or duplicate Python bundle member')
                seen.add(entry.name)
                total += entry.size
                if len(seen) > MAX_MEMBERS or total > MAX_UNPACKED_BYTES:
                    raise ValueError('Python bundle exceeds extraction budget')
                if entry.name in ('manifest.json', 'install.lock') and entry.size > MAX_MANIFEST_BYTES:
                    raise ValueError('Python bundle metadata exceeds budget')
                source = bundle.extractfile(entry)
                if source is None:
                    raise ValueError('Unreadable Python bundle member')
                with source, (stage / entry.name).open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        lock = reuse(stage, requirements, runtime)
        if lock is None:
            return None
        return _replace(stage, house)


def package(house: Path, requirements: Path, runtime: dict,
            version: str, out: Path) -> Path:
    if reuse(house, requirements, runtime) is None:
        raise ValueError('Cannot publish an incompatible Python wheelhouse')
    out.mkdir(parents=True, exist_ok=True)
    archive = out / asset_name(version, runtime)
    with tempfile.TemporaryDirectory(prefix='.package-wheels-', dir=out) as temporary:
        pending = Path(temporary) / archive.name
        record = json.loads((house / 'manifest.json').read_text())
        with tarfile.open(pending, 'w:gz') as bundle:
            for filename in sorted([*record['wheels'], 'manifest.json', 'install.lock']):
                info = bundle.gettarinfo(str(house / filename), arcname=filename)
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ''
                with (house / filename).open('rb') as source:
                    bundle.addfile(info, source)
        pending.replace(archive)
    archive.with_name(archive.name + '.sha256').write_text(f'{digest(archive)}  {archive.name}\n')
    return archive


def _download(url: str, dest: Path, limit: int) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        raise ValueError('Python bundle downloads require HTTPS without URL credentials')
    deadline = time.monotonic() + 60
    total = 0
    with urlopen(url, timeout=10) as response, dest.open('xb') as output:
        if urlsplit(response.url).scheme != 'https':
            raise ValueError('Python bundle redirect downgraded HTTPS')
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > limit or time.monotonic() > deadline:
                raise ValueError('Python bundle download exceeded its budget')
            output.write(block)


def release_bundle(house: Path, requirements: Path, runtime: dict) -> Path | None:
    """Explicit local bundles fail closed; absent online releases fall back."""
    local = os.environ.get('TOFU_PYTHON_BUNDLE')
    if local:
        archive = Path(local)
        lock = import_bundle(archive, archive.with_name(archive.name + '.sha256'),
                             house, requirements, runtime)
        if lock is None:
            raise ValueError('Explicit Python bundle does not match source/Python/platform')
        return lock
    if os.environ.get('TOFU_PYTHON_BUNDLE_NO_DOWNLOAD') == '1':
        return None
    root = _source_root(requirements)
    if root is None:
        return None
    version = (root / 'VERSION').read_text().strip()
    name = asset_name(version, runtime)
    base = os.environ.get('TOFU_PYTHON_BUNDLE_BASE_URL') or (
        os.environ.get('TOFU_GH_RELEASE_BASE', 'https://github.com').rstrip('/')
        + f'/rangehow/ToFu/releases/download/v{version}')
    with tempfile.TemporaryDirectory(prefix='.download-wheels-', dir=house.parent) as temporary:
        archive = Path(temporary) / name
        checksum = archive.with_name(name + '.sha256')
        try:
            # Probe the tiny checksum FIRST: absent assets cost no large transfer.
            _download(base.rstrip('/') + '/' + checksum.name, checksum, 4096)
            _download(base.rstrip('/') + '/' + name, archive, MAX_ARCHIVE_BYTES)
        except (OSError, ValueError):
            print('Release Python bundle unavailable; preparing artifacts locally.', file=sys.stderr)
            return None
        return import_bundle(archive, checksum, house, requirements, runtime)
