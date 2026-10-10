"""Bounded native-adapter read admission; no content, cookies, threads or I/O.

The adapter execution boundary owns this guard, so tools and package providers
cannot bypass it. State is reconstructible, owner/device scoped, expires after
15 minutes with no route-count quota. Active cooldowns remain intact. No sleeping/retry loop spends browser latency on throttling.
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_ROUTES: dict[tuple[str, str, str], dict] = {}
_TTL = 900


class SiteReadThrottled(RuntimeError):
    pass


def admit(owner, device, site, *, interval_s, cooldown_s):
    key = (str(owner), str(device), str(site))
    if not key[0] or not key[1]:
        raise SiteReadThrottled('Native read requires an explicit owner and device')
    now = time.monotonic()
    with _LOCK:
        for route, state in list(_ROUTES.items()):
            if now - state['seen'] > _TTL:
                del _ROUTES[route]
        state = _ROUTES.get(key)
        if state is None:
            state = {'seen': now, 'next': 0.0, 'empty': 0}
            _ROUTES[key] = state
        if now < state['next']:
            raise SiteReadThrottled('Native site is paced or cooling down; retry later, not through another transport')
        state['seen'] = now
        state['next'] = now + min(60, max(0, interval_s))
    return key, min(600, max(0, cooldown_s))


def settle(ticket, *, empty=False, blocked=False):
    key, cooldown_s = ticket
    now = time.monotonic()
    with _LOCK:
        state = _ROUTES.get(key)
        if state is None:
            return
        state['seen'] = now
        state['empty'] = state['empty'] + 1 if empty else 0
        if blocked or state['empty'] >= 3:
            state['next'] = max(state['next'], now + cooldown_s)
            state['empty'] = 0
