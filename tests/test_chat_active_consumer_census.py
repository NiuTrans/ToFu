"""Census ratchet: every consumer of the carrier filter is enumerated ONCE.

WHY A CENSUS RATHER THAN N INDIVIDUAL FIXES (charter #26)
---------------------------------------------------------
``is_carrier_task`` hides the autopilot VU sub-task and inline holders. Two
separate defects in this family were BOTH "a new consumer of a by-design-
filtering surface did not account for the filter":

  * pt_d97f9098776c48e9 — the stale-pin sweep used ``/api/v1/chat/active`` as
    its only liveness source, so a live carrier's pin read as stale and the
    sweep stamped ``interrupted`` on work that was still generating;
  * pt_f7a292dc13de47f0 — the follow-up funnel returned before probing at all
    when a carrier id was pinned, stranding the successor worker (the VU user
    message on screen with no Agent bubble, ever).

Fixing them one at a time leaves the NEXT consumer to rediscover the same
trap. Charter #26 therefore requires a census: enumerate the consumers, and
make a new one fail loudly until it declares how it treats carriers.

WHAT IS PINNED
--------------
  * the backend consumer set of ``is_carrier_task``. ``_registry.py`` documents
    this predicate as the SINGLE SOURCE OF TRUTH for both the reconnect
    endpoint (``routes/chat.py``) AND the self-update restart guard
    (``list_running_tasks``) — the two once disagreed about whether a carrier
    counted as a running conversation, which made the restart dialog report
    "N conversations running" while the sidebar showed none. A change to this
    predicate therefore moves the restart dialog's count too, so the restart
    guard is part of the census by construction, not as an afterthought.
  * the frontend caller set of ``Api.chat.active()``, each of which must be
    accounted for.

Both scans strip comments first (charter #24, via ``tests/_source_scan``) —
this family has already produced a guard that its own explanatory comment
satisfied, and a ban that the comment explaining the ban triggered.
"""

import os
import re

import pytest

from tests._source_scan import strip_comments

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# ── Backend: every call site of is_carrier_task, with WHY it filters. ───────
# Adding a call site without adding it here fails the ratchet.
_BACKEND_CONSUMERS = {
    'routes/chat.py': (
        'GET /api/v1/chat/active — the RECONNECT view. Hides carriers because '
        'its five consumers feed the result to the PLAIN connectToTask path, '
        'where a carrier births a permanently-stuck "Waiting…" bubble. Any '
        'consumer that needs carrier-awareness MUST use the conv-state '
        'projection instead; this endpoint must not change.'
    ),
    'lib/tasks_pkg/manager/_registry.py': (
        'list_running_tasks — the self-update RESTART GUARD, plus the '
        'snapshot builder that surfaces a carrier as "<tid>#vu". Shares the '
        'predicate with the reconnect endpoint so the restart dialog and the '
        'sidebar can never again disagree about a carrier being "running".'
    ),
    'lib/tasks_pkg/manager/_sync.py': (
        'Result-sync / conversation-commit paths — a carrier owns no visible '
        'assistant turn, so it must not write one into conversations.messages.'
    ),
}

# ── Frontend: every caller of Api.chat.active(), with its carrier stance. ───
_FRONTEND_CONSUMERS = {
    'static/js/core/cross_tab_sync.js': (
        'Orphan-recovery + the stale-pin sweep. CARRIER-AWARE since '
        'pt_d97f9098776c48e9: pairs this endpoint with the conv-state '
        'projection so a live carrier pin is never judged stale.'
    ),
    'static/js/main/main_send_pipeline.js': (
        'The follow-up/queue funnel. CARRIER-AWARE since pt_f7a292dc13de47f0: '
        'computeFollowupRoute routes the pin (live carrier → VU connector, '
        'terminal/unknown → probe, plain worker → skip) instead of treating a '
        "pin's presence as proof that someone else is driving."
    ),
}


def _py_call_sites(pattern):
    """Files under lib/ + routes/ that CALL ``pattern`` (comments stripped)."""
    hits = set()
    for sub in ('lib', 'routes'):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, sub)):
            for fn in files:
                if not fn.endswith('.py'):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, ROOT)
                try:
                    with open(path, encoding='utf-8') as fh:
                        src = fh.read()
                except OSError:
                    continue
                body = strip_comments(src, lang='py')
                # Calls only — never the def, never a re-export in __all__.
                for m in re.finditer(re.escape(pattern) + r'\s*\(', body):
                    line_start = body.rfind('\n', 0, m.start()) + 1
                    line = body[line_start:m.start()]
                    if line.strip().startswith('def '):
                        continue
                    hits.add(rel)
                    break
    return hits


def _js_call_sites(pattern):
    """Files under static/js/ that call ``pattern`` (comments stripped)."""
    hits = set()
    js_dir = os.path.join(ROOT, 'static', 'js')
    for dirpath, _dirs, files in os.walk(js_dir):
        for fn in files:
            if not fn.endswith('.js') or fn.startswith('bundle-'):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT)
            try:
                with open(path, encoding='utf-8') as fh:
                    src = fh.read()
            except OSError:
                continue
            if pattern in strip_comments(src, lang='js'):
                hits.add(rel)
    return hits


def test_backend_carrier_filter_consumers_are_enumerated():
    """A new is_carrier_task call site must declare its carrier stance."""
    actual = _py_call_sites('is_carrier_task')
    # Sanity: the scan must actually find something, or it proves nothing.
    assert actual, 'scan found no is_carrier_task call sites — the scan is broken'
    undeclared = actual - set(_BACKEND_CONSUMERS)
    assert not undeclared, (
        'New consumer(s) of the carrier filter are not in the census: '
        f'{sorted(undeclared)}.\nCharter #26: when an endpoint filters a class '
        'of object BY DESIGN, a new consumer MUST account for that filter. Add '
        'the file to _BACKEND_CONSUMERS with a one-line statement of how it '
        'treats a VU carrier. If it needs carrier-awareness, read the '
        'conv-state projection — do NOT unhide carriers here, that would break '
        "the reconnect endpoint's contract with its other consumers."
    )


def test_frontend_active_endpoint_consumers_are_enumerated():
    """A new Api.chat.active() caller must declare its carrier stance."""
    actual = _js_call_sites('Api.chat.active(')
    assert actual, 'scan found no Api.chat.active() callers — the scan is broken'
    undeclared = actual - set(_FRONTEND_CONSUMERS)
    assert not undeclared, (
        'New caller(s) of /api/v1/chat/active are not in the census: '
        f'{sorted(undeclared)}.\nThat endpoint EXCLUDES VU carriers by design. '
        'Declare the stance in _FRONTEND_CONSUMERS; if the caller must see '
        'carriers, pair it with Api.chat.convState() as cross_tab_sync.js and '
        'main_send_pipeline.js do.'
    )


def test_restart_guard_shares_the_predicate_with_the_reconnect_view():
    """The two consumers the docstring says once diverged stay coupled.

    ``_registry.py`` records that the restart dialog reported "N conversations
    running" while the sidebar showed none, because the restart guard and the
    reconnect view answered the carrier question differently. They are coupled
    now only because BOTH call this one predicate. Pin that: if either grows
    its own private carrier test, the divergence returns silently.
    """
    for rel in ('routes/chat.py', 'lib/tasks_pkg/manager/_registry.py'):
        with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
            body = strip_comments(fh.read(), lang='py')
        assert 'is_carrier_task' in body, (
            f'{rel} no longer calls is_carrier_task — the restart guard and the '
            'reconnect view must keep sharing ONE predicate, or they will '
            'disagree about whether a carrier counts as a running conversation.'
        )
