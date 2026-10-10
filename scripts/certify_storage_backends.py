#!/usr/bin/env python3
"""Certify one explicit native executable at wire, application and browser seams.

Uses disposable pytest authorities; never starts or migrates the user's store.
Dependencies: pytest, the repository's browser test dependencies, a built engine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(binary: Path) -> str:
    with binary.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verification_sources() -> dict[str, str]:
    """Fingerprint retained application and verification inputs, including WIP."""
    names = subprocess.check_output([
        'git', 'ls-files', '--cached', '--others', '--exclude-standard', '--',
        'lib', 'tofu_db', 'tofu_agent', 'tests', 'frontend/src', 'contracts',
        'routes', 'server', 'templates', 'static', 'app.py', 'vite.config.mjs',
        'package.json', 'package-lock.json', 'pyproject.toml',
        'scripts/certify_storage_backends.py', 'scripts/tofudb_perf_compare.py',
        'scripts/audit_storage_logs.py', 'scripts/gen_tofudb_ir_contract.py',
        'scripts/gen_storage_operation_catalog.py'], cwd=ROOT, text=True).splitlines()
    # Browser tests execute generated assets, including ignored Vite outputs.
    # Pin those bytes as well as retained sources and HTTP request handlers.
    names.extend(subprocess.check_output([
        'git', 'ls-files', '--others', '--ignored', '--exclude-standard', '--',
        'static/vite'], cwd=ROOT, text=True).splitlines())
    names.extend(['scripts/generated_storage_operations.py',
                  'lib/storage_sidecar/generated_storage_receipts.py'])
    return {name: fingerprint(ROOT / name) for name in sorted(set(names))
            if (ROOT / name).is_file()}


def validate_evidence(data: dict) -> int:
    """Every collected case must pass setup, call and teardown exactly once."""
    nodes = data.get('collected', [])
    if (not nodes or len(set(nodes)) != len(nodes) or data.get('deselected')
            or data.get('pytest_exitstatus') != 0):
        raise ValueError('empty, duplicate, deselected or failed test collection')
    phases = {node: [] for node in nodes}
    for report in data.get('reports', []):
        if (report['nodeid'] not in phases or report['outcome'] != 'passed'
                or report.get('xfail')):
            raise ValueError('failed, skipped, xfailed or unexpected test report')
        phases[report['nodeid']].append(report['when'])
    if any(sorted(value) != ['call', 'setup', 'teardown'] for value in phases.values()):
        raise ValueError('missing or duplicate setup/call/teardown evidence')
    return len(nodes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--report-dir', type=Path, help='new directory for retained evidence')
    args = parser.parse_args()
    binary = args.binary.resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('--binary must name an executable tofu-db candidate')
    report_dir = args.report_dir or Path(tempfile.mkdtemp(prefix='tofu-storage-evidence-'))
    if args.report_dir:
        report_dir.mkdir(parents=True, exist_ok=False)
    report_dir = report_dir.resolve()
    print(f'Evidence: {report_dir}', flush=True)
    digest = fingerprint(binary)
    print(f'Certifying {binary}\nSHA256 {digest}', flush=True)
    manifest_path = ROOT / 'contracts/storage_harness_certification_v1.json'
    manifest = json.loads(manifest_path.read_text())
    stages = manifest['stages']
    sources = verification_sources()
    (report_dir / 'verification-sources.json').write_text(json.dumps(sources, indent=2) + '\n')
    with tempfile.TemporaryDirectory(prefix='tofu-storage-certification-') as directory:
        candidate = Path(directory) / binary.name
        shutil.copy2(binary, candidate)
        if fingerprint(candidate) != digest:
            print('Candidate changed while being copied; retry after the build completes.', file=sys.stderr)
            return 1
        candidate.chmod(0o500)
        # The embedded transport resolves libtofu_db.so as the selected
        # binary's sibling; pin the same build snapshot next to the pinned
        # executable, exactly like the perf harness does.
        library = binary.parent / 'libtofu_db.so'
        if library.is_file():
            shutil.copy2(library, candidate.parent / 'libtofu_db.so')
        environment = dict(os.environ, TOFUDB_BIN=str(candidate), TOFUDB_DIFFERENTIAL_BIN=str(candidate))
        # Ambient selection options must not silently shrink a release gate.
        environment.pop('PYTEST_ADDOPTS', None)
        summary = {'binary_sha256': digest, 'stages': [], 'passed': False,
                   'scope': manifest['scope'], 'uncovered_claims': manifest['uncovered_claims'],
                   'manifest_sha256': fingerprint(manifest_path)}
        summary_path = report_dir / 'summary.json'
        summary_path.write_text(json.dumps(summary, indent=2) + '\n')
        failures = False
        for stage in stages:
            name, backend, tests, marker = (stage[key] for key in ('name', 'backend', 'tests', 'marker'))
            report = report_dir / f'{name}.xml'
            evidence = report_dir / f'{name}.json'
            print(f'\nStorage gate: {name}', flush=True)
            try:
                result = subprocess.run(
                    [sys.executable, '-m', 'pytest', *tests, '-m', marker,
                     '-q', '--tb=short', '-p', 'tests.storage_certification_plugin',
                     f'--junitxml={report}'], cwd=ROOT,
                    env=dict(environment, TOFU_PERSONAL_STORAGE_BACKEND=backend,
                             TOFU_STORAGE_CERTIFICATION_EVIDENCE=str(evidence)),
                    timeout=1200,
                )
            except subprocess.TimeoutExpired:
                print(f'{name}: exceeded 1200-second gate budget', file=sys.stderr)
                failures = True
                summary['stages'].append({'name': name, 'status': 'timeout'})
                summary_path.write_text(json.dumps(summary, indent=2) + '\n')
                continue
            if result.returncode:
                failures = True
                summary['stages'].append({'name': name, 'status': 'failed', 'exit_code': result.returncode})
                summary_path.write_text(json.dumps(summary, indent=2) + '\n')
                continue
            try:
                data = json.loads(evidence.read_text())
                count = validate_evidence(data)
                missing = set(stage['required_cases']) - set(data['collected'])
                if missing:
                    raise ValueError(f'missing required harness scenarios: {sorted(missing)}')
            except (ValueError, OSError, KeyError, TypeError) as exc:
                print(f'{name}: invalid certification evidence: {exc}', file=sys.stderr)
                failures = True
                summary['stages'].append({'name': name, 'status': 'invalid-evidence', 'reason': str(exc)})
                summary_path.write_text(json.dumps(summary, indent=2) + '\n')
                continue
            summary['stages'].append({'name': name, 'backend': backend, 'passed': count})
            summary_path.write_text(json.dumps(summary, indent=2) + '\n')
            if fingerprint(binary) != digest or fingerprint(candidate) != digest:
                print('Candidate executable changed during certification; rerun on a stable build.', file=sys.stderr)
                return 1
        current_sources = verification_sources()
        changed = sorted(name for name in sources.keys() | current_sources.keys()
                         if sources.get(name) != current_sources.get(name))
        summary['changed_verification_sources'] = changed
        if changed:
            failures = True
            print('Verification sources changed during the run; no stable-source certification.', file=sys.stderr)
        summary['passed'] = not failures
        summary_path.write_text(json.dumps(summary, indent=2) + '\n')
    print(f'Storage gates {"FAILED" if failures else "passed"} for SHA256 {digest}', flush=True)
    return int(failures)


if __name__ == '__main__':
    raise SystemExit(main())
