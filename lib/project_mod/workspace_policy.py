"""Select direct or isolated task execution without inspecting project contents.

Entry point: resolve_workspace_mode, called by workspace_delivery only for a
canonical Git checkout with no existing task-owned workspace. The process
TOFU_WORKSPACE_MODE override wins over repository-local tofu.workspaceMode;
absence means direct execution. This policy never creates snapshots, consults
publication refs, or enables automatic commits. Existing workspace ownership
and readonly admission remain the caller's responsibility.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Literal

from lib.git_checkpoint_policy import git_policy_environment
from lib.git_runtime import git_executable

WorkspaceMode = Literal['direct', 'isolated']
WORKSPACE_MODES = frozenset({'direct', 'isolated'})


class WorkspacePolicyError(RuntimeError):
    """An explicit execution policy could not be safely resolved."""


def _mode(value: str, source: str) -> WorkspaceMode:
    normalized = value.strip().lower()
    if normalized not in WORKSPACE_MODES:
        raise WorkspacePolicyError(
            f'Invalid {source}; expected direct or isolated')
    return normalized  # type: ignore[return-value]


def resolve_workspace_mode(project_path: str | Path) -> WorkspaceMode:
    """Read bounded local configuration; unknown/failed reads never opt out.

    Callers pass a verified canonical Git repository. No cache or tree walk is
    needed: one local config read per admission (zero with a process override).
    Git routing/config environment variables cannot redirect this policy read.
    """
    override = os.environ.get('TOFU_WORKSPACE_MODE')
    if override is not None:
        return _mode(override, 'TOFU_WORKSPACE_MODE')
    try:
        result = subprocess.run(
            [git_executable(), 'config', '--local', '--get-all', 'tofu.workspaceMode'],
            cwd=str(project_path), env=git_policy_environment(), text=True,
            capture_output=True, timeout=10, check=False)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise WorkspacePolicyError(f'Workspace policy read failed: {error}') from error
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return 'direct'
    if result.returncode != 0 or result.stderr.strip():
        raise WorkspacePolicyError('Workspace policy read failed')
    values = result.stdout.splitlines()
    if len(values) != 1:
        raise WorkspacePolicyError('Ambiguous tofu.workspaceMode; set exactly one value')
    return _mode(values[0], 'tofu.workspaceMode')
