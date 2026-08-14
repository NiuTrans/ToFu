#!/usr/bin/env python3
"""Guard test — windowed first-open bounds the response to a tail slice of the
AUTHORITATIVE ``messages`` JSONB blob, WITHOUT the row-store migration flag.

Root-cause fix for "large conversation first-open" (2026-07-15): ``get_conv``
shipped the entire ``messages`` array (e.g. 6.5 MB for 26 msgs) on every open,
which timed out the client fetch over the tunnel. The row-store windowed path
(``_windowed_served_readonly``) exists but is gated behind an incomplete data
migration (``rows_read_enabled()``) — 116 convs have zero rows, so flipping it
on would serve empty windows and risk a PUT truncating real history.

``_windowed_blob_slice_readonly`` is the SAFE default: it tail-slices the
always-complete authoritative blob, emits the SAME pagination envelope the row
path uses (so the frontend needs no branch), and is correct for every conv
regardless of backfill state. This test drives it directly (pure function, no
DB) and asserts:
  * the served body is bounded to the window, NOT the full array;
  * the envelope (totalCount / firstLoadedSeq / lastLoadedSeq / hasMore) is
    correct for a tail open AND a page-up (before_seq) open;
  * seq == array index (interchangeable with the row store's cursor);
  * a trailing ghost in the tail window is still reconciled.

Plus a Node harness asserting the shipped frontend sends ``window=`` on the
initial-open GET by default.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_conv_windowed_blob_slice.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
from tests._runtime_sections import (
    runtime_section_names, runtime_section_path,
)

CONV_WINDOW = runtime_section_path('conv_window.js')


def _fake_row(messages, *, rev=7, settings=None):
    """A dict-like conversation row as async_fetchone would return."""
    return {
        'id': 'bigconv',
        'title': 'Big Conversation',
        'messages': json.dumps(messages, ensure_ascii=False),
        'created_at': 1000,
        'updated_at': 2000,
        'settings': json.dumps(settings or {}, ensure_ascii=False),
        'rev': rev,
    }


def _big_messages(n):
    """n messages, each padded so the full blob is far larger than a window."""
    msgs = []
    for i in range(n):
        role = 'user' if i % 2 == 0 else 'assistant'
        msgs.append({'role': role, 'content': ('x' * 2000) + f'#{i}',
                     'timestamp': 1000 + i, '_msgId': f'm{i}'})
    return msgs


def test_full_put_loads_old_state_once_through_repository():
    """Preservation guards share one authority-aware repository snapshot."""
    import inspect
    from routes.conversations import _save_conv_blocking

    src = inspect.getsource(_save_conv_blocking)
    assert src.count('load_conversation(') == 1
    assert 'SELECT messages FROM conversations' not in src


def test_row_mirror_fast_path_has_a_per_conversation_coverage_gate():
    """A global feature flag cannot prove this particular mirror is whole."""
    import inspect
    from routes.conversations import get_conv

    src = inspect.getsource(get_conv)
    assert 'row_window_usable' in src
    assert 'msg_count' in src
    assert '_authority or not _is_live' in src


# ═══════════════════════════════════════════════════════════════════════
#  Backend: blob tail-slice bounds the body + correct envelope
# ═══════════════════════════════════════════════════════════════════════


def _slice():
    from routes.conversations import _windowed_blob_slice_readonly
    return _windowed_blob_slice_readonly


def test_sqlite_projects_tail_before_python_and_preserves_envelope():
    """The authoritative SQL projection, not Python slicing, bounds DB I/O."""
    import sqlite3
    from routes.conversations import (
        _projected_blob_window_sql, _windowed_projected_blob_readonly,
    )

    msgs = _big_messages(200)
    full_json = json.dumps(msgs, ensure_ascii=False)
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        'CREATE TABLE conversations '
        '(id TEXT, user_id INTEGER, title TEXT, messages TEXT, '
        'created_at INTEGER, updated_at INTEGER, settings TEXT, rev INTEGER)')
    conn.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?)',
                 ('bigconv', 1, 'Big Conversation', full_json,
                  1000, 2000, '{}', 7))

    sql, has_before = _projected_blob_window_sql('sqlite', None)
    assert has_before is False
    r = conn.execute(sql, ('bigconv', 1, 60)).fetchone()
    projected_json = r['messages']
    assert len(projected_json) < len(full_json) / 2
    assert len(json.loads(projected_json)) == 60

    class _NoFullFetch:
        def execute(self, *args, **kwargs):
            raise AssertionError('unchanged projected tail must not fetch full blob')

    served, changed, cleaned, _ = _windowed_projected_blob_readonly(
        _NoFullFetch(), 'bigconv', r, before_seq=None)
    conn.close()
    assert changed is False and cleaned is None
    assert served['firstLoadedSeq'] == 140
    assert served['lastLoadedSeq'] == 199
    assert served['totalCount'] == 200 and served['hasMore'] is True
    assert served['messages'][0]['content'].endswith('#140')


def test_sqlite_projected_page_up_uses_absolute_cursor():
    import sqlite3
    from routes.conversations import _projected_blob_window_sql

    msgs = _big_messages(200)
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        'CREATE TABLE conversations '
        '(id TEXT, user_id INTEGER, title TEXT, messages TEXT, '
        'created_at INTEGER, updated_at INTEGER, settings TEXT, rev INTEGER)')
    conn.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?)',
                 ('bigconv', 1, 'Big Conversation', json.dumps(msgs),
                  1000, 2000, '{}', 7))
    sql, has_before = _projected_blob_window_sql('sqlite', 140)
    assert has_before is True
    r = conn.execute(sql, ('bigconv', 1, 140, 60)).fetchone()
    page = json.loads(r['messages'])
    conn.close()
    assert r['slice_start'] == 80 and r['slice_end'] == 140
    assert len(page) == 60
    assert page[0]['content'].endswith('#80')
    assert page[-1]['content'].endswith('#139')


def test_pg_projection_uses_indexed_element_slice_not_full_return():
    from routes.conversations import _projected_blob_window_sql

    sql, _ = _projected_blob_window_sql('pg', None)
    assert 'generate_series(slice_start, slice_end - 1)' in sql
    assert 'jsonb_agg(' in sql
    assert "all_messages -> g.i" in sql
    assert "- 'segments' - 'toolRounds'" in sql
    assert 'all_messages AS messages' not in sql
    assert 'ORDER BY i)' in sql


def test_sqlite_projection_strips_heavy_fields_before_python():
    """One huge message must stay bounded even when N exceeds msg_count."""
    import sqlite3
    from routes.conversations import _projected_blob_window_sql

    heavy = [{
        'role': 'assistant', 'content': 'answer', '_msgId': 'a1',
        'segments': [{'type': 'tool', 'payload': 'x' * 500_000}],
        'toolRounds': [{'result': 'y' * 500_000}],
    }]
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        'CREATE TABLE conversations '
        '(id TEXT, user_id INTEGER, title TEXT, messages TEXT, '
        'created_at INTEGER, updated_at INTEGER, settings TEXT, rev INTEGER)')
    conn.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?)',
                 ('heavy', 1, 'Heavy', json.dumps(heavy), 1, 2, '{}', 1))
    sql, _ = _projected_blob_window_sql('sqlite', None)
    row = conn.execute(sql, ('heavy', 1, 60)).fetchone()
    conn.close()

    projected = json.loads(row['messages'])
    assert len(row['messages']) < 2000
    assert projected[0]['content'] == 'answer'
    assert projected[0]['_trimmed'] is True
    assert projected[0]['_trimmedToolRoundCount'] == 1
    assert 'segments' not in projected[0]
    assert 'toolRounds' not in projected[0]


def test_tail_window_bounds_body_and_envelope():
    fn = _slice()
    msgs = _big_messages(200)
    r = _fake_row(msgs)
    full_bytes = len(r['messages'])

    served, changed, cleaned_full, sd = fn('bigconv', r, window=60, before_seq=None)

    # Only the tail 60 are served — the body is bounded, not the full 200.
    assert len(served['messages']) == 60
    assert served['messages'][0]['content'].endswith('#140')   # seq 140 = 200-60
    assert served['messages'][-1]['content'].endswith('#199')
    served_bytes = len(json.dumps(served, ensure_ascii=False))
    assert served_bytes < full_bytes / 2, (
        f'served {served_bytes}B not meaningfully smaller than full {full_bytes}B')

    # Envelope mirrors the row path.
    assert served['windowed'] is True
    assert served['totalCount'] == 200
    assert served['firstLoadedSeq'] == 140       # seq == array index
    assert served['lastLoadedSeq'] == 199
    assert served['hasMore'] is True             # 140 older messages above
    assert served['rev'] == 7
    # An unchanged tail persists nothing.
    assert changed is False and cleaned_full is None


def test_page_up_before_seq_slice():
    fn = _slice()
    msgs = _big_messages(200)
    r = _fake_row(msgs)

    # Page up from seq 140 → the 60 messages ending just before it: [80, 140).
    served, changed, cleaned_full, sd = fn('bigconv', r, window=60, before_seq=140)
    assert len(served['messages']) == 60
    assert served['firstLoadedSeq'] == 80
    assert served['lastLoadedSeq'] == 139
    assert served['messages'][0]['content'].endswith('#80')
    assert served['messages'][-1]['content'].endswith('#139')
    assert served['hasMore'] is True             # seq 80 > 0 → 80 older remain
    # A page-up slice is NEVER reconciled (only the tail can carry a ghost).
    assert changed is False and cleaned_full is None


def test_page_up_slice_is_also_trimmed():
    """A scrolled-in EARLIER page must be heavy-field-trimmed too — else page-up
    re-imports the megabytes the tail open just avoided. Its trimmed messages
    carry the _trimmed marker so the frontend can (re-)arm hydration for them."""
    fn = _slice()
    msgs = _heavy_msgs(120)                       # 120 heavy assistant turns
    r = _fake_row(msgs)
    # Page up from seq 60 → the 60 messages [0, 60): all heavy → all trimmed.
    served, _, _, _ = fn('bigconv', r, window=60, before_seq=60)
    assert len(served['messages']) == 60
    assert served['firstLoadedSeq'] == 0
    for m in served['messages']:
        for f in _HEAVY:
            assert f not in m, f'heavy field {f!r} leaked into a page-up message'
        assert m.get('apiRounds') is not None, 'apiRounds must survive the trim'
        assert m.get('_trimmed') is True          # marker present for re-hydration
    assert served['trimmed'] is True


def test_short_conv_returns_all_no_hasmore():
    fn = _slice()
    msgs = _big_messages(5)
    r = _fake_row(msgs)
    served, changed, cleaned_full, sd = fn('bigconv', r, window=60, before_seq=None)
    assert len(served['messages']) == 5          # window >= total → all
    assert served['firstLoadedSeq'] == 0
    assert served['lastLoadedSeq'] == 4
    assert served['hasMore'] is False            # nothing older
    assert served['totalCount'] == 5


def test_empty_blob_safe():
    fn = _slice()
    r = _fake_row([])
    served, changed, cleaned_full, sd = fn('bigconv', r, window=60, before_seq=None)
    assert served['messages'] == []
    assert served['totalCount'] == 0
    assert served['firstLoadedSeq'] is None
    assert served['lastLoadedSeq'] is None
    assert served['hasMore'] is False


def test_trailing_ghost_in_tail_is_reconciled():
    """A trailing empty-assistant ghost in the tail window is swept, and the
    change is surfaced for the deferred FULL-array persist."""
    fn = _slice()
    msgs = _big_messages(40)
    # Append an orphaned empty-assistant ghost (no content, no toolRounds).
    msgs.append({'role': 'assistant', 'content': '', 'timestamp': 9999, '_msgId': 'ghost'})
    r = _fake_row(msgs)

    served, changed, cleaned_full, sd = fn('bigconv', r, window=10, before_seq=None)
    # The ghost must not survive in the served tail.
    assert not (served['messages'] and served['messages'][-1].get('role') == 'assistant'
                and not served['messages'][-1].get('content')), \
        'trailing empty-assistant ghost was served instead of reconciled away'
    # totalCount still reflects the authoritative (pre-persist) blob length.
    assert served['totalCount'] == 41


# ═══════════════════════════════════════════════════════════════════════
#  Backend: heavy-field trim bounds the body by BYTES (not just count)
# ═══════════════════════════════════════════════════════════════════════

# Heavy fields the windowed serve STRIPS for transport.
_HEAVY = ('toolRounds', 'segments', '_continueToolRounds', 'toolSummary')
# Heavy fields the windowed serve deliberately KEEPS: the cost popover's
# per-round breakdown reads apiRounds and is never refilled by
# loadMessageActivity, so trimming it would silently kill the table until the
# user opens execution history. (Its ~226 KB/round _wire_fp bulk is already
# stripped at persist time; the residual usage/toolCalls dicts are tiny.)
_KEPT_HEAVY = ('apiRounds', '_continueApiRounds')


def _heavy_msgs(n):
    """n assistant messages, each carrying big heavy fields (~50KB toolRounds +
    ~50KB segments) so per-message WEIGHT dominates, not message count — the
    exact shape of the reported conv (26 msgs, 5.8 MB)."""
    msgs = []
    for i in range(n):
        msgs.append({
            'role': 'assistant', 'content': f'answer #{i}', 'thinking': 'hmm',
            'timestamp': 1000 + i, '_msgId': f'm{i}', 'model': 'test',
            'toolRounds': [{'roundNum': j, 'status': 'done',
                            'results': [{'toolName': 'x', 'out': 'y' * 500}]}
                           for j in range(20)],
            'segments': [{'type': 'tool', 'text': 'z' * 2000} for _ in range(25)],
            'apiRounds': [{'usage': {'in': 1, 'out': 2}, 'blob': 'q' * 3000}],
            'toolSummary': 's' * 5000,
        })
    return msgs


def test_trim_bounds_body_by_bytes():
    """A conv heavy by per-message weight (few msgs, huge toolRounds/segments)
    must shrink DRAMATICALLY on windowed serve — the count-window alone can't
    do this, the field trim is what bounds the bytes."""
    fn = _slice()
    msgs = _heavy_msgs(26)                        # 26 msgs, like the reported conv
    r = _fake_row(msgs)
    full_bytes = len(r['messages'])

    served, changed, _, _ = fn('bigconv', r, window=60, before_seq=None)
    # All 26 served (window >= count) — but heavy fields stripped.
    assert len(served['messages']) == 26
    served_bytes = len(json.dumps(served, ensure_ascii=False))
    assert served_bytes < full_bytes * 0.25, (
        f'trim did not bound the body: served {served_bytes}B vs full '
        f'{full_bytes}B ({100*served_bytes/full_bytes:.0f}%)')

    # Every trimmed message: heavy fields gone, light fields kept, marker set.
    for m in served['messages']:
        for f in _HEAVY:
            assert f not in m, f'heavy field {f!r} leaked into a trimmed message'
        # apiRounds is intentionally retained so the cost breakdown table
        # survives a windowed (reloaded) open.
        assert m.get('apiRounds') is not None, 'apiRounds must survive the trim'
        assert m.get('_trimmed') is True
        assert m['content'].startswith('answer #')      # light field kept
        assert m['thinking'] == 'hmm'                     # thinking kept
        assert m['_msgId']                                # id kept (for hydrate)
        assert m['_trimmedToolRoundCount'] == 20          # shape hint kept

    # Envelope advertises the trim so the frontend knows to lazy-hydrate.
    assert served['trimmed'] is True


def test_trim_keeps_api_round_costs_but_drops_historical_wire_diagnostics():
    """Old blobs predate the persist sanitizer and can carry MB-scale
    ``usage._wire_*`` values.  Windowed GET keeps the cost table but must not
    send backend-only diagnostics to the browser."""
    fn = _slice()
    msgs = [{
        'role': 'assistant', 'content': 'answer', '_msgId': 'a1',
        'usage': {'completion_tokens': 4, '_wire_markers': ['x'] * 1000},
        'apiRounds': [{
            'round': 1, 'cost': {'costCny': 0.2},
            'usage': {'prompt_tokens': 12, '_dispatch': {'provider': 'p'},
                      '_wire_bytes': list(range(4000)),
                      '_wire_field_bytes': {'messages': 'x' * 200000},
                      '_wire_markers': {'ttls': ['5m'] * 1000}},
        }],
        '_continueApiRounds': [{
            'round': 2,
            'usage': {'completion_tokens': 3, 'trace_id': 'continue',
                      '_wire_bytes': list(range(1000))},
        }],
        '_liveLastRoundUsage': {
            'tokensIn': 12,
            'usage': {'prompt_tokens': 12, '_wire_fp': list(range(1000))},
        },
    }]
    served, _, _, _ = fn('oldfat', _fake_row(msgs), window=60, before_seq=None)
    got = served['messages'][0]['apiRounds'][0]
    assert got['cost'] == {'costCny': 0.2}
    assert got['usage']['prompt_tokens'] == 12
    assert got['usage']['_dispatch'] == {'provider': 'p'}
    assert not any(k.startswith('_wire_') for k in got['usage'])
    projected = served['messages'][0]
    assert projected['usage'] == {'completion_tokens': 4}
    assert projected['_continueApiRounds'][0]['usage'] == {
        'completion_tokens': 3, 'trace_id': 'continue',
    }
    assert projected['_liveLastRoundUsage'] == {
        'tokensIn': 12, 'usage': {'prompt_tokens': 12},
    }
    assert len(json.dumps(served, ensure_ascii=False)) < 5000


def test_trim_is_readonly_on_input():
    """The trim must NOT mutate the caller's authoritative message dicts."""
    fn = _slice()
    msgs = _heavy_msgs(3)
    r = _fake_row(msgs)
    fn('bigconv', r, window=60, before_seq=None)
    # The original list still carries every heavy field, untouched.
    parsed = json.loads(r['messages'])
    for m in parsed:
        assert 'toolRounds' in m and 'segments' in m and 'apiRounds' in m


