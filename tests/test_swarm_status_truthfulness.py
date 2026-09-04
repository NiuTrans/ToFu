"""tests/test_swarm_status_truthfulness.py — swarm status/事件真实性契约.

Root-cause repair for the recurring "Unconfirmed"（无结果）swarm panel:

  * Agent failure events MUST carry the reason (``error``) — the panel used
    to show a bare ❌ because only the model-facing inbox got the message.
  * A crashed driver MUST say so on the wire (``swarm_phase:error`` + the
    terminal ``complete`` frame carries ``error``) — previously it only
    logged, and the panel drifted into Unconfirmed or a false green.
  * ``get_swarm_status`` is THREE-STATE: live / definitively-terminated
    (in-memory OR durable row, restart-proof) / unknown (keep probing).
    The old two-state answer let an alias miss or a pre-rehydrate window
    false-settle a LIVE swarm into the Unconfirmed limbo.
  * The spawning event + handle carry ``swarmKey`` so the frontend
    reconciler probes with the key that always resolves.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pytest

from lib import agent_inbox
from lib.swarm.integration import (
    _active_sessions,
    _sessions_lock,
    abort_swarm,
    execute_swarm_tool,
    get_swarm_status,
)
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import (
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
)
from lib.swarm.scheduler import StreamingScheduler


pytestmark = pytest.mark.unit
_TEST_OWNER_USER_ID = 1



def _wait_until(predicate, timeout=5.0, poll=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(poll)
    return False


def _reset_global_state(*keys: str):
    with _sessions_lock:
        for k in keys:
            _active_sessions.pop(k, None)
    for k in keys:
        agent_inbox.reset_for_test(k)


class _FakeAgent:
    """Instant SubAgent stand-in (no LLM), configurable terminal state."""

    def __init__(self, spec: SubTaskSpec, *,
                 status: str = SubAgentStatus.COMPLETED.value,
                 error_message: str = '',
                 gate: threading.Event | None = None):
        self.spec = spec
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self.gate = gate
        self.result = SubAgentResult(
            status=status,
            final_answer='' if status != SubAgentStatus.COMPLETED.value
                         else f'Answer {spec.id}',
            error_message=error_message,
            elapsed_seconds=0.05,
            total_tokens=10,
            rounds_used=1,
        )

    def run(self) -> SubAgentResult:
        if self.gate is not None:
            self.gate.wait(5)
        time.sleep(0.01)
        return self.result


def _factory_for(fake_cfg: dict[str, dict] | None = None,
                 gate: threading.Event | None = None):
    fake_cfg = fake_cfg or {}

    def _factory(spec, **kwargs):
        cfg = dict(fake_cfg.get(spec.id, {}))
        if gate is not None:
            cfg['gate'] = gate
        return _FakeAgent(spec, **cfg)

    return patch('lib.swarm.master._build_sub_agent', side_effect=_factory)


# ═════════════════════════════════════════════════════════
#  swarmKey on the wire (event + handle)
# ═════════════════════════════════════════════════════════

class TestSpawnCarriesSwarmKey(unittest.TestCase):

    def setUp(self):
        self.task_id = 'tkey-' + str(id(self))
        self.conv_id = 'convkey-' + str(id(self))
        _reset_global_state(self.task_id, self.conv_id)

    def tearDown(self):
        _reset_global_state(self.task_id, self.conv_id)

    def test_spawning_event_and_handle_carry_swarm_key(self):
        events: list[dict] = []
        with _factory_for():
            raw = execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'k1', 'objective': 'probe the key'}]},
                task={
                    'id': self.task_id,
                    'convId': self.conv_id,
                    '_userId': _TEST_OWNER_USER_ID,
                },
                on_event=events.append,
            )
            session = _active_sessions.get(self.conv_id)
            self.assertIsNotNone(session)
            self.assertTrue(
                _wait_until(lambda: session.is_terminated),
                'swarm did not settle in time')

        handle = json.loads(raw)
        # The conv-scoped key, not the per-turn task id.
        self.assertEqual(handle['swarm_key'], self.conv_id)
        spawning = [e for e in events
                    if e.get('type') == 'swarm_phase'
                    and e.get('phase') == 'spawning']
        self.assertTrue(spawning, 'no spawning event captured')
        self.assertEqual(spawning[0]['swarmKey'], self.conv_id)


# ═════════════════════════════════════════════════════════
#  Error transparency: agent failure + driver crash
# ═════════════════════════════════════════════════════════

class TestAgentFailureCarriesError(unittest.TestCase):

    def setUp(self):
        self.task_id = 'tfail-' + str(id(self))
        _reset_global_state(self.task_id)

    def tearDown(self):
        _reset_global_state(self.task_id)

    def test_agent_complete_event_carries_error_message(self):
        events: list[dict] = []
        with _factory_for({'bad': {
                'status': SubAgentStatus.FAILED.value,
                'error_message': 'gateway 500: upstream wedged'}}):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'bad', 'objective': 'will fail'}]},
                task={'id': self.task_id, '_userId': _TEST_OWNER_USER_ID},
                on_event=events.append,
            )
            self.assertTrue(
                _wait_until(lambda: any(
                    e.get('type') == 'swarm_agent_complete'
                    for e in events)),
                'agent complete event never arrived')

        done = [e for e in events if e.get('type') == 'swarm_agent_complete']
        self.assertEqual(done[0]['status'], SubAgentStatus.FAILED.value)
        self.assertEqual(done[0]['error'], 'gateway 500: upstream wedged')

    def test_driver_crash_emits_error_phase_before_complete(self):
        events: list[dict] = []
        m = MasterOrchestrator(
            task_id=self.task_id, conv_id='',
            user_id=1,
            specs=[SubTaskSpec(role='general', objective='x', id='d1')],
            on_progress=events.append,
            abort_check=lambda: False,
            all_tools=[],
        )
        with _factory_for(), patch.object(
                StreamingScheduler, 'iter_completions',
                side_effect=RuntimeError('driver boom')):
            m.run_in_background()
            self.assertTrue(_wait_until(lambda: m.is_terminated),
                            'driver thread did not exit')

        self.assertEqual(m._driver_error, 'RuntimeError: driver boom')
        phases = [e for e in events if e.get('type') == 'swarm_phase']
        err = [e for e in phases if e.get('phase') == 'error']
        comp = [e for e in phases if e.get('phase') == 'complete']
        self.assertTrue(err, 'no swarm_phase:error event on driver crash')
        self.assertIn('driver boom', err[0]['error'])
        self.assertTrue(comp, 'no terminal complete frame after crash')
        self.assertIn('driver boom', comp[0].get('error', ''))
        # error MUST precede the complete frame so the UI never promotes
        # unreported agents to a false done first.
        self.assertLess(events.index(err[0]), events.index(comp[0]))


# ═════════════════════════════════════════════════════════
#  get_swarm_status — three-state contract
# ═════════════════════════════════════════════════════════

class TestSwarmStatusThreeState(unittest.TestCase):

    def setUp(self):
        self.task_id = 'tstat-' + str(id(self))
        _reset_global_state(self.task_id)

    def tearDown(self):
        _reset_global_state(self.task_id)

    def test_live_session_is_active_and_known(self):
        gate = threading.Event()
        # Everything stays INSIDE the patch window: agents are created on
        # scheduler pool threads AFTER spawn returns, so releasing the patch
        # (or the gate) early lets a REAL SubAgent leak out and make a real
        # LLM call.
        with _factory_for(gate=gate):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 's1', 'objective': 'slow work'}]},
                task={'id': self.task_id, '_userId': _TEST_OWNER_USER_ID},
            )
            status = get_swarm_status(
                self.task_id,
                user_id=_TEST_OWNER_USER_ID,
            )
            self.assertIsNotNone(status)
            self.assertIs(status['active'], True)
            self.assertIs(status['known'], True)
            self.assertIs(status['terminated'], False)
            gate.set()
            session = _active_sessions.get(self.task_id)
            self.assertIsNotNone(session)
            self.assertTrue(_wait_until(lambda: session.is_terminated),
                            'swarm did not settle after gate release')

    def test_foreign_owner_cannot_observe_or_abort_live_session(self):
        gate = threading.Event()
        with _factory_for(gate=gate):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 's1', 'objective': 'slow work'}]},
                task={'id': self.task_id, '_userId': _TEST_OWNER_USER_ID},
            )
            session = _active_sessions.get(self.task_id)
            self.assertIsNotNone(session)

            self.assertIsNone(get_swarm_status(
                self.task_id,
                user_id=_TEST_OWNER_USER_ID + 1,
            ))
            result = abort_swarm(
                self.task_id,
                user_id=_TEST_OWNER_USER_ID + 1,
            )
            self.assertIs(result['success'], False)
            self.assertIs(_active_sessions.get(self.task_id), session)
            self.assertFalse(session.is_terminated)

            gate.set()
            self.assertTrue(_wait_until(lambda: session.is_terminated),
                            'swarm did not settle after gate release')

    def test_terminated_in_memory_session_reports_agents_with_error(self):
        with _factory_for({'f1': {
                'status': SubAgentStatus.FAILED.value,
                'error_message': 'provider 429 storm'}}):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'f1', 'objective': 'will fail'}]},
                task={'id': self.task_id, '_userId': _TEST_OWNER_USER_ID},
            )
            session = _active_sessions.get(self.task_id)
            self.assertIsNotNone(session)
            self.assertTrue(_wait_until(lambda: session.is_terminated))

        status = get_swarm_status(
            self.task_id,
            user_id=_TEST_OWNER_USER_ID,
        )
        self.assertIs(status['active'], False)
        self.assertIs(status['known'], True)
        self.assertIs(status['terminated'], True)
        agent = {a['id']: a for a in status['agents']}['f1']
        self.assertEqual(agent['status'], SubAgentStatus.FAILED.value)
        self.assertEqual(agent['error'], 'provider 429 storm')


class TestSwarmStatusPersistenceFallback(unittest.TestCase):
    """No in-memory session — the durable row must answer, restart-proof.

    Uses a real sqlite StorageSupervisor rooted in a temp dir and redirects
    ``persistence._storage`` at it (the test_swarm_async.TestRehydration
    pattern): the process-wide storage client stays fenced in a dev shell
    with no sidecar, and persistence must never depend on it here.
    """

    @classmethod
    def setUpClass(cls):
        from lib.storage import StorageSupervisor
        from lib.swarm import persistence as persistence

        cls._storage_root = tempfile.TemporaryDirectory(
            prefix='tofu-swarm-status-')
        cls._storage_supervisor = StorageSupervisor(
            project_root=Path(cls._storage_root.name), backend='sqlite',
            startup_timeout=60)
        cls._storage_supervisor.start()
        cls._storage_patch = patch.object(
            persistence, '_storage',
            side_effect=lambda **_kwargs: cls._storage_supervisor.client)
        cls._storage_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._storage_patch.stop()
        cls._storage_supervisor.stop()
        cls._storage_root.cleanup()

    def setUp(self):
        from lib.swarm import persistence as p
        self.p = p
        self.key = 'convpersist-' + str(id(self))
        _reset_global_state(self.key)

    def tearDown(self):
        self.p.delete_session(self.key)
        _reset_global_state(self.key)

    def test_terminated_persisted_row_is_definitive_and_carries_error(self):
        self.p.save_session(self.key, conv_id=self.key, task_id='t-old',
                            specs=[{'id': 'p1', 'role': 'coder', 'objective': 'X'}],
                            config={'user_id': _TEST_OWNER_USER_ID},
                            status='running')
        self.p.save_agent(self.key, 'p1', role='coder', objective='X',
                          status='failed', messages=[],
                          result={'status': 'failed',
                                  'error_message': 'OOM in tool loop'},
                          rounds_used=3)
        self.p.mark_session_terminated(self.key)

        status = get_swarm_status(
            self.key,
            user_id=_TEST_OWNER_USER_ID,
        )
        self.assertIsNotNone(status)
        self.assertIs(status['active'], False)
        self.assertIs(status['known'], True)
        self.assertIs(status['terminated'], True)
        self.assertEqual(status['source'], 'persisted')
        agent = {a['id']: a for a in status['agents']}['p1']
        self.assertEqual(agent['status'], 'failed')
        self.assertEqual(agent['error'], 'OOM in tool loop')
        self.assertIsNone(get_swarm_status(
            self.key,
            user_id=_TEST_OWNER_USER_ID + 1,
        ))

    def test_running_persisted_row_without_memory_is_ambiguous(self):
        """Pre-rehydrate restart window: the row says 'running' but the
        process has nothing — NEVER a settle signal (keep probing)."""
        self.p.save_session(self.key, conv_id=self.key, task_id='t-old',
                            specs=[{'id': 'p2', 'role': 'coder', 'objective': 'Y'}],
                            config={'user_id': _TEST_OWNER_USER_ID},
                            status='running')
        self.p.save_agent(self.key, 'p2', role='coder', objective='Y',
                          status='running', messages=[], rounds_used=1)

        status = get_swarm_status(
            self.key,
            user_id=_TEST_OWNER_USER_ID,
        )
        self.assertIsNotNone(status)
        self.assertIsNone(status['active'])
        self.assertIs(status['known'], False)
        self.assertEqual(status['persisted_status'], 'running')

    def test_no_record_anywhere_returns_none(self):
        self.assertIsNone(get_swarm_status(
            self.key,
            user_id=_TEST_OWNER_USER_ID,
        ))

    def test_malformed_persisted_owner_fails_closed_without_route_error(self):
        self.p.save_session(
            self.key, conv_id=self.key, task_id='t-invalid-owner',
            specs=[{'id': 'p3', 'role': 'coder', 'objective': 'Z'}],
            config={'user_id': 'not-a-number'}, status='running')
        self.p.save_agent(
            self.key, 'p3', role='coder', objective='Z', status='running',
            messages=[], rounds_used=1)

        self.assertIsNone(get_swarm_status(
            self.key,
            user_id=_TEST_OWNER_USER_ID,
        ))
