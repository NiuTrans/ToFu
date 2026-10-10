#!/usr/bin/env python3
"""Install a checked release frontend without Node or destructive extraction.

Verify the SHA-256 sidecar and bounded archive into a private same-volume
staging directory, reuse lib.vite_assets for graph/source validation, and only
then replace static/vite. Failed validation never touches the previous graph;
failed publication restores it. Called by install.sh after Python setup.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACT_BYTES = 512 * 1024 * 1024
MAX_FILES = 10000


def install_archive(root: Path, archive: Path, checksum: Path, *, validate=None) -> None:
    """Verify before publication; do not extract links, devices or other paths."""
    if archive.stat().st_size > MAX_ARCHIVE_BYTES or checksum.stat().st_size > 4096:
        raise ValueError('Frontend download exceeds its size budget')
    check = checksum.read_text(encoding='utf-8').strip()
    match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?([^\r\n]+)', check)
    if not match or match[2] != archive.name:
        raise ValueError('Frontend checksum must name exactly the downloaded archive')
    digest = hashlib.sha256()
    with archive.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != match[1].lower():
        raise ValueError('Frontend checksum mismatch; previous frontend preserved')

    static = root / 'static'
    target = static / 'vite'
    if static.is_symlink() or target.is_symlink():
        raise ValueError('Refusing to replace a symlinked frontend directory')
    static.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.tofu-frontend-', dir=static) as temp:
        stage = Path(temp)
        total = 0
        seen: set[str] = set()
        with tarfile.open(archive, 'r:gz') as bundle:
            for number, entry in enumerate(bundle, 1):
                path = PurePosixPath(entry.name)
                if (number > MAX_FILES or entry.name in seen or '\\' in entry.name
                        or path.is_absolute() or '..' in path.parts
                        or path.parts[:2] != ('static', 'vite')
                        or not (entry.isdir() or entry.isfile())):
                    raise ValueError('Unsafe or oversized frontend archive')
                seen.add(entry.name)
                total += entry.size
                if total > MAX_EXTRACT_BYTES:
                    raise ValueError('Frontend extraction exceeds its size budget')
                destination = stage.joinpath(*path.parts)
                if entry.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(entry)
                    if source is None:
                        raise ValueError('Frontend archive entry is unreadable')
                    with source, destination.open('xb') as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    destination.chmod(0o644)
        candidate = stage / 'static' / 'vite'
        if validate is None:
            # The isolated installer process points the canonical validator at
            # staged assets while retaining the real checkout's authoring inputs.
            sys.path.insert(0, str(ROOT))
            from lib import vite_assets
            original = vite_assets.VITE_OUT_DIR, vite_assets.VITE_MANIFEST
            try:
                vite_assets.VITE_OUT_DIR = str(candidate)
                vite_assets.VITE_MANIFEST = str(candidate / 'manifest.json')
                vite_assets.validate_source_vite_artifact()
            finally:
                vite_assets.VITE_OUT_DIR, vite_assets.VITE_MANIFEST = original
        else:
            validate(candidate)
        backup = stage / 'previous'
        had_previous = target.exists()
        if had_previous:
            os.replace(target, backup)
        try:
            os.replace(candidate, target)
        except BaseException:
            if had_previous:
                os.replace(backup, target)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('checksum', type=Path)
    args = parser.parse_args()
    try:
        install_archive(ROOT, args.archive, args.checksum)
    except (OSError, ValueError, tarfile.TarError, RuntimeError) as exc:
        print(f'Frontend install failed: {exc}', file=sys.stderr)
        return 1
    print('Verified frontend published; temporary extraction removed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