def test_light_message_untouched_by_trim():
    """A message with no heavy fields is passed through verbatim (no _trimmed)."""
    fn = _slice()
    msgs = [{'role': 'user', 'content': 'hi', 'timestamp': 1, '_msgId': 'u0'}]
    r = _fake_row(msgs)
    served, _, _, _ = fn('bigconv', r, window=60, before_seq=None)
    assert served['messages'][0].get('_trimmed') is None
    assert served['messages'][0]['content'] == 'hi'


# ═══════════════════════════════════════════════════════════════════════
#  Backend PUT-merge: a trimmed PUT must NEVER drop stored heavy fields
#  (data-loss guard — the whole point of the trim being safe)
# ═══════════════════════════════════════════════════════════════════════

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/conv_windowed_blob_slice_test.db')


def _seed_full_conv(conv_id, msgs):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    import time as _t
    db = get_thread_db(DOMAIN_CHAT)
    now = int(_t.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'heavy',
        'messages': json_dumps_pg(msgs), 'msg_count': len(msgs),
        'created_at': now, 'updated_at': now,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_full_conv(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    if not row or not row[0]:
        return None
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


@pytest.mark.unit
def test_put_refills_trimmed_heavy_fields_from_stored_blob():
    """THE data-loss guard: seed a conv whose assistant msg carries heavy
    fields; simulate the frontend PUTting back the TRIMMED shape (heavy fields
    absent, matched by _msgId); assert the stored blob STILL round-trips the
    full toolRounds / segments / apiRounds. Drives the REAL _save_conv_blocking."""
    from lib.database import DOMAIN_CHAT, get_thread_db, init_db
    init_db()
    from routes.conversations import _save_conv_blocking

    conv_id = 'cv-heavy-' + str(os.getpid())
    heavy_round = [{'roundNum': 0, 'status': 'done',
                    'results': [{'toolName': 'grep', 'out': 'BIG' * 1000}]}]
    heavy_segs = [{'type': 'tool', 'text': 'SEG' * 1000}]
    heavy_api = [{'usage': {'in': 5}, 'blob': 'API' * 1000}]
    full_msgs = [
        {'role': 'user', 'content': 'q', 'timestamp': 1, '_msgId': 'u0'},
        {'role': 'assistant', 'content': 'the answer', 'thinking': 't',
         'timestamp': 2, '_msgId': 'a0', 'model': 'test',
         'toolRounds': heavy_round, 'segments': heavy_segs, 'apiRounds': heavy_api},
    ]
    db = get_thread_db(DOMAIN_CHAT)
    db.execute('DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()
    _seed_full_conv(conv_id, full_msgs)

    try:
        # The frontend PUTs back the TRIMMED shape: same _msgId, heavy fields
        # gone, _trimmed marker present (exactly what a windowed open produced).
        trimmed_put = {
            'title': 'heavy',
            'messages': [
                {'role': 'user', 'content': 'q', 'timestamp': 1, '_msgId': 'u0'},
                {'role': 'assistant', 'content': 'the answer', 'thinking': 't',
                 'timestamp': 2, '_msgId': 'a0', 'model': 'test',
                 '_trimmed': True, '_trimmedToolRoundCount': 1},
            ],
        }
        _save_conv_blocking(db, conv_id, trimmed_put)

        stored = _read_full_conv(conv_id)
        assert stored is not None and len(stored) == 2
        a = next(m for m in stored if m.get('_msgId') == 'a0')
        # ★ Heavy fields REFILLED from the stored blob — not dropped.
        assert a.get('toolRounds') == heavy_round, 'toolRounds lost on trimmed PUT'
        assert a.get('segments') == heavy_segs, 'segments lost on trimmed PUT'
        assert a.get('apiRounds') == heavy_api, 'apiRounds lost on trimmed PUT'
        # The transient trim markers must NOT persist into the authoritative blob.
        assert '_trimmed' not in a
        assert '_trimmedToolRoundCount' not in a
    finally:
        db.execute('DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()


@pytest.mark.unit
def test_put_does_not_clobber_client_fresh_heavy_fields():
    """The refill must only fill fields the client OMITTED — a client that sends
    a FRESH toolRounds (e.g. regen) keeps its own, not the stale stored one."""
    from lib.database import DOMAIN_CHAT, get_thread_db, init_db
    init_db()
    from routes.conversations import _save_conv_blocking

    conv_id = 'cv-fresh-' + str(os.getpid())
    db = get_thread_db(DOMAIN_CHAT)
    db.execute('DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()
    _seed_full_conv(conv_id, [
        {'role': 'assistant', 'content': 'old', 'timestamp': 2, '_msgId': 'a0',
         'toolRounds': [{'roundNum': 0, 'status': 'done', 'old': True}]},
    ])
    try:
        fresh_rounds = [{'roundNum': 0, 'status': 'done', 'fresh': True}]
        _save_conv_blocking(db, conv_id, {'title': 'x', 'messages': [
            {'role': 'assistant', 'content': 'new', 'timestamp': 2, '_msgId': 'a0',
             'toolRounds': fresh_rounds},
        ]})
        stored = _read_full_conv(conv_id)
        a = stored[0]
        assert a['toolRounds'] == fresh_rounds, 'refill clobbered a fresh client value'
    finally:
        db.execute('DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()


# ═══════════════════════════════════════════════════════════════════════
#  Frontend: initial-open GET carries window= by default
# ═══════════════════════════════════════════════════════════════════════

_HARNESS = r"""
const fs = require('fs');
global.window = global;
// Default (unset) → windowing ON. Do NOT set TOFU_CONV_WINDOW.
eval(fs.readFileSync(process.argv[2], 'utf8'));  // conv_window.js
const out = [];
function check(name, cond){ out.push((cond?'PASS ':'FAIL ')+name); }

// Default active window size (unset env → default 60).
check('default_window_active', convWindowParam() === '60');

// Explicit 0 disables it (legacy full load).
global.window.TOFU_CONV_WINDOW = 0;
check('explicit_zero_disables', convWindowParam() === '');

// Custom override honored.
global.window.TOFU_CONV_WINDOW = 25;
check('custom_size_honored', convWindowParam() === '25');

console.log(out.join('\n'));
process.exit(0);
"""


def _node_available():
    return bool(shutil.which('node'))


def _conv_family():
    """The drift-proof conv-family eval list (see
    tests/_conv_bundle_sources.conv_family_sources) — this suite's
    initial-open harness drives conversations.js, whose leaf references
    (_setCacheVerifying & co) need the whole family, not a two-file
    inline list (2026-08-01 RED)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    names = [
        name for name in runtime_section_names()
        if name == 'core/conversations.js'
        or name.startswith('core/conv_')
        or name == 'core/pending_sync.js'
    ]
    return [runtime_section_path(name) for name in names]

@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_frontend_default_window_param_active(tmp_path):
    harness = tmp_path / '_win_param_harness.js'
    harness.write_text(_HARNESS, encoding='utf-8')
    proc = subprocess.run(
        ['node', str(harness), CONV_WINDOW],
        capture_output=True, text=True, timeout=60)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'window-param failures:\n' + out


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_initial_open_sends_window_param(tmp_path):
    """Drive the REAL loadConversationMessages for a non-cached conv and assert
    the first-open getResponse carried query.window (so the body is bounded)."""
    harness = tmp_path / '_initial_open_harness.js'
    harness.write_text(_INITIAL_OPEN_HARNESS, encoding='utf-8')
    proc = subprocess.run(
        ['node', str(harness),
         CONV_WINDOW,
         *_conv_family()],
        capture_output=True, text=True, timeout=60)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    assert 'PASS initial_open_window_param' in out, out
    assert 'WINDOW_PARAM=60' in out, out


_INITIAL_OPEN_HARNESS = r"""
const fs = require('fs');
global.window = global;
// default (unset) → windowing ON
global.activeConvId = 'c1';
global.activeStreams = new Map();
global.streamBufs = new Map();
global.streamSessions = new Map();
global.getStreamSession = global.getStreamSession = (cid) => { let s = global.streamSessions.get(cid); if (!s) { s = { phase: null }; global.streamSessions.set(cid, s); } return s; };
global.setStreamPhase = global.setStreamPhase = (cid, p) => { if (!global.streamSessions.has(cid) && !(typeof activeStreams !== "undefined" && activeStreams.has(cid))) return; global.getStreamSession(cid).phase = p; };
global.clearStreamSession = global.clearStreamSession = (cid) => { global.streamSessions.delete(cid); };
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};
global.renderChat = () => {};
/* Post-SEAM-2-fold (Phase 3.5 step 5): repaints route through ConvView;
 * the latent fetch-fail branch reads document.getElementById. */
global.ConvView = { replaceAll: () => {}, apply: () => true };
global.document = { getElementById: () => null };
global.renderConversationList = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global.attachCompactionMarkersToConversation = undefined;
global._bgRefreshChat = undefined;
global.Icon = () => '';
global.escapeHtml = (s) => String(s == null ? '' : s);
global.syncConversationToServer = () => {};
global._retriggerHgTranslations = () => {};
global.apiUrl = (p) => p;
global._convSorter = () => 0;

for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));  // conv_window.js first, then the conv family in bundle order

global.ConvCache = {
  isAvailable: () => true, get: () => Promise.resolve(null),
  getMeta: () => Promise.resolve(null), getAllMeta: () => Promise.resolve([]),
  put: () => {}, remove: () => {},
};

let capturedOpts = null;
const TAIL = [
  { role: 'user', content: 'q', timestamp: 1 },
  { role: 'assistant', content: 'a', timestamp: 2 },
];
const RESP = {
  id: 'c1', title: 'c1', updatedAt: 9, rev: 2,
  windowed: true, totalCount: 100, firstLoadedSeq: 98, lastLoadedSeq: 99,
  hasMore: true, messages: TAIL,
};
global.Api = { conversations: {
  getResponse: async (id, opts) => {
    capturedOpts = opts;
    return { status: 200, ok: true, headers: { get: () => null },
             json: async () => RESP };
  },
  get: async () => RESP,
}};

global.conversations = [{
  id: 'c1', title: 'c1', messages: [], _serverMsgCount: 100,
  _needsLoad: true, createdAt: 1, updatedAt: 1, activeTaskId: null,
}];

global.conversations = conversations;

(async () => {
  if (typeof loadConversationMessages !== 'function') { console.log('FAIL fn_exposed'); process.exit(0); }
  await loadConversationMessages('c1');
  for (let i = 0; i < 50; i++) { await Promise.resolve(); }
  const win = capturedOpts && capturedOpts.query && capturedOpts.query.window;
  console.log('WINDOW_PARAM=' + (win || ''));
  console.log((win === '60' ? 'PASS ' : 'FAIL ') + 'initial_open_window_param');
  process.exit(0);
})();
"""


# ═══════════════════════════════════════════════════════════════════════
#  Frontend: page-up re-arms _trimmed AND hydrate refills a scroll-in message
# ═══════════════════════════════════════════════════════════════════════

_SCROLLUP_HYDRATE_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.activeConvId = 'c1';
global.renderChat = () => {};   // no DOM
global.ConvView = { replaceAll: () => {}, apply: () => true };  // post-fold seam
global.document = { getElementById: () => null };  // loadEarlier reads #chatInner
eval(fs.readFileSync(process.argv[2], 'utf8'));  // conv_window.js (argv[2]: argv[1] is this harness)

const out = [];
function check(name, cond){ out.push((cond?'PASS ':'FAIL ')+name); }

// Conv opened windowed: initial tail was ALL LIGHT (no _trimmed) so the flag is
// false. Then the user scrolls up and an EARLIER page arrives trimmed.
const conv = {
  id: 'c1', _windowed: true, _trimmed: false, _hasMoreEarlier: true,
  _firstLoadedSeq: 2,
  messages: [ { role:'user', content:'q', _msgId:'u2', timestamp:3 } ],  // light tail
};
global.conversations = [conv];

// before_seq page: 2 earlier messages, one heavy assistant TRIMMED.
const EARLIER = {
  windowed:true, trimmed:true, firstLoadedSeq:0, hasMore:false,
  messages: [
    { role:'user', content:'older-q', _msgId:'u0', timestamp:1 },
    { role:'assistant', content:'older-a', _msgId:'a0', timestamp:2,
      _trimmed:true, _trimmedToolRoundCount:3 },
  ],
};
const ACTIVITY = {
  ok:true, msgId:'a0', idx:1,
  activity:{
    toolRounds:[{roundNum:0,status:'done',big:'X'}],
    segments:[{type:'tool'}],
  },
};
let activityCalls = 0;
global.Api = { conversations: {
  get: async (id, opts) => {
    const q = (opts && opts.query) || {};
    if (q.before_seq !== undefined) return EARLIER;
    return null;
  },
  messageActivity: async (id, msgId) => {
    activityCalls++;
    return ACTIVITY;
  },
}};

(async () => {
  // 1) scroll-up loads the earlier trimmed page.
  const n = await loadEarlierMessages('c1');
  check('page_up_prepended', n === 2 && conv.messages.length === 3);
  // 2) the scroll-in trimmed message RE-ARMS conv._trimmed (was false).
  check('scrollup_rearms_trimmed', conv._trimmed === true);
  // 3) expanding that scrolled-in message loads only its execution history.
  const ok = await loadMessageActivity('c1', 'a0');
  const a0 = conv.messages.find(m => m._msgId === 'a0');
  check('message_activity_refilled_scrollin_msg',
        ok === true && activityCalls === 1
        && Array.isArray(a0.toolRounds) && a0.toolRounds.length === 1
        && !a0._trimmed);
  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_scrollup_rearms_trimmed_and_loads_message_activity(tmp_path):
    """A scrolled-in trimmed message loads only its own execution fields by
    stable _msgId; the surrounding conversation is not hydrated."""
    harness = tmp_path / '_scrollup_harness.js'
    harness.write_text(_SCROLLUP_HYDRATE_HARNESS, encoding='utf-8')
    proc = subprocess.run(
        ['node', str(harness), CONV_WINDOW],
        capture_output=True, text=True, timeout=60)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'scroll-up/hydrate failures:\n' + out


# ═══════════════════════════════════════════════════════════════════════
#  Frontend: scroll-anchor is preserved on prepend (measured on the REAL
#  scroll container #chatContainer, not the non-scrolling #chatInner child)
# ═══════════════════════════════════════════════════════════════════════

_SCROLL_ANCHOR_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.activeConvId = 'c1';
global.renderChat = () => {};
/* Post-SEAM-2-fold: loadEarlierMessages re-renders via ConvView.replaceAll —
 * the prepend-height simulation rides THAT call now (same side effect the
 * old renderChat stub modeled). */
global.ConvView = { replaceAll: () => {
  chatContainer.scrollHeight = 2000;
}, apply: () => true };

// Fake DOM: #chatContainer is the overflow-y:auto scroll box (mutable
// scrollTop/scrollHeight); #chatInner is a NON-scrolling child whose scrollTop
// stays 0 and scrollHeight==clientHeight (exactly the CSS reality). If the fix
// regresses and measures #chatInner, prevTop=0 & the re-pin writes to a dead
// element, so chatContainer.scrollTop is left at its old value (asserted below).
const chatContainer = { id:'chatContainer', scrollTop: 500, scrollHeight: 1200,
                        clientHeight: 600 };
const chatInner = { id:'chatInner', scrollTop: 0, scrollHeight: 600,
                    clientHeight: 600 };
global.document = { getElementById: (id) =>
  id === 'chatContainer' ? chatContainer :
  id === 'chatInner' ? chatInner : null };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // conv_window.js

const out = [];
function check(name, cond){ out.push((cond?'PASS ':'FAIL ')+name); }

const conv = {
  id:'c1', _windowed:true, _hasMoreEarlier:true, _firstLoadedSeq:60,
  messages: [ { role:'user', content:'tail-q', _msgId:'u60', timestamp:61 } ],
};
global.conversations = [conv];

// A page of 3 earlier messages; envelope advances the cursor.
const EARLIER = { windowed:true, firstLoadedSeq:57, hasMore:true, messages: [
  { role:'user', content:'e57', _msgId:'u57', timestamp:58 },
  { role:'assistant', content:'e58', _msgId:'a58', timestamp:59 },
  { role:'user', content:'e59', _msgId:'u59', timestamp:60 },
]};
global.Api = { conversations: { get: async () => EARLIER } };

(async () => {
  const n = await loadEarlierMessages('c1');
  check('prepended_count', n === 3 && conv.messages.length === 4);
  // Anchor math: prevTop(500) + (newHeight 2000 - prevHeight 1200) = 1300.
  check('scroll_repinned_on_container', chatContainer.scrollTop === 1300);
  // The non-scrolling inner child must NOT have been written.
  check('inner_untouched', chatInner.scrollTop === 0);
  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_scroll_anchor_preserved_on_prepend(tmp_path):
    """Regression: loadEarlierMessages must measure + re-pin the scroll anchor
    on the ACTUAL scroll box (#chatContainer), so the viewport does not jump to
    the top when an earlier page is prepended. Guards against measuring the
    non-scrolling #chatInner child (scrollTop always 0 → re-pin is a no-op)."""
    harness = tmp_path / '_scroll_anchor_harness.js'
    harness.write_text(_SCROLL_ANCHOR_HARNESS, encoding='utf-8')
    proc = subprocess.run(
        ['node', str(harness), CONV_WINDOW],
        capture_output=True, text=True, timeout=60)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'scroll-anchor failures:\n' + out


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
