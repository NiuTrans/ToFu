#!/usr/bin/env python3
"""Install a verified prebuilt tofu-db engine archive (install.sh fast path).

WHY THIS SCRIPT EXISTS
----------------------
Compiling tofu-db on the user's machine was the standalone installer's only
compile step: minutes of ``cargo build --release`` that needs rustup plus a
modern C toolchain, and the most common accelerant failure on old hosts.
Releases now publish per-platform prebuilt engine archives
(``tofu-db-<version>-<rust-triple>.tar.gz`` — musl-static on Linux, so the
binary has no glibc floor). This script is the verification + installation
boundary that lets ``install.sh`` drop one in with the same fail-closed
discipline as the frontend-dist installer:

  * the ``<archive>.sha256`` sidecar must name exactly the downloaded
    archive and match its SHA-256 (transport integrity);
  * the archive must contain exactly one regular file — the engine binary —
    within the size budget the supervisor's pin verification enforces, with
    no links, devices, absolute paths or ``..`` components;
  * the extracted binary is deployed through
    ``scripts/deploy_tofudb_engine.py``, the ONE supported engine deployment
    path, which smoke-runs the candidate (proving it executes on THIS host),
    installs it atomically with a backup of any previous artifact, and
    writes the ``<binary>.sha256`` launch pin the supervisor verifies on
    every start.

Any failure is a refusal (exit 1) and nothing is installed; ``install.sh``
then falls back to a source build, preserving the accelerant-never-a-gate
contract. Stdlib-only; performs no network access itself.

Usage:
  install_tofudb_prebuilt.py ARCHIVE CHECKSUM DEST
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import deploy_tofudb_engine  # noqa: E402  (same-dir import after sys.path setup)

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_CHECKSUM_BYTES = 4096
ENGINE_NAMES = {'tofu-db', 'tofu-db.exe'}


class RefusalError(RuntimeError):
    """An actionable, fail-closed installation refusal."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_checksum(archive: Path, checksum: Path) -> None:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES or checksum.stat().st_size > MAX_CHECKSUM_BYTES:
        raise RefusalError('tofu-db download exceeds its size budget')
    check = checksum.read_text(encoding='utf-8').strip()
    match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?([^\r\n]+)', check)
    if not match or match[2] != archive.name:
        raise RefusalError('tofu-db checksum must name exactly the downloaded archive')
    if _sha256_file(archive) != match[1].lower():
        raise RefusalError('tofu-db checksum mismatch; refusing to install a modified archive')


def _extract_engine(archive: Path, stage: Path) -> Path:
    """Extract the single engine binary, rejecting anything else."""
    found: Path | None = None
    with tarfile.open(archive, 'r:gz') as bundle:
        for entry in bundle:
            name = PurePosixPath(entry.name)
            parts = name.parts
            if parts and parts[0] == '.':
                parts = parts[1:]
            if not parts:
                continue
            if entry.isdir():
                continue
            if (name.is_absolute() or '..' in parts or '\\' in entry.name
                    or not entry.isfile()):
                raise RefusalError(f'unsafe entry in tofu-db archive: {entry.name!r}')
            if len(parts) != 1 or parts[0] not in ENGINE_NAMES or found is not None:
                raise RefusalError(
                    'tofu-db archive must contain exactly one engine binary, '
                    f'found unexpected entry {entry.name!r}')
            if entry.size > deploy_tofudb_engine.MAX_ENGINE_BYTES:
                raise RefusalError('tofu-db engine exceeds the pin verification size budget')
            source = bundle.extractfile(entry)
            if source is None:
                raise RefusalError(f'tofu-db archive entry {entry.name!r} is unreadable')
            found = stage / parts[0]
            with source, found.open('xb') as output:
                remaining = entry.size
                while remaining > 0:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise RefusalError('tofu-db archive entry is truncated')
                    output.write(block)
                    remaining -= len(block)
    if found is None:
        raise RefusalError('tofu-db archive contains no engine binary')
    found.chmod(0o755)
    return found


def stage_verified_archive(archive: Path, checksum: Path, staging: Path) -> Path:
    """Verify and unpack without publishing a binary or changing configuration."""
    _verify_checksum(archive, checksum)
    staging.mkdir(parents=True, exist_ok=True)
    return _extract_engine(archive, staging)


def install(archive: Path, checksum: Path, dest: Path) -> dict:
    _verify_checksum(archive, checksum)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.tofudb-prebuilt-', dir=str(dest.parent)) as staged:
        candidate = _extract_engine(archive, Path(staged))
        plan = deploy_tofudb_engine.deploy(argparse.Namespace(
            binary=str(candidate),
            dest=str(dest),
            evidence=None,
            note=f'prebuilt archive {archive.name} installed by install.sh',
            env_file=None,
            no_env_sync=False,
            no_smoke=False,
            dry_run=False,
            output=None,
        ))
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('archive', help='downloaded tofu-db-<version>-<triple>.tar.gz')
    parser.add_argument('checksum', help='SHA-256 sidecar naming the archive')
    parser.add_argument('dest', help='installed engine path (packages/tofu-db/target/release/tofu-db)')
    arguments = parser.parse_args(argv)
    archive = Path(arguments.archive)
    dest = Path(arguments.dest).expanduser()
    try:
        plan = install(archive, Path(arguments.checksum), dest)
    except (RefusalError, deploy_tofudb_engine.DeployError, OSError, tarfile.TarError) as exc:
        print(f'install_tofudb_prebuilt: REFUSED: {exc}', file=sys.stderr)
        return 1
    print(f'install_tofudb_prebuilt: installed {plan["dest"]} '
          f'(sha256 {plan["binary_sha256"]})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
