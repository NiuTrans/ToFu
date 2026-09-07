"""Display-side cache of browser tab titles.

Tool-round labels must not expose raw tab ids, but a bare ``tab`` is not
informative either. The bridge already reports ``{id, title, url}`` rows on
every ``list_tabs`` sighting (handlers call it constantly — work-tab
resolution, action receipts, snapshots), so this module keeps the most
recent sighting per ``(owner, client, tab)`` and lets the DISPLAY path
render ``Read "Friday MCP Hub"`` instead of ``Read tab``.

Strictly read-through for labels: execution routing never consults it,
entries expire, and a miss simply yields the old generic label. Lookups
require a UNIQUE title across the candidate devices because tab ids are
only unique per device — that is what keeps a multi-device setup from
cross-labeling one device's round with another device's page.
"""

from __future__ import annotations

import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['ingest_tab_list', 'tab_title', 'work_tab_title']

_lock = threading.Lock()
# (owner_user_id, client_id) -> {tab_id: (title, url, seen_at)}
_tabs: dict[tuple[str, str], dict[int, tuple[str, str, float]]] = {}
_ROUTE_CAP = 64          # distinct (owner, client) routes kept
_PER_ROUTE_CAP = 200     # tabs remembered per route
_ENTRY_TTL_SEC = 1800.0  # a title sighting goes stale after 30 min


def _norm_owner(owner_user_id) -> str:
    owner = str(owner_user_id or '').strip()
    return owner if owner.isdigit() and int(owner) >= 1 else ''


def ingest_tab_list(owner_user_id, client_id, tabs) -> None:
    """Record one ``list_tabs`` sighting for later label lookups."""
    owner = _norm_owner(owner_user_id)
    client = str(client_id or '').strip()
    if not owner or not client or not isinstance(tabs, list):
        return
    now = time.time()
    rows: dict[int, tuple[str, str, float]] = {}
    for t in tabs:
        if not isinstance(t, dict):
            continue
        try:
            tab_id = int(t.get('id'))
        except (TypeError, ValueError):
            continue
        title = str(t.get('title') or '').strip()
        url = str(t.get('url') or '').strip()
        if title or url:
            rows[tab_id] = (title, url, now)
    if not rows:
        return
    with _lock:
        route = (owner, client)
        table = _tabs.setdefault(route, {})
        table.update(rows)
        if len(table) > _PER_ROUTE_CAP:
            stale_first = sorted(table.items(), key=lambda kv: kv[1][2])
            for old_id, _ in stale_first[:len(table) - _PER_ROUTE_CAP]:
                table.pop(old_id, None)
        if len(_tabs) > _ROUTE_CAP:
            oldest_route = min(
                _tabs,
                key=lambda r: max((e[2] for e in _tabs[r].values()), default=0.0))
            if oldest_route != route:
                _tabs.pop(oldest_route, None)


def _route_rows(owner: str, client_id) -> list[tuple[int, tuple[str, str, float]]]:
    route = (owner, str(client_id or '').strip())
    with _lock:
        return list(_tabs.get(route, {}).items())


def tab_title(owner_user_id, tab_id, *, client_ids) -> str:
    """Title of ``tab_id`` when exactly one candidate device reports one."""
    owner = _norm_owner(owner_user_id)
    try:
        wanted = int(tab_id)
    except (TypeError, ValueError):
        return ''
    if not owner:
        return ''
    now = time.time()
    titles: set[str] = set()
    for cid in client_ids or ():
        for tid, (title, _url, seen) in _route_rows(owner, cid):
            if tid == wanted and title and now - seen < _ENTRY_TTL_SEC:
                titles.add(title)
    return titles.pop() if len(titles) == 1 else ''


def work_tab_title(owner_user_id, *, client_ids) -> tuple[int | None, str]:
    """(tab_id, title) of the owner's remembered working tab, when unique.

    Reads the working-tab memory from ``lib.browser._resolve`` and resolves
    its title through this cache; the same single-distinct-title guard as
    :func:`tab_title` applies across the candidate devices.
    """
    owner = _norm_owner(owner_user_id)
    if not owner:
        return None, ''
    from lib.browser._resolve import current_work_tab
    now = time.time()
    found: dict[str, int] = {}
    for cid in client_ids or ():
        client = str(cid or '').strip()
        if not client:
            continue
        try:
            tab_id = current_work_tab((owner, client))
        except ValueError:
            continue
        if tab_id is None:
            continue
        for tid, (title, _url, seen) in _route_rows(owner, client):
            if tid == tab_id and title and now - seen < _ENTRY_TTL_SEC:
                found[title] = tab_id
    if len(found) == 1:
        title, tab_id = next(iter(found.items()))
        return tab_id, title
    return None, ''


def _reset_for_tests() -> None:
    with _lock:
        _tabs.clear()
