#!/usr/bin/env python3
"""check_tofudb_engine_drift — prove the deployed engine speaks the current contract.

WHY THIS EXISTS (root cause): the supervisor content pin
(``lib/storage/supervisor.py``) proves the deployed binary is *the artifact
that was certified*; it cannot prove the artifact still implements the
*current* application storage contract. A certified engine survived a later
contract change (``system.reclaim`` gained the offline boundary verdict in
``docs/STORAGE_TOFUDB.md``) and the deployment kept serving the stale semantics
until the application contract suite was run by hand. This tool closes that
gap behaviorally: it resolves the exact binary the application would launch,
starts it against a disposable authority, and replays the small probe set that
pins contract surfaces which have drifted or are structurally drift-prone.
Hash pins and ``build.json`` provenance are metadata; only a live semantic
round-trip detects semantic drift.

Resolution order matches the running application: project ``.env`` values sit
*under* the process environment (same merge as the app), then
``lib/storage/supervisor.py``'s single resolver applies
``TOFUDB_BIN`` → bundled → source release build → ``PATH``, including its pin
verification. ``--binary`` skips deployment resolution for certification of a
candidate before deploy (pair with ``scripts/deploy_tofudb_engine.py``).

Read-only towards user data: the probe authority lives in a fresh temporary
directory and is deleted on exit; the real data root is never opened.

Usage:
  check_tofudb_engine_drift.py [--binary PATH] [--json] [--startup-timeout S]

Exit codes: 0 = engine satisfies the probed current contract;
1 = semantic drift or unhealthy engine; 2 = usage/resolution error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Contract surfaces probed against the live engine. Each entry names the
#: authority document that owns the shape, so a future intentional contract
#: change updates the probe in the same commit as the document.
ONLINE_RECLAIM_CONTRACT = 'docs/STORAGE_TOFUDB.md#online-reclaim-boundary'

_HASH_CHUNK_BYTES = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b''):
            digest.update(chunk)
    return digest.hexdigest()


class _deployment_environment:
    """Temporarily overlay the project's dotenv under the process environment.

    The supervisor resolver and engine launch read ``os.environ`` directly;
    the application's own merge order (dotenv first, process environment wins)
    is reproduced so the checked binary is the one a real restart would pick.
    """

    def __init__(self, repo_root: Path):
        from tofu_dotenv import read_dotenv_values
        dotenv = read_dotenv_values(repo_root / '.env')
        self._merged = {**dotenv, **os.environ}
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self._merged.items():
            if os.environ.get(key) != value:
                self._saved[key] = os.environ.get(key)
                os.environ[key] = value
        return self._merged

    def __exit__(self, *_):
        for key, previous in self._saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self._saved.clear()


def resolve_deployment_binary(repo_root: Path) -> str:
    """Resolve the engine exactly like an application restart would."""
    from lib.storage.supervisor import _resolve_tofudb_binary
    with _deployment_environment(repo_root):
        return _resolve_tofudb_binary()


def reclaim_boundary_verdict(result) -> list[str]:
    """Pin the online-reclaim boundary from ``docs/STORAGE_TOFUDB.md``.

    Physical GC is deferred to the exclusive offline operator boundary, so the
    foreground authority must answer with the exact deferred capability shape;
    an engine built before that contract (or a SQL authority answering the
    native probe) violates every clause below.
    """
    if not isinstance(result, dict):
        return ['system.reclaim result is not an object']
    violations = []
    # Current contract (docs/STORAGE_TOFUDB.md#online-reclaim-boundary): the
    # two-mark online collector serves system.reclaim — offline_required is
    # false and no exclusive offline GC reason code is produced.
    expectations = {
        'backend': 'tofu-db',
        'offline_required': False,
        'reason_code': None,
        'statistics_complete': False,
        'reclaimed': 0,
        'reclaimed_bytes': 0,
        'more_candidates': False,
        'epoch_in_progress': False,
        'epochs_completed': 0,
    }
    for field, expected in expectations.items():
        actual = result.get(field)
        if actual != expected or type(actual) is not type(expected):
            violations.append(
                f'system.reclaim.{field}: expected {expected!r}, got {actual!r}')
    if result.get('budget_ms') != 10 or type(result.get('budget_ms')) is not int:
        violations.append(
            f"system.reclaim.budget_ms must echo the request budget, got {result.get('budget_ms')!r}")
    return violations


def _probe_engine(binary: str, *, startup_timeout: float) -> list[dict]:
    """Run the contract probe set against a disposable supervised authority."""
    # --binary must be the artifact actually exercised, not merely the path
    # printed in the report. Never attach to an inherited live authority.
    overrides = {'TOFUDB_BIN': binary,
                 'TOFUDB_EXPECTED_SHA256': _sha256_file(Path(binary)),
                 'TOFUDB_TRANSPORT': 'daemon',
                 'TOFU_STORAGE_CONNECTION_FILE': ''}
    saved = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        return _probe_selected_engine(startup_timeout=startup_timeout)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _probe_selected_engine(*, startup_timeout: float) -> list[dict]:
    from lib.storage import StorageRuntime, StorageSupervisor

    probes: list[dict] = []
    with tempfile.TemporaryDirectory(prefix='tofudb-drift-') as workdir:
        supervisor = StorageSupervisor(
            project_root=Path(workdir), backend='tofudb',
            startup_timeout=startup_timeout)
        runtime = StorageRuntime(supervisor, auto_restart=False)
        try:
            runtime.start()
            client = runtime.client()

            version = client.query('system.schema_version', {})
            ok = isinstance(version, dict) and type(version.get('version')) is int \
                and version['version'] > 0
            probes.append({
                'probe': 'semantic_roundtrip',
                'contract': 'contracts/storage_operations_v1.json',
                'ok': ok,
                'detail': f'system.schema_version -> {version!r}' if not ok
                          else f"schema version {version['version']}",
            })

            reclaim = client.command('system.reclaim', {
                'max_pages': 32, 'min_free_pages': 0, 'budget_ms': 10},
                'drift-probe-reclaim')
            violations = reclaim_boundary_verdict(reclaim)
            probes.append({
                'probe': 'online_reclaim_boundary',
                'contract': ONLINE_RECLAIM_CONTRACT,
                'ok': not violations,
                'detail': '; '.join(violations) if violations
                          else 'deferred-GC boundary shape intact',
            })

            conv_id = 'drift-probe-conversation'
            client.command('conversation.create', {
                'conv_id': conv_id, 'title': 'drift probe', 'settings': {}},
                'drift-probe-conversation-create')
            document = client.query('conversation.get', {'conv_id': conv_id})
            metadata = (document or {}).get('metadata') or {}
            count = client.query('conversation.count', {})
            count_value = count.get('count') if isinstance(count, dict) else None
            ok = metadata.get('id') == conv_id \
                and metadata.get('title') == 'drift probe' \
                and type(count_value) is int and count_value >= 1
            probes.append({
                'probe': 'owner_scoped_write_read',
                'contract': 'docs/STORAGE.md#conversation-data',
                'ok': ok,
                'detail': 'create/get/count round-trip intact' if ok
                          else f'get -> {metadata!r}; count -> {count!r}',
            })
        finally:
            runtime.stop()
    return probes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Prove the deployed tofu-db engine speaks the current '
                    'application storage contract.')
    parser.add_argument('--binary', type=Path, default=None,
                        help='explicit candidate engine; default resolves the '
                             'deployment binary exactly like the application')
    parser.add_argument('--json', action='store_true',
                        help='machine-readable report')
    parser.add_argument('--startup-timeout', type=float, default=60.0,
                        help='engine readiness budget in seconds (default 60)')
    args = parser.parse_args(argv)

    try:
        if args.binary is not None:
            binary = str(args.binary.expanduser().resolve())
            if not (Path(binary).is_file() and os.access(binary, os.X_OK)):
                raise RuntimeError(
                    f'--binary must name an executable tofu-db binary: {binary}')
        else:
            binary = resolve_deployment_binary(REPO_ROOT)
    except (RuntimeError, ValueError) as exc:
        message = f'cannot resolve the deployment engine: {exc}'
        if args.json:
            print(json.dumps({'ok': False, 'error': message}))
        else:
            print(f'error: {message}', file=sys.stderr)
        return 2

    digest = _sha256_file(Path(binary))
    provenance = {}
    build_record = Path(binary).parent / 'build.json'
    if build_record.is_file():
        try:
            record = json.loads(build_record.read_text(encoding='utf-8'))
            provenance = {key: record.get(key) for key in
                          ('source_head', 'deployment_utc', 'binary_sha256')
                          if record.get(key)}
        except (OSError, ValueError):
            provenance = {'build.json': 'unreadable'}

    try:
        probes = _probe_engine(binary, startup_timeout=args.startup_timeout)
    except Exception as exc:  # launch/transport failure: the engine is unhealthy
        probes = [{'probe': 'engine_launch', 'contract': 'docs/STORAGE_TOFUDB.md',
                   'ok': False, 'detail': f'{type(exc).__name__}: {exc}'}]

    ok = all(probe['ok'] for probe in probes)
    report = {'ok': ok, 'binary': binary, 'sha256': digest,
              'provenance': provenance, 'probes': probes}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f'engine:   {binary}')
        print(f'sha256:   {digest}')
        for key, value in provenance.items():
            print(f'{key}:{" " * (12 - len(key))}{value}')
        for probe in probes:
            marker = 'PASS' if probe['ok'] else 'FAIL'
            print(f"  {marker} {probe['probe']} ({probe['contract']})")
            print(f'       {probe["detail"]}')
        print('drift check: ' + ('IN SYNC' if ok else 'DRIFT DETECTED'))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
