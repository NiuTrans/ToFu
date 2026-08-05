#!/usr/bin/env python3
"""tests/test_swarm_snapshot_persist.py — durable swarm snapshot persistence.

ROOT-CAUSE fix verification. The swarm "Parallel Execution" panel's per-agent
state (``_swarmAgents``) is synthesized live on the FRONTEND and never
persisted, so a reload could only rebuild objective-only / ``unknown`` stubs —
catastrophically so for a FIRE-AND-FORGET swarm (spawned, never
``await_agents``-ed, the spawning turn ended). The backend now writes a durable
``_swarmSnapshot`` onto the ``spawn_agents`` tool round inside
``conversations.messages`` when the swarm settles (and incrementally per agent).

These tests drive the REAL production path:
  * ``execute_swarm_tool('spawn_agents', …)`` (the actual tool dispatch),
  * a real ``MasterOrchestrator`` + ``StreamingScheduler`` running on its own
    daemon thread (only the LLM-bound ``_build_sub_agent`` factory is patched
    to a canned ``_FakeAgent`` — no network),
  * a real ``conversations`` row holding the persisted spawn round,
  * the real ``lib.swarm.snapshot`` CAS write into the DB.

We assert the settled snapshot lands on the spawn round with each agent's REAL
status/preview/tokens/modifiedFiles — WITHOUT any ``await_agents`` call. The
neuter check at the bottom proves removing the snapshot write breaks it.

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/swarm_snap.db \
        python3 tests/test_swarm_snapshot_persist.py
or via pytest.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# DATA-LOSS GUARD: this module imports lib.* AT MODULE TOP (below), which can
# transitively freeze _core._BACKEND. A bare `python tests/x.py` skips conftest,
# and a plain setdefault('TOFU_DB_BACKEND','sqlite') is DEFEATED by the ambient
# TOFU_DB_BACKEND=postgres in .env. Force sqlite + assert a test DB BEFORE the
# imports. setUp() calls init_db(), so skip schema bootstrap here.
if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_swarm_snapshot_persist.__main__', init_schema=False)

from lib import agent_inbox
from lib.swarm.integration import (
    _active_sessions,
    _sessions_lock,
    execute_swarm_tool,
    get_active_session,
)
import pytest

from lib.swarm.protocol import SubAgentResult, SubAgentStatus

pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]


# ── Canned sub-agent (no LLM) — mirrors tests/test_swarm_async.py ──
class _FakeAgent:
    def __init__(self, spec, *, final_answer='', elapsed=0.05, tokens=123,
                 status=SubAgentStatus.COMPLETED.value, tool_log=None):
        self.spec = spec
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self.model = 'test-model'
        self.result = SubAgentResult(
            status=status,
            final_answer=final_answer or f'ANSWER<{spec.id}>: {spec.objective[:30]}',
            elapsed_seconds=elapsed,
            total_tokens=tokens,
            rounds_used=1,
            tool_log=list(tool_log or []),
        )
        self.max_rounds = spec.max_rounds

    def run(self):
        time.sleep(0.01)
        return self.result


def _patch_factory(by_id=None):
    by_id = by_id or {}

    def _factory(spec, **kwargs):
        return _FakeAgent(spec, **by_id.get(spec.id, {}))

    return patch('lib.swarm.master._build_sub_agent', side_effect=_factory)


def _wait_until(predicate, timeout=5.0, poll=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(poll)
    return False


def _seed_conv_with_spawn_round(conv_id, agent_specs):
    """Insert a conversations row whose last assistant message holds the
    persisted spawn_agents round — exactly the shape the frontend/backend
    write after the spawn tool runs in a normal turn."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert

    handle = {
        'status': 'async_launched', 'swarm_id': conv_id + '-t1',
        'agents': [{'id': s['id'], 'role': s['role'],
                    'objective': s['objective'],
                    'output_file': f'/x/{s["id"]}.log'} for s in agent_specs],
    }
    messages = [
        {'role': 'user', 'content': 'do parallel work', 'timestamp': 1},
        {'role': 'assistant', 'content': 'spawning…', 'timestamp': 2,
         'toolRounds': [{
             'roundNum': 1, 'toolName': 'spawn_agents', '_swarm': True,
             'status': 'done', 'toolContent': json.dumps(handle),
         }]},
    ]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'swarm-snap',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_spawn_round(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    if not row or not row[0]:
        return None
    msgs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    for m in reversed(msgs):
        if m.get('role') == 'assistant':
            for r in (m.get('toolRounds') or []):
                if r.get('toolName') == 'spawn_agents':
                    return r
    return None


def _cleanup(conv_id):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    with _sessions_lock:
        _active_sessions.pop(conv_id, None)
    agent_inbox.reset_for_test(conv_id)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    except Exception:
        pass


class TestSwarmSnapshotPersist(unittest.TestCase):

    def setUp(self):
        from lib.database import init_db
        init_db()
        self.conv_id = 'cv-snap-' + str(id(self))
        _cleanup(self.conv_id)

    def tearDown(self):
        _cleanup(self.conv_id)

    def test_fire_and_forget_snapshot_persisted_on_spawn_round(self):
        """Spawn → let the swarm settle → NEVER await. The durable snapshot
        must land on the persisted spawn round with each agent's REAL state."""
        specs = [
            {'id': 'aa11', 'role': 'researcher', 'objective': 'Survey A'},
            {'id': 'bb22', 'role': 'coder', 'objective': 'Patch B'},
        ]
        _seed_conv_with_spawn_round(self.conv_id, specs)

        # 'bb22' wrote two files → modifiedFiles must surface in the snapshot.
        by_id = {
            'aa11': {'final_answer': 'A FINDINGS', 'tokens': 700},
            'bb22': {'final_answer': 'B PATCH', 'tokens': 900,
                     'tool_log': [{'tool': 'write_file'}, {'tool': 'apply_diff'}]},
        }
        with _patch_factory(by_id):
            raw = execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': s['id'], 'role': s['role'],
                             'objective': s['objective']} for s in specs]},
                task={'id': self.conv_id + '-t1', 'convId': self.conv_id},
            )
            handle = json.loads(raw)
            self.assertEqual(handle['status'], 'async_launched')

            # Wait for the swarm to SETTLE (driver terminated) — no await call.
            sess = get_active_session(self.conv_id)
            self.assertIsNotNone(sess)
            self.assertTrue(_wait_until(lambda: sess.is_terminated),
                            'swarm driver did not terminate')

        # The snapshot is written from the driver thread; give the CAS write a
        # moment, then read the persisted spawn round straight from the DB.
        self.assertTrue(
            _wait_until(lambda: (_read_spawn_round(self.conv_id) or {}).get('_swarmSnapshot')),
            'spawn round never got a _swarmSnapshot')

        # The first CAS write may be an INCREMENTAL (unsettled) snapshot; the
        # settle write follows it (same pattern as the contention test below).
        # The invariant is unchanged — the settled snapshot MUST land — we just
        # wait for the async write instead of sampling mid-flight.
        self.assertTrue(
            _wait_until(
                lambda: ((_read_spawn_round(self.conv_id) or {})
                         .get('_swarmSnapshot') or {}).get('settled') is True,
                timeout=8.0),
            'settled snapshot never landed on the spawn round')

        rnd = _read_spawn_round(self.conv_id)
        snap = rnd['_swarmSnapshot']
        self.assertTrue(rnd.get('_swarm'), 'spawn round lost _swarm flag')
        self.assertTrue(snap.get('settled'), 'snapshot should be settled')
        agents = {a['id']: a for a in snap['agents']}
        self.assertEqual(set(agents), {'aa11', 'bb22'})

        # REAL per-agent state (the fire-and-forget case that previously
        # rendered as 'unknown' stubs).
        self.assertEqual(agents['aa11']['status'], 'done')
        self.assertEqual(agents['aa11']['preview'], 'A FINDINGS')
        self.assertEqual(agents['aa11']['tokens'], 700)
        self.assertEqual(agents['aa11']['modifiedFiles'], 0)

        self.assertEqual(agents['bb22']['status'], 'done')
        self.assertEqual(agents['bb22']['preview'], 'B PATCH')
        self.assertEqual(agents['bb22']['modifiedFiles'], 2)
        self.assertEqual(agents['bb22']['role'], 'coder')

        # The per-agent tool timeline must survive the reload path — without
        # it the recovered card renders no tools/timeline (the reported bug).
        self.assertEqual(agents['bb22']['tools'], ['write_file', 'apply_diff'])
        self.assertEqual([c['toolName'] for c in agents['bb22']['toolCalls']],
                         ['write_file', 'apply_diff'])
        self.assertTrue(all(c['status'] == 'done'
                            for c in agents['bb22']['toolCalls']))
        # An agent that used no tools carries empty lists, not missing keys.
        self.assertEqual(agents['aa11']['tools'], [])
        self.assertEqual(agents['aa11']['toolCalls'], [])

    def test_failed_agent_status_recorded(self):
        """A failed sub-agent is snapshotted as 'failed' with its error — not a
        fake 'done'."""
        specs = [{'id': 'ff99', 'role': 'general', 'objective': 'will fail'}]
        _seed_conv_with_spawn_round(self.conv_id, specs)
        by_id = {'ff99': {'status': SubAgentStatus.FAILED.value,
                          'final_answer': ''}}
        with _patch_factory(by_id):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'ff99', 'role': 'general',
                             'objective': 'will fail'}]},
                task={'id': self.conv_id + '-t1', 'convId': self.conv_id},
            )
            sess = get_active_session(self.conv_id)
            self.assertTrue(_wait_until(lambda: sess.is_terminated))
        self.assertTrue(
            _wait_until(lambda: (_read_spawn_round(self.conv_id) or {}).get('_swarmSnapshot')))
        snap = _read_spawn_round(self.conv_id)['_swarmSnapshot']
        a = {x['id']: x for x in snap['agents']}['ff99']
        self.assertEqual(a['status'], 'failed')


    def test_concurrent_frontend_writer_no_clobber(self):
        """THE race the quiescent tests miss: while the swarm settles, a
        frontend-style writer hammers the SAME conversation row's updated_at
        (mirrors _sync_partial_to_conversation / syncConversationToServer).

        Asserts: (a) the run completes without the driver thread raising
        ('dict changed size during iteration' would surface as a missing/blank
        snapshot), and (b) the FINAL persisted snapshot is settled:true with
        BOTH agents 'done' — no partial clobbered the settled write."""
        import threading
        from lib.database import DOMAIN_CHAT, get_thread_db

        specs = [
            {'id': 'cc11', 'role': 'researcher', 'objective': 'Survey C'},
            {'id': 'dd22', 'role': 'coder', 'objective': 'Patch D'},
        ]
        _seed_conv_with_spawn_round(self.conv_id, specs)

        stop = threading.Event()
        hammer_count = {'n': 0}
        hammer_err = {'e': None}

        # A live chat task whose toolRounds hold the SAME spawn round object
        # the driver will stamp — so _sync_partial_to_conversation serializes
        # it by-reference (via _merge_tool_rounds) on this thread while the
        # driver thread stamps _swarmSnapshot onto it. THIS is the #1 race.
        from lib.tasks_pkg.manager import (
            create_task, _sync_partial_to_conversation, tasks, tasks_lock)
        handle = {'status': 'async_launched', 'swarm_id': self.conv_id + '-t1',
                  'agents': [{'id': s['id'], 'role': s['role'],
                              'objective': s['objective']} for s in specs]}
        live_task = create_task(self.conv_id,
                                [{'role': 'user', 'content': 'x'}], {})
        live_task['content'] = 'streaming…'
        live_task['toolRounds'] = [{
            'roundNum': 1, 'toolName': 'spawn_agents', '_swarm': True,
            'status': 'done', 'toolContent': json.dumps(handle)}]

        def _hammer():
            """Run the REAL partial-sync repeatedly — serializes the shared
            round dict by-reference (the exact #1 cross-thread race) and bumps
            updated_at (forces CAS contention with the snapshot write)."""
            while not stop.is_set():
                try:
                    live_task['content'] += '.'
                    _sync_partial_to_conversation(live_task)
                    hammer_count['n'] += 1
                except Exception as e:
                    hammer_err['e'] = repr(e)
                time.sleep(0.002)

        hammer = threading.Thread(target=_hammer, daemon=True)
        hammer.start()
        try:
            by_id = {
                'cc11': {'final_answer': 'C DONE', 'tokens': 500, 'elapsed': 0.08},
                'dd22': {'final_answer': 'D DONE', 'tokens': 600, 'elapsed': 0.12,
                         'tool_log': [{'tool': 'write_file'}]},
            }
            with _patch_factory(by_id):
                execute_swarm_tool(
                    'spawn_agents',
                    {'agents': [{'id': s['id'], 'role': s['role'],
                                 'objective': s['objective']} for s in specs]},
                    task={'id': self.conv_id + '-t1', 'convId': self.conv_id},
                )
                sess = get_active_session(self.conv_id)
                self.assertTrue(_wait_until(lambda: sess.is_terminated),
                                'swarm driver did not terminate under contention')
            # Let the settle snapshot win against ongoing churn.
            settled_ok = _wait_until(
                lambda: ((_read_spawn_round(self.conv_id) or {})
                         .get('_swarmSnapshot') or {}).get('settled') is True,
                timeout=8.0)
        finally:
            stop.set()
            hammer.join(timeout=2.0)
            with tasks_lock:
                tasks.pop(live_task['id'], None)

        self.assertGreater(hammer_count['n'], 5,
                           'hammer thread never ran — race not exercised')
        # #1: a by-reference serialization racing the driver's stamp would
        # raise "dictionary changed size during iteration" here.
        self.assertIsNone(hammer_err['e'],
                          f'partial-sync raised under the stamp race: {hammer_err["e"]}')
        self.assertTrue(settled_ok,
                        'settled snapshot never landed under concurrent writer')
        snap = _read_spawn_round(self.conv_id)['_swarmSnapshot']
        self.assertTrue(snap['settled'])
        agents = {a['id']: a for a in snap['agents']}
        self.assertEqual(set(agents), {'cc11', 'dd22'})
        self.assertEqual(agents['cc11']['status'], 'done',
                         'a partial snapshot clobbered the settled one (cc11)')
        self.assertEqual(agents['dd22']['status'], 'done',
                         'a partial snapshot clobbered the settled one (dd22)')

    def test_neuter_proof_snapshot_write(self):
        """Monkeypatch the persist away → the round must have NO _swarmSnapshot.
        Proves the assertions above actually depend on the write."""
        import lib.swarm.master as _m
        specs = [{'id': 'ee11', 'role': 'general', 'objective': 'neuter'}]
        _seed_conv_with_spawn_round(self.conv_id, specs)
        orig = _m.MasterOrchestrator._persist_agent_snapshot
        _m.MasterOrchestrator._persist_agent_snapshot = lambda self: None
        try:
            with _patch_factory({}):
                execute_swarm_tool(
                    'spawn_agents',
                    {'agents': [{'id': 'ee11', 'role': 'general', 'objective': 'neuter'}]},
                    task={'id': self.conv_id + '-t1', 'convId': self.conv_id},
                )
                sess = get_active_session(self.conv_id)
                self.assertTrue(_wait_until(lambda: sess.is_terminated))
            time.sleep(0.2)
            rnd = _read_spawn_round(self.conv_id) or {}
            self.assertIsNone(rnd.get('_swarmSnapshot'),
                              'snapshot present despite neutered write')
        finally:
            _m.MasterOrchestrator._persist_agent_snapshot = orig


