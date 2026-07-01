"""tests/test_project_brain_live_paths.py — Project Brain path resolution
proven END-TO-END against the REAL running server (not stubs).

WHY THIS EXISTS
---------------
The Project Brain jsdom tests (``test_frontend_project_brain.py``) stub
``Api.project.*`` — so they prove *render-given-data*, NOT that the real
``conv.projectPath → /api/v1/project/{board,feed,charter} → DB`` chain returns
the data. The screenshot bug ("任务板为空 / all blank" while the board had epics)
was exactly a path-resolution mismatch a stubbed test structurally cannot see:
the write side stored the board/feed rows under the raw ``conv.projectPath``
(which could carry a trailing slash) while the panel read the frontend-stripped
form → the keys diverged → the real route returned an empty body.

This module boots the ACTUAL ``server.app`` (the ``live_server`` fixture, an
ephemeral-port Hypercorn in a daemon thread — the same one the visual E2E
suite uses) and drives the REAL HTTP routes with ``urllib``. It seeds a board
epic + a feed event + a charter through the real library, then asserts:

  1. the stripped-path query resolves the seeded rows (baseline sanity);
  2. the TRAILING-SLASH query (what the browser sends when ``conv.projectPath``
     carries a slash) resolves the SAME rows — the fix under test;
  3. the same holds for /feed and /charter.

MANDATORY NEGATIVE CONTROL (source-level, byte-restored): no-op
``normalize_project_path`` in ``lib/conversations/project_feed.py`` → the
trailing-slash route query MISSES the seeded row (empty board) → the assertion
FAILS. This proves the normalization is load-bearing against the LIVE route,
not just in a unit harness.

Runs in CI: the ``live_server`` fixture needs only hypercorn (a hard dep), not
node/Playwright, and open-mode loopback auth means no token is required.
"""

from __future__ import annotations

import importlib
import json
import os
import time
import urllib.request

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_FEED_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_feed.py')

# A distinctive project path that no other test touches; the trailing slash is
# the whole point of the exercise.
_PROJ = '/tmp/tofu-live-brain-proj'


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    """Create every table in the isolated test SQLite DB (mirrors
    test_project_board.py) so the live server + seeding have real tables."""
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


