"""tests/test_chat_poll_incremental.py — the echo-fingerprint incremental poll.

WHY (measured 2026-08-06, epic pt_688f9783)
-------------------------------------------
When a conv surrenders to the poll fallback, `_pollFallback` re-requests
`/api/v1/chat/poll/<id>` every ~0.3-2s for the REST OF THE TURN, and the FULL
snapshot — content + thinking + toolRounds with every tool result verbatim —
rode every response: measured 968KB per poll on a 60-round task, ~2MB/2s
through the VS Code tunnel when several convs degrade at once (the
2026-08-06 four-streams-one-second flap).

THE PROTOCOL (opt-in; non-incr callers byte-identical)
------------------------------------------------------
An `?incr=1` response carries `fp` — per-section fingerprints
(crc32 of content / thinking, [count, crc32-of-last-element] for
toolRounds / endpointTurns). The poll loop echoes the last `fp` verbatim as
`ifp`; a section whose fingerprint still matches is OMITTED with a
`<section>Same` marker. Self-healing by construction: any reset / truncation
/ rewrite changes the fingerprint, so the next response carries the full
section again. crc32 (not length) is the comparator because an in-flight
round's elapsed tick is a SAME-LENGTH mutation a length check cannot catch.

These tests drive the REAL route (importlib-loaded server, Quart test client,
in-memory registered task — the harness shape proven by
tests/test_duplicate_bubble_midturn_finish_reason.py):

  * plain GET (no incr) → byte-compatible full body, NO `fp` key;
  * incr first contact → full body + `fp`;
  * echo round-trip → fat sections omitted with `*Same` markers, meta intact;
  * append / mutable-tail rewrite → only a bounded toolRoundsDelta returns;
    a boundary mismatch falls back to the full section;
  * a garbage `ifp` degrades to a full body, never a 400/500.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_chat_poll_incremental.py -q
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
POLL = os.path.join(ROOT, 'routes', 'chat_poll_abort.py')


class _PollRig:
    """One importlib-loaded server + one mutable in-memory task, with a
    `get(query)` closure hitting the REAL route. Mirrors the dup-bubble
    suite's harness; loading server.py once keeps the suite fast."""

    def __init__(self, task_fields: dict):
        import asyncio
        import importlib.util
        import threading

        sys.path.insert(0, ROOT)
        os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
        os.environ.setdefault('TOFU_DB_PATH', '/tmp/incr_poll_gate.db')

        from lib.database import init_db
        init_db()
        from lib import auth_mode as _auth_mode
        self._auth = _auth_mode
        self._prev_auth = os.environ.pop('TOFU_AUTH_MODE', None)
        _auth_mode.reset_for_tests()
        _auth_mode.set_mode('open', set_by='incr-poll-test')

        from lib.tasks_pkg import tasks, tasks_lock
        self._tasks, self._lock = tasks, tasks_lock
        self.tid = 'tk-incr-poll'
        self.task = {
            'id': self.tid, 'convId': 'cv-incr', 'content': 'hello world',
            'thinking': 'think-1', 'error': None,
            'toolRounds': [{'tool': 'read_files', 'result': 'x' * 100}],
            'status': 'running', 'model': 'kimi-k3', '_contentEpoch': 0,
            'created_at': 1_700_000_000.0,
            'events': [], 'events_lock': threading.Lock(),
            'content_lock': threading.Lock(),
        }
        self.task.update(task_fields)
        with tasks_lock:
            tasks[self.tid] = self.task

        spec = importlib.util.spec_from_file_location(
            'server', os.path.join(ROOT, 'server.py'))
        mod = importlib.util.module_from_spec(spec)
        mod.__name__ = 'server'
        spec.loader.exec_module(mod)
        self._app = mod.app
        self._asyncio = asyncio

    def get(self, query: str = '') -> dict:
        captured: dict = {}

        async def _t():
            async with self._app.test_client() as client:
                r = await client.get(f'/api/v1/chat/poll/{self.tid}{query}')
                captured['status'] = r.status_code
                captured['json'] = await r.get_json()

        self._asyncio.run(_t())
        assert captured.get('status') == 200, captured
        body = captured.get('json') or {}
        # api_ok envelope — unwrap the data payload.
        return body.get('data', body)

    def close(self):
        with self._lock:
            self._tasks.pop(self.tid, None)
        self._auth.reset_for_tests()
        os.environ['TOFU_AUTH_MODE'] = self._prev_auth if self._prev_auth is not None else 'private'
        self._auth.reset_for_tests()


def _ifp_q(fp: dict) -> str:
    return '?incr=1&ifp=' + urllib.parse.quote(json.dumps(fp))