class TestSnapshotMonotonicAndScoping(unittest.TestCase):
    """Pure-function tests for the #2 monotonic guard, #3 abort coercion path
    output, and #4 per-wave filter — no DB, no threads."""

    def test_stamp_round_refuses_older_over_newer(self):
        from lib.swarm.snapshot import stamp_round
        rnd = {'toolName': 'spawn_agents', '_swarm': True}
        settled = {'agents': [{'id': 'a', 'status': 'done'}],
                   'settled': True, 'version': 100001}
        partial = {'agents': [{'id': 'a', 'status': 'running'}],
                   'settled': False, 'version': 0}
        self.assertTrue(stamp_round(rnd, settled))
        # Late partial must NOT clobber the settled one.
        self.assertFalse(stamp_round(rnd, partial))
        self.assertEqual(rnd['_swarmSnapshot']['version'], 100001)
        self.assertTrue(rnd['_swarmSnapshot']['settled'])

    def test_stamp_round_accepts_newer(self):
        from lib.swarm.snapshot import stamp_round
        rnd = {'toolName': 'spawn_agents', '_swarm': True}
        v0 = {'agents': [{'id': 'a', 'status': 'running'}], 'settled': False, 'version': 0}
        v1 = {'agents': [{'id': 'a', 'status': 'done'}], 'settled': False, 'version': 1}
        stamp_round(rnd, v0)
        self.assertTrue(stamp_round(rnd, v1))
        self.assertEqual(rnd['_swarmSnapshot']['version'], 1)

    def test_merge_tool_rounds_returns_independent_copies(self):
        """#1b (deterministic): the sync paths serialize _merge_tool_rounds'
        output while the swarm driver stamps _swarmSnapshot onto the LIVE task
        round. The merge MUST return shallow copies so a post-merge mutation of
        the live round can't change the list being json-serialized (the
        by-reference path raised 'dict changed size during iteration')."""
        from lib.tasks_pkg.manager import _merge_tool_rounds
        task = {'toolRounds': [
            {'roundNum': 1, 'toolName': 'spawn_agents', 'toolContent': '{}'}]}
        merged = _merge_tool_rounds(task)
        self.assertIsNot(merged[0], task['toolRounds'][0])
        task['toolRounds'][0]['_swarmSnapshot'] = {'settled': True}
        self.assertNotIn('_swarmSnapshot', merged[0],
                         '_merge_tool_rounds returned by-reference — a concurrent '
                         'stamp would mutate the dict being serialized')

    def test_abort_coerces_running_to_aborted(self):
        """#3: when the swarm is aborted (terminated + _aborted), an agent with
        no result must NOT persist as 'running'/'pending' under settled:true."""
        from lib.swarm.master import MasterOrchestrator
        from lib.swarm.protocol import SubTaskSpec
        m = MasterOrchestrator(
            task_id='t-abort', conv_id='cv-abort',
            specs=[SubTaskSpec(id='zz', role='general', objective='never finishes')])
        m._aborted = True
        m._terminated = True
        snap = m._build_agent_snapshot()
        self.assertTrue(snap['settled'])
        a = {x['id']: x for x in snap['agents']}['zz']
        self.assertEqual(a['status'], 'aborted')
        self.assertNotIn(a['status'], ('running', 'pending'))

    def test_snapshot_cas_write_is_throttled_settle_forced(self):
        """Write-amp guard: the DEDICATED full-blob CAS write is coalesced to
        one per interval for incremental per-agent calls, but force=True
        (the settle) always writes. The cheap live-task stamp is unaffected
        (it's an in-memory dict op, not counted here)."""
        from unittest.mock import patch
        from lib.swarm.master import MasterOrchestrator
        from lib.swarm.protocol import SubTaskSpec
        m = MasterOrchestrator(
            task_id='t-throttle', conv_id='cv-throttle',
            specs=[SubTaskSpec(id='q1', role='general', objective='o')])

        calls = {'n': 0}

        def _count(conv_id, agent_ids, snapshot):
            calls['n'] += 1
            return True

        # Big interval so nothing is "due" except a forced call.
        with patch('lib.swarm.master._SNAPSHOT_CAS_MIN_INTERVAL_S', 1000.0), \
             patch('lib.swarm.snapshot.persist_snapshot_to_conversation',
                   side_effect=_count):
            m._persist_agent_snapshot()              # 1st incremental — due (cold)
            m._persist_agent_snapshot()              # throttled
            m._persist_agent_snapshot()              # throttled
            self.assertEqual(calls['n'], 1,
                             'incremental writes were not coalesced')
            m._persist_agent_snapshot(force=True)    # settle — always writes
            self.assertEqual(calls['n'], 2,
                             'forced settle write did not bypass the throttle')

    def test_snapshot_tool_timeline_dedup_shape_and_cap(self):
        """_snapshot_tool_timeline rebuilds the panel's tool list + per-call
        timeline from tool_log: unique names (order preserved), row shape
        {toolName,argsBrief,status:'done'}, capped at the last 30 rows."""
        from lib.swarm.master import _SNAPSHOT_TOOLCALLS_CAP, _snapshot_tool_timeline
        # Empty / falsy input → two empty lists.
        self.assertEqual(_snapshot_tool_timeline(None), ([], []))
        self.assertEqual(_snapshot_tool_timeline([]), ([], []))

        log = [
            {'round': 1, 'tool': 'read_files', 'args_brief': 'a.py'},
            {'round': 1, 'tool': 'grep_search', 'args_brief': 'foo'},
            {'round': 2, 'tool': 'read_files', 'args_brief': 'b.py'},  # dup name
            {'round': 2, 'tool': None},                                 # skipped
            'not-a-dict',                                               # skipped
        ]
        tools, calls = _snapshot_tool_timeline(log)
        self.assertEqual(tools, ['read_files', 'grep_search'])
        self.assertEqual(len(calls), 3)
        # Row shape now also carries the tool RESULT text (preview/error) so a
        # reloaded panel shows what the live one did — legacy tool_log rows
        # predate those keys and default to ''.
        self.assertEqual(calls[0], {'toolName': 'read_files',
                                    'argsBrief': 'a.py', 'status': 'done',
                                    'preview': '', 'error': ''})
        self.assertEqual(calls[2]['argsBrief'], 'b.py')

        # Cap: keep only the last _SNAPSHOT_TOOLCALLS_CAP rows.
        big = [{'round': i, 'tool': 'read_files', 'args_brief': str(i)}
               for i in range(_SNAPSHOT_TOOLCALLS_CAP + 15)]
        _t, big_calls = _snapshot_tool_timeline(big)
        self.assertEqual(len(big_calls), _SNAPSHOT_TOOLCALLS_CAP)
        self.assertEqual(big_calls[-1]['argsBrief'],
                         str(_SNAPSHOT_TOOLCALLS_CAP + 14))  # tail preserved

    def test_filter_snapshot_scopes_to_wave(self):
        from lib.swarm.snapshot import filter_snapshot
        combined = {
            'agents': [
                {'id': 'w1a', 'status': 'done', 'tokens': 10},
                {'id': 'w1b', 'status': 'done', 'tokens': 20},
                {'id': 'w2a', 'status': 'running', 'tokens': 0},
            ],
            'settled': True, 'version': 100002,
        }
        wave1 = filter_snapshot(combined, {'w1a', 'w1b'})
        self.assertEqual({a['id'] for a in wave1['agents']}, {'w1a', 'w1b'})
        self.assertEqual(wave1['doneCount'], 2)
        self.assertEqual(wave1['totalTokens'], 30)
        wave2 = filter_snapshot(combined, {'w2a'})
        self.assertEqual({a['id'] for a in wave2['agents']}, {'w2a'})
        self.assertEqual(wave2['doneCount'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