def _get_json(base, path):
    """GET ``base+path`` against the live server; return (status, parsed)."""
    url = base + path
    req = urllib.request.Request(url, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode('utf-8')
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as e:  # noqa: F841
        body = e.read().decode('utf-8', 'replace')
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {'_raw': body}


def _qs(path_value):
    """URL-encode a project path for the ?path= query param."""
    from urllib.parse import quote
    return quote(path_value, safe='')


@pytest.fixture()
def _seed_brain(flask_app):
    """Seed one board epic + a feed event + a charter under the STRIPPED path,
    then clean up. Yields nothing — the rows live in the shared DB the live
    server reads from."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_charter import commit_charter
    from lib.database import DOMAIN_CHAT, get_thread_db
    # Stub the push mirror so seeding doesn't require a live WS hub.
    import lib.agent_core.push as _push
    _orig_push = _push.push_event
    _push.push_event = lambda *a, **k: None
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('DELETE FROM project_tasks WHERE project_path=?', (_PROJ,))
        db.execute('DELETE FROM project_events WHERE project_path=?', (_PROJ,))
        db.execute('DELETE FROM project_charter WHERE project_path=?', (_PROJ,))
        db.commit()
        post_task(_PROJ, 'cLIVE', 'LIVE BRAIN EPIC')
        commit_charter(_PROJ, content='LIVE NORTH STAR',
                       add_decision='live decision', updated_by_conv='cLIVE')
    try:
        yield
    finally:
        _push.push_event = _orig_push
        with flask_app.app_context():
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM project_tasks WHERE project_path=?', (_PROJ,))
            db.execute('DELETE FROM project_events WHERE project_path=?', (_PROJ,))
            db.execute('DELETE FROM project_charter WHERE project_path=?', (_PROJ,))
            db.commit()


def test_live_board_resolves_stripped_and_trailing_slash(live_server, _seed_brain):
    """The REAL GET /board route returns the seeded epic for BOTH the stripped
    path AND the trailing-slash variant (the browser sends conv.projectPath
    verbatim — a slash must not blank the board)."""
    # Baseline: stripped path finds the epic.
    st, data = _get_json(live_server, '/api/v1/project/board?path=' + _qs(_PROJ))
    assert st == 200, data
    assert data.get('open') == 1, f'stripped board must find the epic: {data}'
    epic_id = data['tasks'][0]['id']

    # The fix: the trailing-slash variant resolves the SAME row (not empty).
    st2, data2 = _get_json(live_server,
                           '/api/v1/project/board?path=' + _qs(_PROJ + '/'))
    assert st2 == 200, data2
    assert data2.get('open') == 1, \
        f'trailing-slash board query MUST resolve the seeded epic, got: {data2}'
    assert data2['tasks'][0]['id'] == epic_id, \
        'both path variants must resolve to the SAME board row'


def test_live_feed_and_charter_resolve_trailing_slash(live_server, _seed_brain):
    """The REAL /feed and /charter routes also resolve the trailing-slash
    variant to the seeded data."""
    # Feed: seeding a board epic + a charter commit emitted feed events.
    st, feed = _get_json(live_server,
                        '/api/v1/project/feed?path=' + _qs(_PROJ + '/') + '&since=0')
    assert st == 200, feed
    assert len(feed.get('events', [])) >= 1, \
        f'trailing-slash feed query must resolve the seeded events: {feed}'

    # Charter: committed with content + a decision.
    st2, ch = _get_json(live_server,
                       '/api/v1/project/charter?path=' + _qs(_PROJ + '/'))
    assert st2 == 200, ch
    assert ch.get('exists') is True, \
        f'trailing-slash charter query must resolve the seeded charter: {ch}'
    assert ch.get('content') == 'LIVE NORTH STAR', ch


def test_live_board_resolves_double_encoded_path(live_server, _seed_brain):
    """THE screenshot bug's real root cause: a reverse proxy (the VS Code web
    IDE ``/proxy/<port>/``) RE-ENCODES the already-percent-encoded query value,
    so the route receives ``%252Fmnt%252F…``. Quart decodes once → the handler
    sees the literal ``%2Fmnt%2F…`` string → matches no rows → blank board.

    The ``_decoded_path_arg`` helper must defensively decode-until-stable so the
    DOUBLE-encoded path (what the browser-via-proxy actually sends) resolves the
    seeded epic exactly like the single-encoded path a direct client sends."""
    from urllib.parse import quote
    single = quote(_PROJ, safe='')          # /mnt → %2Fmnt   (direct client)
    double = quote(single, safe='')         # %2F  → %252F    (proxy re-encoded)

    st, base = _get_json(live_server, '/api/v1/project/board?path=' + single)
    assert st == 200 and base.get('open') == 1, f'single-encoded baseline: {base}'

    st2, data = _get_json(live_server, '/api/v1/project/board?path=' + double)
    assert st2 == 200, data
    assert data.get('open') == 1, (
        'DOUBLE-encoded (proxy-re-encoded) board query MUST resolve the seeded '
        f'epic — this is the actual blank-panel bug. got: {data}')
    # /feed + /brain/summary share the same helper — spot-check the summary.
    st3, summ = _get_json(live_server, '/api/v1/project/brain/summary?path=' + double)
    assert st3 == 200 and summ.get('epicsOpen') == 1, \
        f'double-encoded summary must see the epic: {summ}'


def test_NC_live_double_encode_decode_is_load_bearing(live_server, _seed_brain):
    """NEGATIVE CONTROL: neuter the SHARED ``decode_proxy_path_arg`` seam in
    ``lib/request_parser.py`` so it stops re-decoding (returns the once-decoded
    raw arg) → the DOUBLE-encoded live query MISSES → empty board (the
    screenshot bug reproduced through the real HTTP route). Byte-identical
    restore. Runs in the same process as the live server; reloading the seam
    module (which routes/api_v1/project.py imports lazily per-call) rebinds the
    decoder the handler dispatches to.
    """
    from urllib.parse import quote
    rp_src = os.path.join(ROOT, 'lib', 'request_parser.py')
    with open(rp_src, encoding='utf-8') as f:
        original = f.read()
    anchor = ("    raw = (request.args.get(name) or '').strip()\n"
              "    if not raw:\n        return default\n"
              "    for _ in range(_MAX_REDECODE_PASSES):")
    assert anchor in original, 'decode anchor not found in request_parser.py'
    patched = original.replace(
        anchor,
        "    raw = (request.args.get(name) or '').strip()\n"
        "    if not raw:\n        return default\n"
        "    return raw  # NC (re-decode disabled)\n"
        "    for _ in range(_MAX_REDECODE_PASSES):", 1)

    double = quote(quote(_PROJ, safe=''), safe='')
    try:
        with open(rp_src, 'w', encoding='utf-8') as f:
            f.write(patched)
        import lib.request_parser as rp
        importlib.reload(rp)
        time.sleep(0.1)
        st, data = _get_json(live_server, '/api/v1/project/board?path=' + double)
        assert st == 200, data
        assert data.get('open') == 0, (
            'NC: with re-decode disabled the double-encoded live query MUST '
            f'return an empty board (reproduces the screenshot bug). got: {data}')
    finally:
        with open(rp_src, 'w', encoding='utf-8') as f:
            f.write(original)
        import lib.request_parser as rp
        importlib.reload(rp)
        time.sleep(0.1)
    with open(rp_src, encoding='utf-8') as f:
        assert f.read() == original, 'request_parser.py must be restored byte-identical'


def test_NC_live_no_normalization_blanks_trailing_slash(live_server, flask_app):
    """NEGATIVE CONTROL against the LIVE route: no-op normalize_project_path in
    project_feed.py → the board's write key ('/proj') and the trailing-slash
    read key ('/proj/') diverge → the REAL route returns an empty board.

    We seed AFTER patching (so the write also goes through the neutered
    normalizer, landing under the stripped key), then hit the live route with
    the trailing-slash path and assert it MISSES. Byte-identical restore.

    NOTE: the live server holds the ORIGINAL (correct) bytecode in its already-
    imported modules; this NC patches + reloads the modules THIS process's
    seeding uses, and asserts on the divergence between the stored key and the
    queried key — which the route (running the normalizer at request time in
    the server process) would normally paper over. To make the NC bite the LIVE
    route we must also force the server's imported module to the neutered
    version. Since the live_server shares this process's module objects
    (same interpreter, daemon thread), reloading the module here rebinds the
    functions the route calls too.
    """
    with open(_FEED_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("    if not project_path:\n        return ''\n"
              "    return _TRAILING_SEP_RE.sub('', str(project_path))")
    assert anchor in original, 'normalize anchor not found in project_feed.py'
    patched = original.replace(
        anchor, "    return str(project_path or '')  # NC (normalization off)", 1)

    import lib.agent_core.push as _push
    _orig_push = _push.push_event
    _push.push_event = lambda *a, **k: None
    try:
        with open(_FEED_SRC, 'w', encoding='utf-8') as f:
            f.write(patched)
        # Reload so BOTH the seeding calls AND the route handlers (same process)
        # bind the neutered normalizer.
        import lib.conversations.project_feed as pf
        import lib.conversations.project_board as pb
        import lib.conversations.project_charter as pc
        importlib.reload(pf)
        importlib.reload(pb)
        importlib.reload(pc)

        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM project_tasks WHERE project_path IN (?,?)',
                       (_PROJ, _PROJ + '/'))
            db.commit()
            # Seed under the STRIPPED path (write key = '/proj' since the
            # frontend-normalized value is what a correct client sends).
            pb.post_task(_PROJ, 'cNC', 'NC EPIC')

        # Give the reload a beat to settle in the route module refs.
        time.sleep(0.1)
        # The live route, now running the neutered normalizer, queries the
        # trailing-slash key verbatim → MISS.
        st, data = _get_json(live_server,
                            '/api/v1/project/board?path=' + _qs(_PROJ + '/'))
        assert st == 200, data
        assert data.get('open') == 0, (
            'NC: with normalization disabled the trailing-slash live route '
            f'MUST return an empty board (reproduces the bug), got: {data}')
    finally:
        _push.push_event = _orig_push
        with open(_FEED_SRC, 'w', encoding='utf-8') as f:
            f.write(original)
        # Reload the restored (correct) source so later tests + the live
        # server see the fix again.
        import lib.conversations.project_feed as pf
        import lib.conversations.project_board as pb
        import lib.conversations.project_charter as pc
        importlib.reload(pf)
        importlib.reload(pb)
        importlib.reload(pc)
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM project_tasks WHERE project_path IN (?,?)',
                       (_PROJ, _PROJ + '/'))
            db.commit()
    with open(_FEED_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'project_feed.py must be restored byte-identical'