def test_incr_echo_roundtrip_and_self_healing():
    """The full narrative on one live task: byte-compat → first contact →
    echo omission → append / same-length rewrite / reset all come back full."""
    rig = _PollRig({})
    try:
        # 1. Plain GET (the four OTHER callers' shape): full, and NO fp key —
        #    the protocol is strictly opt-in.
        plain = rig.get('')
        assert plain.get('content') == 'hello world', plain
        assert plain.get('thinking') == 'think-1', plain
        assert plain.get('toolRounds'), plain
        assert 'fp' not in plain, f'non-incr callers must stay byte-identical: {plain}'

        # 2. First incr contact (no ifp): full body + fp to echo.
        first = rig.get('?incr=1')
        fp0 = first.get('fp')
        assert fp0, f'incr response must carry fp: {first}'
        assert first.get('content') == 'hello world', first
        assert first.get('contentEpoch') == 0, first
        assert fp0.get('c', [None])[0] == 0, fp0
        assert first.get('toolRounds'), first

        # 3. Echo round-trip with NOTHING changed: fat sections omitted,
        #    markers set, meta + fp still shipped.
        same = rig.get(_ifp_q(fp0))
        assert same.get('contentSame') is True and 'content' not in same, same
        assert same.get('thinkingSame') is True and 'thinking' not in same, same
        assert same.get('roundsSame') is True and 'toolRounds' not in same, same
        assert same.get('status') == 'running', f'meta must survive trimming: {same}'
        assert same.get('fp') == fp0, f'fp must re-state the current sections: {same}'

        # 4. Content APPEND → only content returns; the other sections stay omitted.
        with rig.task['content_lock']:
            rig.task['content'] = 'hello world!!'
        grown = rig.get(_ifp_q(fp0))
        assert grown.get('content') == 'hello world!!', grown
        assert grown.get('thinkingSame') is True, grown
        assert grown.get('roundsSame') is True, grown
        fp1 = grown['fp']

        # 5. Round APPEND → only the new round returns, not the old 100-byte
        #    result again on every poll.
        rig.task['toolRounds'].append({'tool': 'run_command', 'result': 'y' * 50})
        r2 = rig.get(_ifp_q(fp1))
        assert 'toolRounds' not in r2, r2
        assert r2.get('toolRoundsDelta') == {
            'start': 1,
            'rows': [{'tool': 'run_command', 'result': 'y' * 50}],
        }, r2
        assert r2.get('contentSame') is True, r2
        fp2 = r2['fp']

        # 6. SAME-LENGTH last-round mutation (the elapsed-tick shape) — replace
        #    only the mutable tail instead of re-sending the sealed prefix.
        rig.task['toolRounds'][-1]['result'] = 'y' * 49 + 'z'
        r3 = rig.get(_ifp_q(fp2))
        assert 'toolRounds' not in r3, r3
        assert r3.get('toolRoundsDelta') == {
            'start': 1,
            'rows': [{'tool': 'run_command', 'result': 'y' * 49 + 'z'}],
        }, (
            f'a same-length last-round mutation slipped past the fingerprint: {r3}')

        # 6b. If append raced with a mutation of the echoed boundary, the
        #     server cannot prove a safe patch base and must self-heal full.
        fp3 = r3['fp']
        rig.task['toolRounds'][-1]['result'] = 'q' * 50
        rig.task['toolRounds'].append({'tool': 'read_files', 'result': 'new'})
        raced = rig.get(_ifp_q(fp3))
        assert len(raced.get('toolRounds') or []) == 3, raced
        assert 'toolRoundsDelta' not in raced, raced

        # 7. SAME-LENGTH wholesale content rewrite (the retry/reset shape) —
        #    again crc-caught, full section returns.
        with rig.task['content_lock']:
            rig.task['content'] = 'HELLO WORLD!!'
        r4 = rig.get(_ifp_q(r3['fp']))
        assert r4.get('content') == 'HELLO WORLD!!', (
            f'a same-length content rewrite slipped past the fingerprint: {r4}')

        # 8. RESET EPOCH with the same bytes → content must return despite an
        #    unchanged crc, because the generation boundary authorizes shrink.
        before_epoch = r4['fp']
        with rig.task['content_lock']:
            rig.task['_contentEpoch'] = 1
        epoch_changed = rig.get(_ifp_q(before_epoch))
        assert epoch_changed.get('content') == 'HELLO WORLD!!', epoch_changed
        assert epoch_changed.get('contentEpoch') == 1, epoch_changed
        assert epoch_changed['fp']['c'][0] == 1, epoch_changed

        # 9. Garbage ifp → graceful FULL body (never a 400/500, never a trim).
        bad = rig.get('?incr=1&ifp=%7Boops')
        assert bad.get('content') == 'HELLO WORLD!!', bad
        assert bad.get('toolRounds'), bad
        assert bad.get('fp'), bad
    finally:
        rig.close()


def test_incr_never_trims_the_db_branch():
    """The DB branch (crashed/evicted task) must always ship the full
    checkpoint: it has no live task dict to fingerprint, and its consumers are
    one-shot recovery reads where the full body is the point."""
    src = open(POLL, encoding='utf-8').read()
    assert src.count('_maybe_trim_incr_poll(r, task)') == 1, (
        'the trim helper must wrap exactly the ONE in-memory return; the DB '
        'branch stays full-body by construction')


def test_incr_markers_and_fp_shape_are_source_pinned():
    """Wiring ratchet: the marker names are the frontend merge contract — a
    rename here without the sse_poll_fallback.js guards silently re-fattens
    every poll (or worse, drops a section the merge then keeps stale)."""
    src = open(POLL, encoding='utf-8').read()
    for marker in ("'contentSame'", "'thinkingSame'", "'roundsSame'",
                   "'toolRoundsDelta'",
                   "'endpointTurnsSame'", "'fp'"):
        assert marker in src, f'{marker} missing from the incr poll contract'
    fe = open(os.path.join(
        ROOT, 'frontend', 'src', 'runtime', 'app-runtime.js'),
              encoding='utf-8').read()
    for guard in ('data.contentSame', 'data.thinkingSame', 'data.roundsSame',
                  'data.toolRoundsDelta',
                  'data.endpointTurnsSame', '_pollFP', 'ifp'):
        assert guard in fe, f'{guard} missing from the poll-loop merge guards'


if __name__ == '__main__':
    test_incr_echo_roundtrip_and_self_healing()
    print('PASS test_incr_echo_roundtrip_and_self_healing')
    test_incr_never_trims_the_db_branch()
    print('PASS test_incr_never_trims_the_db_branch')
    test_incr_markers_and_fp_shape_are_source_pinned()
    print('PASS test_incr_markers_and_fp_shape_are_source_pinned')
    print('ALL GREEN')
