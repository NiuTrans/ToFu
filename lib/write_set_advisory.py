"""Write-set advisory: surface writes that land OUTSIDE the conversation's
claimed epics' declared write_set.

The project board already dispatches epics disjoint-first
(``project_dispatch._epic_write_sets_clash``) and can hold epics on exact
path leases — but nothing observes the WRITE ITSELF. A conversation that
claims an epic scoped to ``['lib/foo.py']`` and then edits ``lib/bar.py``
is either drifting off-plan or stepping into a sibling's lane. Today that
is invisible until a human reviews the diff.

Deliberately ADVISORY-ONLY (observe, don't block — the write-freshness
gate owns blocking): on each successful project write, look up the epics
the writing conversation has CLAIMED on the written project's board, union
their write_set entries, and when the written path matches none of them,
log a WARNING, audit_log, and emit a project-feed 'note' event so the
drift is visible in the panel.

Fail-open everywhere (an advisory must never become an availability risk):

  * board read failure / no claims by this conv / write_set empty → silence;
  * write_set entries that don't look like paths (no ``/``, ``.`` or ``*``)
    are subsystem TAGS, not paths — ignored for matching;
  * absolute writes outside the written project's root → silence (cross-root
    writes can't be judged against repo-relative entries);
  * each (conv_id, rel_path) warns at most ONCE per process (bounded set).

The board read is TTL-cached (10s) so a 30-edit batch costs one board fetch.
"""

from __future__ import annotations

import fnmatch
import os
import threading
import time
from collections import OrderedDict

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_BOARD_TTL_SEC = 10.0
_WARNED_MAX = 2048

_lock = threading.Lock()
_board_cache: dict = {}          # project_path -> (fetched_ts, {conv_id: [entries]})
_warned: OrderedDict = OrderedDict()  # (conv_id, rel_path) -> True


def _looks_like_path(entry: str) -> bool:
    """A write_set entry is a path pattern only if it carries a separator,
    extension dot, or glob metachar — bare words are subsystem tags."""
    return any(c in entry for c in ('/', '.', '*', '?', '['))


def _norm_rel(path: str) -> str:
    """Normalise a repo-relative path for matching ('./' strip, sep→'/')."""
    p = str(path).replace('\\', '/').strip()
    while p.startswith('./'):
        p = p[2:]
    return p


def _to_rel(project_path: str, raw_path: str) -> str:
    """Relativise *raw_path* against *project_path*; '' when not expressible.

    Absolute paths inside the project become repo-relative; absolute paths
    OUTSIDE it return '' (can't be judged against repo-relative entries);
    relative inputs are normalised as-is.
    """
    if not raw_path:
        return ''
    raw = str(raw_path)
    if os.path.isabs(raw):
        proj = os.path.abspath(project_path or '')
        ap = os.path.abspath(raw)
        if proj and (ap == proj or ap.startswith(proj + os.sep)):
            return _norm_rel(os.path.relpath(ap, proj))
        return ''
    return _norm_rel(raw)


def _matches(entry: str, rel: str) -> bool:
    """True when *rel* falls inside write_set *entry* (file, dir, or glob)."""
    e = _norm_rel(entry).rstrip('/')
    if not e:
        return False
    if rel == e or rel.startswith(e + '/'):
        return True
    return fnmatch.fnmatch(rel, e)


def _claimed_write_sets(project_path: str) -> dict:
    """{conv_id: [write_set entries]} of currently-claimed epics, TTL-cached."""
    proj = (project_path or '').rstrip('/\\')
    now = time.time()
    with _lock:
        hit = _board_cache.get(proj)
        if hit and (now - hit[0]) < _BOARD_TTL_SEC:
            return hit[1]
    out: dict = {}
    try:
        # Lazy import: keeps this module dependency-light for lib/project_mod
        # callers and lets tests monkeypatch the board seam directly.
        from lib.conversations.project_board import read_board
        board = read_board(proj)
        for t in (board.get('tasks') or []):
            if t.get('kind') == 'lease':
                continue
            if t.get('status') not in ('claimed', 'in_progress'):
                continue
            owner = t.get('owner_conv_id') or ''
            if not owner:
                continue
            ws = t.get('write_set') or []
            if isinstance(ws, list) and ws:
                out.setdefault(owner, []).extend(
                    str(e) for e in ws if isinstance(e, str) and e.strip())
    except Exception as e:
        logger.warning('[WriteSetAdvisory] board read failed for %.40r: %s',
                       proj, e)
        out = {}
    with _lock:
        _board_cache[proj] = (now, out)
        if len(_board_cache) > 32:
            # Bounded: projects per process are few, but never grow unbounded.
            oldest = min(_board_cache, key=lambda k: _board_cache[k][0])
            _board_cache.pop(oldest, None)
    return out


def note_project_write(conv_id: str, project_path: str, raw_path: str) -> bool:
    """Advisory hook for one successful write. Returns True when it warned.

    Never raises into the caller; every failure degrades to silence (a
    logged debug at most), per the fail-open contract above.
    """
    try:
        if not conv_id or not project_path:
            return False
        sets = _claimed_write_sets(project_path)
        entries = sets.get(conv_id)
        if not entries:
            return False  # this conv claims nothing → nothing to drift from
        path_entries = [e for e in entries if _looks_like_path(e)]
        if not path_entries:
            return False  # tag-only write_set → can't judge paths
        rel = _to_rel(project_path, raw_path)
        if not rel:
            return False
        if any(_matches(e, rel) for e in path_entries):
            return False
        key = (conv_id, rel)
        with _lock:
            if key in _warned:
                return False
            _warned[key] = True
            while len(_warned) > _WARNED_MAX:
                _warned.popitem(last=False)
        declared = ', '.join(dict.fromkeys(path_entries))[:200]
        logger.warning(
            '[WriteSetAdvisory] conv=%s wrote %s OUTSIDE its claimed epic(s) '
            'write_set (%s) — drift or sibling-lane collision?',
            conv_id[:8], rel, declared)
        audit_log('write_set_outside', conv_id=conv_id, path=rel,
                  project=os.path.basename(project_path.rstrip('/\\')),
                  declared=declared)
        try:
            from lib.conversations.project_feed import emit_project_event
            emit_project_event(
                project_path, conv_id, 'note',
                f'Write outside declared write_set: {rel} '
                f'(claimed epic(s) declare: {declared})',
                payload={'path': rel, 'declared': declared,
                         'guard': 'write_set_advisory'})
        except Exception as e:
            logger.debug('[WriteSetAdvisory] feed emit failed (warned anyway): %s', e)
        return True
    except Exception as e:
        logger.debug('[WriteSetAdvisory] note_project_write failed (silence): %s', e)
        return False


def _reset_caches() -> None:
    """Test isolation: drop the board TTL cache and the warn-once set."""
    with _lock:
        _board_cache.clear()
        _warned.clear()


__all__ = ['note_project_write', '_reset_caches']
