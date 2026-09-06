"""Remote-worktree binding contract (RWA 拍板 3A — same-name routing).

One shared home for the master switch + the cfg binding shape, consumed by
the tail-context projection layer (``ToolContext.project_remote``) and the
execution-routing layer (``handlers/project.py`` → bridge command). Keeping
the contract here — not re-parsed in two places — stops model guidance and the
executor from disagreeing about whether a conversation is remote.
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)


def remote_worktree_enabled() -> bool:
    """Master switch (总闸). Default OFF = byte-identical legacy behaviour."""
    return (os.environ.get('TOFU_REMOTE_WORKTREE', '')
            or '').strip().lower() in ('1', 'true', 'yes', 'on')


def remote_worktree_binding(cfg):
    """``cfg['project_remote']`` → ``{'agent_id', 'root'}`` or None.

    The binding contract (set by the P4 entry points): a dict naming one
    registered agent + one of ITS declared share_roots. Both keys are
    required; a partial binding is treated as absent (fail-closed).
    """
    if not remote_worktree_enabled():
        return None
    remote = (cfg or {}).get('project_remote')
    if isinstance(remote, dict) and remote.get('agent_id') and remote.get('root'):
        return {'agent_id': str(remote['agent_id']),
                'root': str(remote['root'])}
    return None


def parse_remote_path(project_path):
    """``remote:<agent_id>:<root>`` pseudo-path → ``(agent_id, root)`` or None.

    The UI-side binding convention (RWA P4b): a remote root rides the
    EXISTING per-conv projectPath persistence as a pseudo-path, and
    ``resolve_conv_config`` translates it into ``cfg['project_remote']``.
    The root part may itself contain ':' (Windows-y names stay intact).
    """
    text = str(project_path or '')
    if not text.startswith('remote:'):
        return None
    rest = text[len('remote:'):]
    if ':' not in rest:
        return None
    agent_id, root = (p.strip() for p in rest.split(':', 1))
    if not agent_id or not root:
        return None
    return agent_id, root


def validate_remote_binding(agent_id, root, user_id=''):
    """Validate + mint a ``project_remote`` binding (RWA P4 entry points).

    Returns ``(binding, error)``: the agent must be online, ``root`` must
    be one of ITS declared share_roots, and — when the caller is scoped
    (per-user bridge token, RWA P4a) — the agent must belong to the same
    bridge user. Every refusal is an honest, model/user-visible string.
    """
    if not remote_worktree_enabled():
        return None, ('remote worktrees are disabled on this server '
                      '(TOFU_REMOTE_WORKTREE is not set)')
    from lib.desktop import online_agents
    user_id = user_id or ''
    agent = next((a for a in online_agents()
                  if a['agent_id'] == agent_id), None)
    if agent is None:
        return None, (f'desktop agent {agent_id!r} is not online — start '
                      'the agent on the target machine first')
    if (agent.get('user_id') or '') != user_id:
        return None, (f'desktop agent {agent_id!r} belongs to a different '
                      'bridge user')
    roots = {str(r.get('name') or '') for r in agent.get('share_roots') or []
             if isinstance(r, dict)}
    if root not in roots:
        declared = ', '.join(sorted(r for r in roots if r)) or '(none)'
        return None, (f'agent {agent_id!r} has no share root {root!r} '
                      f'(declared: {declared})')
    return {'agent_id': str(agent_id), 'root': str(root)}, None
