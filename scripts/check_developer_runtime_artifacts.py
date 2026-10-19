#!/usr/bin/env python3
"""Validate the public tofu-agent distribution boundary before publication.

Responsibility
--------------
Fail a release when the small model-routing v2 control plane is missing or when a
build artifact leaks full-application routes, storage, tests, or ChatUI
assets into the developer-runtime wheel or source distribution. Runtime
behavior remains covered by the focused pytest and clean-wheel smoke gates in
``docs/DEVELOPER_RUNTIME.md``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import tarfile
import zipfile


REQUIRED_MEMBERS = frozenset({
    'contracts/swarm_messages_v1.json',
    'contracts/paper_build_v1.schema.json',
    'contracts/resource_policy_v1.json',
    'tofu_agent/native_runtime.py',
    'tofu_agent/native_repository.py',
    'lib/repo_map/native_runtime.py',
    'lib/repo_map/_native_stream.py',
    'lib/repo_map/_native_discovery_fallback.py',
    'lib/repo_map/_native_owned.py',
    'lib/repo_map/_native_limits.py',
    'lib/repo_map/_native_source_admission.py',
    'lib/repo_map/_native_catalog.py',
    'lib/repo_map/LICENSE.grep-ast.txt',
    'lib/tasks_pkg/native_invocation.py',
    'lib/tasks_pkg/native_prepared.py',
    'lib/tasks_pkg/native_terminal.py',
    'lib/billing/native_settlement.py',
    'lib/tasks_pkg/native_cost_projection.py',
    'lib/tasks_pkg/native_pricing.py',
    'lib/agent_core/native_model_control.py',
    'lib/tasks_pkg/native_model_account.py',
    'lib/agent_core/abort_signal.py',
    'lib/agent_core/storage_errors.py',
    'lib/agent_core/generated_task_event_delivery_contract.py',
    'lib/agent_core/generated_task_event_delivery_profile.py',
    'lib/agent_core/native_artifact.py',
    'lib/agent_core/native_content_contract.py',
    'lib/agent_core/native_html_projection_contract.py',
    'bootstrap_pkg/runtime_checks.py',
    'bootstrap_pkg/runtime_repair.py',

    'lib/tools/result_value.py',
    'lib/tool_results/tool_result_query_contract.py',
    'lib/tool_results/tool_result_query_generated.py',
    'tofu_agent/provider_setup.py',
    'tofu_agent/provider_store.py',
    'tofu_agent/setup_ui/index.html',
    'tofu_agent/setup_ui/setup.css',
    'tofu_agent/setup_ui/setup.js',
})

REQUIRED_SOURCE_MEMBERS = frozenset({'setup.py'})
REQUIRED_WHEEL_MEMBERS = frozenset()


FORBIDDEN_PREFIXES = (
    'frontend/',
    'lib/database/',
    'lib/storage/',
    'lib/storage_authority_sql/',
    'lib/tests/',
    'routes/',
    'static/',
    'tests/',
)
FORBIDDEN_MEMBERS = frozenset({
    'audit_codex_session.py',
})


def _validate_members(
        path: Path,
        members: frozenset[str],
        *,
        artifact_key: str,
) -> dict[str, int | str]:
    required = REQUIRED_MEMBERS | (REQUIRED_WHEEL_MEMBERS if artifact_key == 'wheel'
                                   else REQUIRED_SOURCE_MEMBERS)
    missing = sorted(required - members)
    leaked = sorted(
        member for member in members
        if (
            member in FORBIDDEN_MEMBERS
            or member.startswith(FORBIDDEN_PREFIXES)
        )
    )
    if missing:
        raise ValueError(
            f'{path.name} is missing required runtime members: {missing}')
    if leaked:
        raise ValueError(
            f'{path.name} leaked excluded application members: {leaked[:20]}')
    return {
        artifact_key: path.name,
        'members': len(members),
        'required_members': len(required),
        'leaked_members': 0,
    }


def validate_wheel(path: Path) -> dict[str, int | str]:
    """Validate one wheel and return a compact successful report."""
    if not path.is_file() or path.suffix != '.whl':
        raise ValueError(f'not a wheel file: {path}')
    with zipfile.ZipFile(path) as archive:
        members = frozenset(archive.namelist())
    return _validate_members(path, members, artifact_key='wheel')


def _normalized_sdist_members(
        path: Path,
        archive_members: list[tarfile.TarInfo],
) -> frozenset[str]:
    """Strip one canonical distribution root and reject unsafe tar entries."""
    canonical_entries: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
    for member in archive_members:
        name = member.name
        parts = tuple(name.split('/'))
        if (
            not name
            or name.startswith('/')
            or '\\' in name
            or any(part in {'', '.', '..'} for part in parts)
        ):
            raise ValueError(
                f'{path.name} contains a non-canonical path: {name!r}')
        if not (member.isfile() or member.isdir()):
            raise ValueError(
                f'{path.name} contains an unsafe tar entry: {name!r}')
        canonical_entries.append((member, parts))

    if not canonical_entries:
        raise ValueError(f'{path.name} is empty')
    roots = {parts[0] for _, parts in canonical_entries}
    if len(roots) != 1:
        raise ValueError(
            f'{path.name} must contain exactly one distribution root: '
            f'{sorted(roots)!r}')
    root = next(iter(roots))
    expected_root = path.name.removesuffix('.tar.gz')
    if root != expected_root:
        raise ValueError(
            f'{path.name} has unexpected distribution root: {root!r}')
    root_entries = [
        member for member, parts in canonical_entries
        if parts == (root,)
    ]
    if len(root_entries) != 1 or not root_entries[0].isdir():
        raise ValueError(
            f'{path.name} is missing its canonical distribution root')

    normalized: set[str] = set()
    for _, parts in canonical_entries:
        if len(parts) == 1:
            continue
        relative_name = '/'.join(parts[1:])
        if relative_name in normalized:
            raise ValueError(
                f'{path.name} contains a duplicate path: {relative_name!r}')
        normalized.add(relative_name)
    return frozenset(normalized)


def validate_sdist(path: Path) -> dict[str, int | str]:
    """Validate one gzip-compressed source distribution without extracting."""
    if not path.is_file() or not path.name.endswith('.tar.gz'):
        raise ValueError(f'not a .tar.gz source distribution: {path}')
    with tarfile.open(path, mode='r:gz') as archive:
        members = _normalized_sdist_members(path, archive.getmembers())
    return _validate_members(path, members, artifact_key='sdist')


def validate_artifact(path: Path) -> dict[str, int | str]:
    """Dispatch one supported release artifact to its strict validator."""
    if path.suffix == '.whl':
        return validate_wheel(path)
    if path.name.endswith('.tar.gz'):
        return validate_sdist(path)
    raise ValueError(f'unsupported release artifact: {path}')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('artifacts', nargs='+', type=Path)
    args = parser.parse_args(argv)
    for artifact in args.artifacts:
        print(validate_artifact(artifact))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
