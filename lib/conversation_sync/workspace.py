"""Conversation application workspace policy, not agent-kernel defaults.

resolve_workspace_config freezes a chat application's selection into the existing
accepted Attempt config without touching the filesystem. provision_workspace is
called at physical worker admission. SDK/embed/headless hosts do not enter this
policy: they retain their explicit roots or deliberate absence of a workspace.
Managed files are durable user output, never an expiring cache or a Git checkout
of Tofu's server source. No active-project global or recent-project inference.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import os
from pathlib import Path
import stat
from typing import Any

from lib.identity import require_user_id
from runtime_path_policy import get_runtime_base

WORKSPACE_CONFIG_KEY = '_conversationWorkspace'


def _workspace_path(conversation_id: str, owner_user_id: int) -> Path:
    if not isinstance(conversation_id, str) or not conversation_id or len(conversation_id) > 256:
        raise ValueError('A bounded conversation identity is required for a workspace')
    digest = sha256(conversation_id.encode('utf-8')).hexdigest()
    return Path(get_runtime_base()) / 'data' / 'conversation_workspaces' / f'u{owner_user_id}' / digest


def resolve_workspace_config(config: Mapping[str, Any], conversation_id: str,
                             user_id: Any) -> dict[str, Any]:
    """Freeze explicitly selected roots, or an application-owned private root.

    A chatMode explicitly opts into the conversation application's everyday
    capabilities. Unlabelled API callers, remote bindings and exact caller-owned
    tool schemas are unchanged. An explicit empty root never inherits ambient
    state. Client workspace metadata is discarded by identity admission first.
    """
    result = dict(config)
    from lib.tasks_pkg.chat_mode import normalize_chat_mode
    if (normalize_chat_mode(result) is None or result.get('project_remote')
            or result.get('tools') or result.get('_explicitToolSchemas')
            or '_projectDispatchAdmission' in result):
        return result
    path = result.get('projectPath') or ''
    if not isinstance(path, str):
        raise ValueError('projectPath must be a string')
    extras = result.get('projectPaths') or []
    readonly = result.get('readOnlyPaths') or []
    if (not isinstance(extras, list) or not isinstance(readonly, list)
            or any(not isinstance(root, str) or not root for root in extras + readonly)):
        raise ValueError('Workspace root lists must contain nonempty paths')
    if not path and extras:
        path = extras[0]
        result['projectPath'] = path
    if not path and readonly:
        raise ValueError('Read-only roots require an explicit workspace selection')
    if path:
        # Existing roots remain a host/user selection. Never inspect siblings or
        # reinterpret a remote pseudo-path as a server-local directory.
        return result
    owner = require_user_id(user_id, context='conversation workspace')
    path = str(_workspace_path(conversation_id, owner))
    result.update(projectPath=path, projectPaths=[path], readOnlyPaths=[])
    result[WORKSPACE_CONFIG_KEY] = {
        'version': 1, 'kind': 'managed', 'ownerUserId': owner,
        'conversationId': conversation_id, 'path': path,
    }
    return result


def provision_workspace(task: Mapping[str, Any]) -> bool:
    """Create only a verified managed workspace; refuse redirected roots.

    No filesystem work occurs for SDK/transient tasks or explicit projects.
    Paths are recomputed from authenticated task identity, never trusted from a
    model field. Persistent roots are private and idempotent across recovery.
    """
    if task.get('_transientRuntime'):
        return False
    config = task.get('config') or {}
    binding = config.get(WORKSPACE_CONFIG_KEY)
    if binding is None:
        return False
    from lib.tasks_pkg.manager import task_user_id
    owner = require_user_id(task_user_id(task), context='managed workspace admission')
    conversation_id = str(task.get('convId') or '')
    path = _workspace_path(conversation_id, owner)
    expected = {'version': 1, 'kind': 'managed', 'ownerUserId': owner,
                'conversationId': conversation_id, 'path': str(path)}
    if (not isinstance(binding, Mapping) or dict(binding) != expected
            or config.get('projectPath') != str(path)
            or config.get('projectPaths') != [str(path)]
            or config.get('readOnlyPaths') not in (None, [])):
        raise PermissionError('Managed workspace does not match its accepted owner and roots')
    if task.get('aborted'):
        raise PermissionError('Task cancelled before workspace admission')
    # The runtime base is operator-owned. Below it, reject every symlink and
    # use descriptor-relative O_NOFOLLOW admission against check/use races.
    base = Path(get_runtime_base())
    if os.name == 'posix':
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in path.relative_to(base).parts:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass  # The following no-follow directory open validates existing state.
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        finally:
            os.close(fd)
    else:
        # Windows directory reparse points are refused, not followed.
        current = base
        for component in path.relative_to(base).parts:
            current /= component
            current.mkdir(mode=0o700, exist_ok=True)
            info = current.lstat()
            if (not stat.S_ISDIR(info.st_mode) or current.is_symlink()
                    or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
                raise PermissionError('Managed workspace path is not a plain directory')
    return True
