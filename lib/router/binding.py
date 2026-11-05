"""Global Router binding: the ordinary Coordinator binding on the router home.

Responsibility: resolve, and on explicit request create, the owner's Global
Router (docs/ROUTER.md R1). The router is selected by the ordinary
``ProjectCoordinationRepository`` binding on ``router_home_location(owner)``;
there is no second binding namespace. Creation provisions the home repository
and a coordinator conversation titled ``Global Router`` (UI label 全局路由)
whose settings carry ``ROUTER_SETTING``. Provisioning uses a deterministic
identity, so retries and concurrent ensures converge on one conversation.

Entry point: ``global_router(user_id, *, create, client=None)`` returns the
binding view ``{projectPath, conversationId, revision, availability,
capabilities}``. ``create=False`` never touches the filesystem.
Dependencies: lib.router.home, lib.conversations.project_coordination.
"""
from __future__ import annotations

from lib.identity import require_user_id

ROUTER_TITLE = 'Global Router'
# Presentation marker only: router identity is the binding on the home path.
ROUTER_SETTING = '_globalRouter'
_PROVISION_COMMAND = 'global-router'


def _bindings(owner: int, client=None):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    if client is None:
        return ProjectCoordinationRepository(owner)
    return ProjectCoordinationRepository(owner, client=client)


def global_router(user_id: int, *, create: bool, client=None) -> dict:
    """Read the owner's Global Router binding; ``create`` ensures it exists."""
    owner = require_user_id(user_id, context='Global Router')
    from lib.router.home import router_home_location, router_home_path
    path = router_home_path(owner) if create else router_home_location(owner)
    bindings = _bindings(owner, client)
    binding = bindings.read(path)
    if create and binding['availability'] != 'available':
        binding = bindings.provision(path, title=ROUTER_TITLE, settings={ROUTER_SETTING: True},
                                     command_id=_PROVISION_COMMAND)
    return binding


__all__ = ['ROUTER_SETTING', 'ROUTER_TITLE', 'global_router']
