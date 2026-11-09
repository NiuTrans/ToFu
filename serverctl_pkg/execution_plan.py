"""Execution generation identity; no application boot or durable-state access.

The headless host uses the existing pinned-import source discovery owner. Python
source, the interpreter environment's installs (the runtime replica's environment
fingerprint) and effective launch policy determine replacement; frontend artifacts
do not. Digests disclose no launch secrets. A plan is admission evidence, not a
copied source tree or an atomic snapshot of non-Python application resources.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

PLAN_FORMAT = 'tofu.execution-plan/v1'
SOURCE_FIELDS = frozenset({'sourceSha256', 'sourceFiles'})
PLAN_FIELDS = SOURCE_FIELDS | {'format', 'python', 'environmentFingerprint', 'configurationSha256'}
SOURCE_CHANGED_BEFORE_BOOT = 'source_changed_before_boot'


class ExecutionSourceChanged(RuntimeError):
    """Only executable source changed; no new launch policy is admitted."""


def source_only_change(expected: dict, actual: dict) -> bool:
    """A complete, same-policy plan may refresh source before application entry."""
    if (set(expected) != PLAN_FIELDS or set(actual) != PLAN_FIELDS
            or expected.get('format') != PLAN_FORMAT):
        return False
    changed = {key for key in PLAN_FIELDS if expected[key] != actual[key]}
    return bool(changed) and changed <= SOURCE_FIELDS


def require_plan_match(expected: dict, actual: dict, *, stage: str) -> None:
    if expected == actual:
        return
    changed = sorted(key for key in expected.keys() | actual.keys()
                     if key not in expected or key not in actual or expected[key] != actual[key])
    error = ExecutionSourceChanged if source_only_change(expected, actual) else RuntimeError
    # Field names identify the mismatch without exposing environment values.
    raise error(f'Execution generation changed {stage}: {", ".join(changed)}')


def prepare_plan(project: Path, environment: dict[str, str], *, python: str = sys.executable) -> dict:
    from lib.runtime.pinned_import_source import _importable_sources, source_identity
    from lib.storage.storage_boundary import require_exclusive_sidecar_boundary
    from serverctl_pkg.runtime_replica import describe_environment, fingerprint

    project = project.resolve()
    # The same startup authority must refuse an invalid candidate while the old
    # application is still serving, not first after replacement has drained it.
    require_exclusive_sidecar_boundary(project)
    roots = [project]
    search = Path(environment.get('TOFU_SEARCH_PATH') or project / 'packages/tofu-search')
    if search.is_dir():
        roots.append(search.resolve())
    sources = {Path(path) for root in roots
               for path in _importable_sources(str(root), recurse=True, ignore_errors=False)}

    def read_sources():
        for path in sorted(sources):
            before = path.stat()
            content = path.read_bytes()
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ExecutionSourceChanged(f'Execution source changed during preparation: {path}')
            yield str(path), content
    identity = source_identity(read_sources())
    configuration = json.dumps(environment, sort_keys=True, separators=(',', ':')).encode()
    # Installed packages are executable source too; None when the layout cannot be described.
    installed = describe_environment(python)
    return {'format': PLAN_FORMAT, 'python': python,
            **identity,
            'environmentFingerprint': None if installed is None else fingerprint(installed),
            'configurationSha256': hashlib.sha256(configuration).hexdigest()}
