"""Execution generation identity; no application boot or durable-state access.

The headless host uses the existing pinned-import source discovery owner. Python
source and effective launch policy determine replacement; frontend artifacts do
not. Digests disclose no launch secrets. A plan is admission evidence, not a
copied source tree or an atomic snapshot of non-Python application resources.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

# Application import owners, excluding tests, local vendored code and user data.
APPLICATION_PACKAGES = ('lib', 'routes', 'bootstrap_pkg', 'serverctl_pkg', 'rootless_vm', 'tofu')


def prepare_plan(project: Path, environment: dict[str, str], *, python: str = sys.executable) -> dict:
    from lib.runtime.pinned_import_source import _importable_sources

    project = project.resolve()
    roots = [project]
    search = Path(environment.get('TOFU_SEARCH_PATH') or project / 'packages/tofu-search')
    if search.is_dir():
        roots.append(search.resolve())
    sources = {Path(path) for path in _importable_sources(str(project), recurse=False, ignore_errors=False)}
    for name in APPLICATION_PACKAGES:
        package = project / name
        if (package / '__init__.py').is_file():
            sources.update(Path(path) for path in _importable_sources(str(package), recurse=True, ignore_errors=False))
    for root in roots[1:]:
        sources.update(Path(path) for path in _importable_sources(str(root), recurse=True, ignore_errors=False))

    identity = hashlib.sha256()
    for path in sorted(sources):
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RuntimeError(f'Execution source changed during preparation: {path}')
        name = str(path).encode()
        identity.update(len(name).to_bytes(8, 'little'))
        identity.update(name)
        identity.update(len(content).to_bytes(8, 'little'))
        identity.update(content)
    configuration = json.dumps(environment, sort_keys=True, separators=(',', ':')).encode()
    return {'format': 'tofu.execution-plan/v1', 'python': python,
            'sourceSha256': identity.hexdigest(), 'sourceFiles': len(sources),
            'configurationSha256': hashlib.sha256(configuration).hexdigest()}
