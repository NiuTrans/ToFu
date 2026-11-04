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
from typing import Literal

from lib.git_checkpoint_policy import git_policy_environment
from lib.git_runtime import GIT_METADATA_CWD, GIT_STALL_SECONDS, git_executable

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


def resolve_workspace_mode(project_path: str | Path, *, cancel=None) -> WorkspaceMode:
    """Read bounded local configuration; unknown/failed reads never opt out.

    Callers pass a verified canonical Git repository. No cache or tree walk is
    needed: one local config read per admission (zero with a process override).
    Git routing/config environment variables cannot redirect this policy read.
    The read waits as long as the checkout volume keeps it running; only the
    shared Git stall window or ``cancel`` (re-raised as ``ProcessCancelled``)
    ends it early.
    """
    override = os.environ.get('TOFU_WORKSPACE_MODE')
    if override is not None:
        return _mode(override, 'TOFU_WORKSPACE_MODE')
    from lib.project_mod.activity_supervised_process import (
        ProcessCancelled, run_activity_supervised)
    try:
        from .workspace_lease import _checkout_binding
        binding = _checkout_binding(project_path)
        if binding is None:
            raise WorkspacePolicyError('Workspace policy requires a Git repository')
        git_dir = binding[2]
        result = run_activity_supervised(
            [git_executable(), f'--git-dir={git_dir}',
             'config', '--local', '--get-all', 'tofu.workspaceMode'],
            cwd=GIT_METADATA_CWD, env=git_policy_environment(),
            stall_seconds=GIT_STALL_SECONDS, cancellation=cancel)
    except ProcessCancelled:
        raise
    except (OSError, RuntimeError) as error:
        raise WorkspacePolicyError(f'Workspace policy read failed: {error}') from error
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return 'direct'
    if result.returncode != 0 or result.stderr.strip():
        raise WorkspacePolicyError('Workspace policy read failed')
    values = result.stdout.splitlines()
    if len(values) != 1:
        raise WorkspacePolicyError('Ambiguous tofu.workspaceMode; set exactly one value')
    return _mode(values[0], 'tofu.workspaceMode')
