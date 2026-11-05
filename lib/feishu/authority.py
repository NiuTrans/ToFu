"""Feishu sender identity and authority: the bridge's one authorization boundary.

Responsibility: for one external sender decide admission (default deny), the
application owner whose account runs the work, whether the sender IS that
owner, and which project root the owner may act on. Every event, command and
pipeline call resolves a :class:`FeishuSender` here before doing anything.

Entry points: :func:`resolve_sender`, :func:`is_admitted`,
:func:`registered_projects`, :func:`matching_registered_projects`,
:func:`authorized_project`.
Dependencies: ``lib.feishu._state`` (allowlist), ``lib.feishu.conversation``
(selected project), ``lib.project_mod.config.get_recent_projects`` (the owner's
registered projects).

Policy (``docs/modules/integrations_api.md``, "Feishu bridge authority"):

* Admission requires an explicit ``FEISHU_ALLOWED_USERS`` entry or an explicit
  ``FEISHU_USER_OWNER_MAP`` entry; an empty allowlist admits no one else.
* Only an owner-map entry makes a sender the owner: slash commands, tool mode
  and human authority. ``FEISHU_DEFAULT_OWNER_USER_ID`` runs other admitted
  senders' chat under that owner's account as nonhuman input, pinned to the
  ``THIRD_PARTY_TEXT`` execution profile (``lib.tasks_pkg.execution_profile``):
  a plain model reply without the owner's tools, context or data.
* Projects are the owner's registered projects; file reads stay inside the
  selected one through the absolute-path guard (``lib.feishu.pipeline``).
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os

from lib.feishu import _state as feishu_state
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'FeishuSender',
    'authorized_project',
    'is_admitted',
    'matching_registered_projects',
    'registered_projects',
    'resolve_sender',
]


@dataclass(frozen=True)
class FeishuSender:
    """One admitted external sender bound to the owner whose account runs its work."""
    open_id: str
    owner_user_id: int
    is_owner: bool

    def identity(self) -> dict:
        """External identity persisted with every exchange of this sender."""
        return {'channel': 'feishu', 'openId': self.open_id, 'owner': self.is_owner}

    @property
    def execution_profile(self) -> str | None:
        """The task profile of this sender's chat: none for the owner."""
        from lib.tasks_pkg.execution_profile import THIRD_PARTY_TEXT
        return None if self.is_owner else THIRD_PARTY_TEXT


def _positive_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and not isinstance(value, bool) else None


def _mapped_owner_user_id(open_id: str) -> int | None:
    raw = os.getenv('FEISHU_USER_OWNER_MAP', '').strip()
    if not raw or not open_id:
        return None
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError:
        logger.error('[Feishu] FEISHU_USER_OWNER_MAP is invalid JSON')
        return None
    if not isinstance(mapping, dict):
        logger.error('[Feishu] FEISHU_USER_OWNER_MAP must be a JSON object')
        return None
    return _positive_int(mapping.get(open_id))


def _default_owner_user_id() -> int | None:
    raw = os.getenv('FEISHU_DEFAULT_OWNER_USER_ID', '').strip()
    owner = _positive_int(raw) if raw else None
    if raw and owner is None:
        logger.error('[Feishu] FEISHU_DEFAULT_OWNER_USER_ID must be a positive integer')
    return owner


def is_admitted(open_id: str) -> bool:
    """Default deny: only an explicitly allowlisted or owner-mapped sender."""
    return bool(open_id) and (open_id in feishu_state.ALLOWED_USERS
                              or _mapped_owner_user_id(open_id) is not None)


def resolve_sender(open_id: str) -> FeishuSender | None:
    """The admitted sender's binding, or None when it must not run anything."""
    owner = _mapped_owner_user_id(open_id)
    if owner is not None:
        return FeishuSender(open_id, owner, True)
    if not is_admitted(open_id):
        return None
    # TODO(enterprise): one default owner per bot app; per-owner bots need
    # per-owner app credentials and allowlists.
    owner = _default_owner_user_id()
    return FeishuSender(open_id, owner, False) if owner is not None else None


def registered_projects(owner_user_id: int) -> list[str]:
    """The owner's registered project paths (``/api/v1/project/recent``)."""
    from lib.project_mod.config import get_recent_projects
    return [row['path'] for row in get_recent_projects(user_id=owner_user_id)
            if isinstance(row, dict) and isinstance(row.get('path'), str) and row['path']]


def _same_directory(left: str, right: str) -> bool:
    return os.path.realpath(os.path.expanduser(left)) == os.path.realpath(os.path.expanduser(right))


def matching_registered_projects(owner_user_id: int, requested: str) -> list[str]:
    """Registered projects named by exact path or by directory name."""
    projects = registered_projects(owner_user_id)
    exact = [path for path in projects if _same_directory(path, requested)]
    if exact:
        return exact[:1]
    return [path for path in projects
            if os.path.basename(os.path.normpath(path)) == requested]


def authorized_project(sender: FeishuSender) -> str | None:
    """The owner's selected project while it remains one of their registered projects."""
    if not sender.is_owner:
        return None
    from lib.feishu.conversation import get_project
    selected = get_project(sender.open_id)
    if not selected:
        return None
    for path in registered_projects(sender.owner_user_id):
        if _same_directory(path, selected) and os.path.isdir(path):
            return path
    return None
