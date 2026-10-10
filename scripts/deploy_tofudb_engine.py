#!/usr/bin/env python3
"""Deploy a verified tofu-db engine binary together with its content pin.

WHY THIS EXISTS (root cause): the supervisor drift check
(``lib/storage/supervisor.py``) refuses to spawn an engine whose SHA-256
differs from its declared pin. When the artifact and the pin live in separate
places and are updated by hand — the binary under
``packages/tofu-db/target/certified/``, the pin in the project ``.env`` — any
out-of-band upgrade desynchronizes them and the next restart fails closed in
a crashloop. This tool is the ONE supported upgrade path. In a single
fail-closed, atomic sequence it:

  * preflights the candidate (executable, size budget, smoke run),
  * installs the binary beside the destination via tmp+rename, preserving the
    previous artifact as ``<name>.bak-YYYYMMDD``,
  * writes the ``<binary>.sha256`` sidecar pin the supervisor verifies, so
    the pin travels with the artifact through every later upgrade,
  * records provenance in ``build.json`` (source HEAD, previous pin,
    deployment time, operator-supplied evidence),
  * rewrites a stale ``TOFUDB_EXPECTED_SHA256`` line in the project ``.env``
    in the same run, because the env pin outranks the sidecar and a stale one
    would recreate the crashloop,
  * re-verifies every written state before reporting success.

Pin-only re-deployment (``--binary`` and ``--dest`` naming the same file)
refreshes the sidecar/provenance without touching the artifact; that is the
remedy the supervisor's mismatch error prints.

Stdlib-only; never runs certification itself and never manufactures evidence.

Usage:
  deploy_tofudb_engine.py --binary path/to/candidate [--dest DIR_OR_FILE]
      [--evidence PATH] [--note TEXT] [--env-file PATH | --no-env-sync]
      [--no-smoke] [--dry-run] [--output binding.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_DEST = REPO_ROOT / 'packages/tofu-db/target/certified'
ENV_PIN_KEY = 'TOFUDB_EXPECTED_SHA256'
ENV_BIN_KEY = 'TOFUDB_BIN'

# Mirrors lib/storage/supervisor.py MAX_PINNED_TOFUDB_BINARY_BYTES: a pinned
# engine larger than this can never launch, so deploying it would be useless.
MAX_ENGINE_BYTES = 512 * 1024 * 1024
SMOKE_TIMEOUT_S = 10.0
HASH_CHUNK_BYTES = 1024 * 1024


class DeployError(RuntimeError):
    """An actionable, fail-closed deployment refusal."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes, mode: int | None = None) -> None:
    """Write ``content`` to ``path`` atomically (tmp+fsync+rename, fsync dir)."""
    descriptor, tmp_name = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            tmp.chmod(mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _smoke_check(candidate: Path) -> None:
    """Prove the candidate executes and identifies as tofu-db, bounded in time."""
    try:
        result = subprocess.run(
            [str(candidate)], capture_output=True, timeout=SMOKE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeployError(
            f'candidate did not terminate within {SMOKE_TIMEOUT_S}s on a no-arg run') from exc
    except OSError as exc:
        raise DeployError(f'candidate cannot execute: {exc}') from exc
    output = (result.stdout or b'') + (result.stderr or b'')
    if b'tofu-db' not in output:
        raise DeployError(
            'candidate smoke run printed nothing identifying it as tofu-db '
            f'(exit={result.returncode}); refusing to deploy an unidentified engine')


def _preflight_candidate(candidate: Path, *, smoke: bool) -> str:
    if not candidate.is_file():
        raise DeployError(f'candidate {candidate} is not a file')
    if not os.access(candidate, os.X_OK):
        raise DeployError(f'candidate {candidate} is not executable')
    size = candidate.stat().st_size
    if size > MAX_ENGINE_BYTES:
        raise DeployError(
            f'candidate {candidate} exceeds the {MAX_ENGINE_BYTES}-byte pin verification budget')
    if smoke:
        _smoke_check(candidate)
    return _sha256_file(candidate)


def _resolve_dest(raw: str | None, binary_name: str) -> Path:
    if raw is None:
        return DEFAULT_DEST / binary_name
    dest = Path(raw).expanduser()
    if dest.is_dir() or raw.endswith(('/', os.sep)):
        return dest / binary_name
    return dest


def _backup_path(dest: Path, today: str) -> Path:
    backup = dest.with_name(f'{dest.name}.bak-{today}')
    counter = 2
    while backup.exists():
        backup = dest.with_name(f'{dest.name}.bak-{today}-{counter}')
        counter += 1
    return backup


def _read_previous_pin(dest: Path) -> str | None:
    sidecar = dest.with_name(dest.name + '.sha256')
    try:
        fields = sidecar.read_text(encoding='ascii').split()
        if fields and len(fields[0]) == 64:
            return fields[0].lower()
    except (OSError, ValueError):
        pass
    record = dest.parent / 'build.json'
    try:
        fields = json.loads(record.read_text(encoding='utf-8'))
        previous = fields.get('binary_sha256')
        if isinstance(previous, str) and len(previous) == 64:
            return previous.lower()
    except (OSError, ValueError):
        pass
    return None


def _source_head() -> str | None:
    try:
        head = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not head:
            return None
        dirty = subprocess.run(
            ['git', 'status', '--porcelain', '--', 'packages/tofu-db'], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return head + ('-dirty' if dirty else '')
    except (OSError, subprocess.TimeoutExpired):
        return None


def _update_build_record(dest: Path, digest: str, previous: str | None,
                         evidence: str | None, note: str | None, now: str) -> Path:
    record = dest.parent / 'build.json'
    fields: dict = {}
    try:
        loaded = json.loads(record.read_text(encoding='utf-8'))
        if isinstance(loaded, dict):
            fields = loaded
    except (OSError, ValueError):
        pass
    fields.setdefault('created_utc', now)
    fields['binary_sha256'] = digest
    if previous and previous != digest:
        fields['previous_binary_sha256'] = previous
    elif 'previous_binary_sha256' not in fields:
        fields['previous_binary_sha256'] = previous
    fields['deployment_utc'] = now
    head = _source_head()
    if head:
        fields['deployment_checkout_head'] = head
    # A deployment checkout cannot attest to an arbitrary candidate's source.
    fields.pop('source_head', None)
    fields.pop('source_sha256', None)
    from serverctl_pkg.native_engine import build_identity, EnginePreparationError
    try:
        fields['source_sha256'] = build_identity(dest)['source_sha256']
    except (EnginePreparationError, OSError):
        pass  # Legacy artifacts have no compiled identity; startup rejects them in a current source install.
    library = dest.parent / 'libtofu_db.so'
    fields.pop('library_sha256', None)
    if library.is_file():
        fields['library_sha256'] = _sha256_file(library)
    if evidence:
        fields['evidence'] = evidence
    if note:
        fields['certification'] = note
    _atomic_write(record, (json.dumps(fields, indent=2, sort_keys=True) + '\n').encode('utf-8'))
    return record


def _sync_env_file(env_file: Path, digest: str, dest: Path) -> tuple[bool, list[str]]:
    """Rewrite a declared env pin to the deployed digest; never create the file.

    The env pin outranks the sidecar at launch, so leaving a stale one behind
    would shadow the freshly deployed sidecar and recreate the mismatch
    crashloop on the next restart.
    """
    warnings: list[str] = []
    if not env_file.is_file():
        return False, warnings
    lines = env_file.read_text(encoding='utf-8').splitlines(keepends=True)
    pin_seen = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(ENV_PIN_KEY + '='):
            newline = '\n' if line.endswith('\n') else ''
            lines[index] = f'{ENV_PIN_KEY}={digest}{newline}'
            pin_seen = True
        elif stripped.startswith(ENV_BIN_KEY + '='):
            pinned = stripped.split('=', 1)[1].strip().strip('"').strip("'")
            if pinned and Path(pinned).expanduser() != dest:
                warnings.append(
                    f'{env_file}: {ENV_BIN_KEY} points at {pinned}, not the deployed {dest}; '
                    'the deployment does not affect the running engine until these agree')
    if not pin_seen:
        return False, warnings
    mode = stat.S_IMODE(env_file.stat().st_mode)
    _atomic_write(env_file, ''.join(lines).encode('utf-8'), mode=mode)
    return True, warnings


def _verify_installed(dest: Path, digest: str) -> None:
    if _sha256_file(dest) != digest:
        raise DeployError(f'postcondition failed: installed {dest} does not match {digest}')
    sidecar = dest.with_name(dest.name + '.sha256')
    fields = sidecar.read_text(encoding='ascii').split()
    if not fields or fields[0] != digest:
        raise DeployError(f'postcondition failed: sidecar {sidecar} does not pin {digest}')


def deploy(arguments: argparse.Namespace) -> dict:
    candidate = Path(arguments.binary).expanduser().resolve()
    dest = _resolve_dest(arguments.dest, candidate.name).resolve()
    digest = _preflight_candidate(candidate, smoke=not arguments.no_smoke)
    same_file = candidate == dest
    previous = _read_previous_pin(dest)
    now = datetime.now(timezone.utc).isoformat()
    env_file = Path(arguments.env_file).expanduser() if arguments.env_file else REPO_ROOT / '.env'

    plan = {
        'candidate': str(candidate),
        'dest': str(dest),
        'binary_sha256': digest,
        'previous_binary_sha256': previous,
        'pin_only': same_file,
        'dry_run': bool(arguments.dry_run),
    }
    if arguments.dry_run:
        return plan

    dest.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if not same_file:
        if dest.exists():
            backup = _backup_path(dest, now[:10].replace('-', ''))
            shutil.copy2(dest, backup)
        descriptor, tmp_name = tempfile.mkstemp(prefix=dest.name + '.', dir=str(dest.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(descriptor, 'wb') as handle, candidate.open('rb') as source:
                shutil.copyfileobj(source, handle, HASH_CHUNK_BYTES)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.chmod(0o755)
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _fsync_dir(dest.parent)
        # The embedded in-process transport (cdylib) is part of the engine
        # pair: it loads libtofu_db.so from the daemon's directory, so the
        # deployment carries the sibling when the candidate has one.
        cdylib = candidate.parent / 'libtofu_db.so'
        if cdylib.is_file():
            lib_dest = dest.parent / cdylib.name
            # Never truncate an mmap'ed library used by a live embedded engine.
            descriptor, lib_name = tempfile.mkstemp(prefix=lib_dest.name + '.', dir=dest.parent)
            try:
                with os.fdopen(descriptor, 'wb') as output, cdylib.open('rb') as source:
                    shutil.copyfileobj(source, output, HASH_CHUNK_BYTES)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(lib_name, 0o755)
                os.replace(lib_name, lib_dest)
            finally:
                Path(lib_name).unlink(missing_ok=True)
            _fsync_dir(dest.parent)
        elif (dest.parent / 'libtofu_db.so').exists():
            old_library = dest.parent / 'libtofu_db.so'
            os.replace(old_library, _backup_path(old_library, now[:10].replace('-', '')))

    sidecar = dest.with_name(dest.name + '.sha256')
    _atomic_write(sidecar, f'{digest}  {dest.name}\n'.encode('ascii'))
    record = _update_build_record(
        dest, digest, previous, arguments.evidence, arguments.note, now)

    env_synced = False
    warnings: list[str] = []
    if not arguments.no_env_sync:
        env_synced, warnings = _sync_env_file(env_file, digest, dest)

    _verify_installed(dest, digest)
    if env_synced:
        declared = [
            line.strip().split('=', 1)[1]
            for line in env_file.read_text(encoding='utf-8').splitlines()
            if line.strip().startswith(ENV_PIN_KEY + '=')
        ]
        if declared != [digest]:
            raise DeployError(
                f'postcondition failed: {env_file} declares {declared}, expected [{digest}]')

    plan.update({
        'backup': str(backup) if backup else None,
        'sidecar': str(sidecar),
        'build_record': str(record),
        'env_file': str(env_file) if env_synced else None,
        'warnings': warnings,
    })
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--binary', required=True, help='candidate engine binary to deploy')
    parser.add_argument('--dest',
                        help='installed location (file path, or existing directory; '
                             f'default: {DEFAULT_DEST}/<binary name>)',
                        default=None)
    parser.add_argument('--evidence', help='path to certification evidence recorded in build.json')
    parser.add_argument('--note', help='certification/scope note recorded in build.json')
    parser.add_argument('--env-file', help='dotenv file to sync (default: project .env)')
    parser.add_argument('--no-env-sync', action='store_true',
                        help='do not rewrite a stale TOFUDB_EXPECTED_SHA256 in the dotenv file')
    parser.add_argument('--no-smoke', action='store_true',
                        help='skip the bounded no-arg smoke run of the candidate')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the plan without mutating anything')
    parser.add_argument('--output', help='also write the JSON deployment record to this path')
    arguments = parser.parse_args(argv)
    try:
        record = deploy(arguments)
    except DeployError as exc:
        print(f'deploy_tofudb_engine: REFUSED: {exc}', file=sys.stderr)
        return 1
    except OSError as exc:
        print(f'deploy_tofudb_engine: FAILED: {exc}', file=sys.stderr)
        return 1
    for warning in record.get('warnings', []):
        print(f'deploy_tofudb_engine: WARNING: {warning}', file=sys.stderr)
    rendered = json.dumps(record, indent=2, sort_keys=True)
    print(rendered)
    if arguments.output:
        _atomic_write(Path(arguments.output), (rendered + '\n').encode('utf-8'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
