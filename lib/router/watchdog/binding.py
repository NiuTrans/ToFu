"""Watchdog binding: the ordinary Coordinator binding on the watchdog path.

Responsibility: compute the owner's watchdog path (``.tofu-watchdog`` inside
the router home, which the home's .gitignore already excludes) and resolve,
and on explicit request provision, its bound conversation through
``ProjectCoordinationRepository.provision``: a deterministic identity, so
retries and concurrent ensures converge on one conversation titled
``Watchdog`` whose settings carry ``WATCHDOG_SETTING``.

Entry points: ``watchdog_location(user_id)`` (pure), ``is_watchdog_location``,
``router_watchdog(user_id, *, create, client=None)`` (binding view) and
``is_watchdog_scope(scope)``. ``create=False`` touches no filesystem.
Dependencies: lib.router.home, lib.conversations.project_coordination.
"""
from __future__ import annotations

import os

from lib.conversations.project_identity import normalize_project_path
from lib.identity import require_user_id

WATCHDOG_TITLE = 'Watchdog'
# Presentation marker only: watchdog identity is the binding on its path.
WATCHDOG_SETTING = '_routerWatchdog'
WATCHDOG_DIRECTORY = '.tofu-watchdog'
_PROVISION_COMMAND = 'router-watchdog'


def watchdog_location(user_id: int) -> str:
    """Canonical watchdog path of one owner; creates nothing."""
    from lib.router.home import router_home_location
    return normalize_project_path(os.path.join(router_home_location(user_id), WATCHDOG_DIRECTORY))


def is_watchdog_location(user_id: int, path: str) -> bool:
    if not isinstance(path, str) or not path.strip():
        return False
    return normalize_project_path(path.strip()) == watchdog_location(user_id)


def is_watchdog_scope(scope: dict | None) -> bool:
    """Whether a coordinator scope is the owner's Watchdog."""
    return bool(scope) and is_watchdog_location(scope['user_id'], scope['project_path'])


def router_watchdog(user_id: int, *, create: bool, client=None) -> dict:
    """Read the owner's Watchdog binding; ``create`` ensures it exists."""
    owner = require_user_id(user_id, context='Router Watchdog')
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    path = watchdog_location(owner)
    if create:
        from lib.router.home import router_home_path
        router_home_path(owner)
        os.makedirs(path, exist_ok=True)
    bindings = (ProjectCoordinationRepository(owner) if client is None
                else ProjectCoordinationRepository(owner, client=client))
    binding = bindings.read(path)
    if create and binding['availability'] != 'available':
        binding = bindings.provision(path, title=WATCHDOG_TITLE, settings={WATCHDOG_SETTING: True},
                                     command_id=_PROVISION_COMMAND)
    return binding


__all__ = ['WATCHDOG_DIRECTORY', 'WATCHDOG_SETTING', 'WATCHDOG_TITLE', 'is_watchdog_location',
           'is_watchdog_scope', 'router_watchdog', 'watchdog_location']
