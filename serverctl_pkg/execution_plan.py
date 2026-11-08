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

def prepare_plan(project: Path, environment: dict[str, str], *, python: str = sys.executable) -> dict:
    from lib.runtime.pinned_import_source import _importable_sources, source_identity
    from serverctl_pkg.runtime_replica import describe_environment, fingerprint

    project = project.resolve()
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
                raise RuntimeError(f'Execution source changed during preparation: {path}')
            yield str(path), content
    identity = source_identity(read_sources())
    configuration = json.dumps(environment, sort_keys=True, separators=(',', ':')).encode()
    # Installed packages are executable source too; None when the layout cannot be described.
    installed = describe_environment(python)
    return {'format': 'tofu.execution-plan/v1', 'python': python,
            **identity,
            'environmentFingerprint': None if installed is None else fingerprint(installed),
            'configurationSha256': hashlib.sha256(configuration).hexdigest()}
