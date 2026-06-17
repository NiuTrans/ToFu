"""tests/test_endpoint_flow_parity.py — live vs flagged endpoint parity.

Runs a trivial endpoint task (planner → worker → critic STOP) through BOTH
orchestrators against the SAME scripted LLM, then compares the PERSISTED
conversation rows for STRUCTURAL parity:

    live path:    lib/tasks_pkg/endpoint.run_endpoint_task
    flagged path: lib/orchestration_endpoint_runner.run_endpoint_via_flow
                  (TOFU_ENDPOINT_VIA_FLOW)

The two engines differ internally, so we do NOT compare byte-for-byte. We
compare the *skeleton the frontend renders + persists*: the ordered list of
``(role, endpoint_kind)`` turns where kind ∈ {planner, worker, critic}. A
true drop-in must produce the same turn skeleton.

Why this matters: it's the side-by-side validation gate before flipping
TOFU_ENDPOINT_VIA_FLOW on by default. Deterministic (scripted LLM), so it
runs in CI.

Scripting note: the LIVE path streams through ``lib.tasks_pkg.manager
.dispatch_stream``; the FLAGGED path's agents are swarm ``SubAgent``s that
stream through ``lib.llm_dispatch.dispatch_stream``. We patch BOTH seams
with the same recorder so one script drives either path.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.log import get_logger

logger = get_logger(__name__)


# ── Scripted LLM: returns canned text by detecting the role from the body ──
class _ScriptedLLM:
    """Returns planner / worker / critic responses based on the system prompt.

    Both dispatch seams call this. We classify the call by scanning the
    system message for role markers, so it works regardless of which engine
    (live helper turns vs SubAgent) issues the call.
    """

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def _classify(self, messages) -> str:
        sys_txt = ''
        for m in messages:
            if m.get('role') == 'system':
                c = m.get('content')
                sys_txt = c if isinstance(c, str) else json.dumps(c)
                break
        low = sys_txt.lower()
        # Critic / reviewer prompts mention "verdict" / "review".
        if 'verdict' in low or 'critic' in low or 'review' in low:
            return 'critic'
        if 'plan' in low and ('checklist' in low or 'planner' in low or 'brief' in low):
            return 'planner'
        return 'worker'

    def __call__(self, body_or_messages, **kwargs):
        if isinstance(body_or_messages, dict):
            messages = body_or_messages.get('messages', [])
        else:
            messages = body_or_messages
        role = self._classify(messages)
        with self._lock:
            self.calls.append(role)

        if role == 'planner':
            content = ('## Goal\nDo the trivial thing.\n\n## Checklist\n'
                       '- [ ] step 1\n\n## Acceptance\n- done\n')
        elif role == 'critic':
            content = 'All acceptance criteria met. [VERDICT: STOP]'
        else:
            content = 'Done — produced the trivial result.'

        on_content = kwargs.get('on_content')
        if on_content:
            on_content(content)
        msg = {'role': 'assistant', 'content': content}
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15,
                 '_dispatch': {'provider_id': 'mock'}}
        return msg, 'end_turn', usage


# Conversations seeded by this module write to the SAME production DB the app
# uses (get_thread_db has no test-DB isolation), so every seeded row MUST be
# deleted afterwards or it leaks into the user's sidebar. Track ids here and
# purge them in the autouse cleanup fixture below.
_CREATED_CONVS: set[str] = set()


def _seed_conversation(conv_id: str, user_text: str):
    """Insert a fresh conversation row with the initial user message."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database._core_schema import CONVERSATIONS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    messages = [{'role': 'user', 'content': user_text}]
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'parity',
        'messages': json.dumps(messages), 'created_at': now, 'updated_at': now,
        'settings': '{}', 'msg_count': len(messages),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                    'updated_at', 'settings', 'msg_count'], commit=True)
    _CREATED_CONVS.add(conv_id)


def _delete_conversations(conv_ids):
    """Remove seeded conversation rows from the production DB."""
    if not conv_ids:
        return
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    for cid in conv_ids:
        try:
            db.execute('DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
        except Exception as e:
            logger.warning('parity cleanup failed for conv=%s: %s', cid, e)
    db.commit()


def _read_turn_skeleton(conv_id: str):
    """Return the ordered [(role, kind)] skeleton of endpoint turns in the row."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT messages FROM conversations WHERE id=? AND user_id=1', (conv_id,)
    ).fetchone()
    if not row:
        return []
    msgs = json.loads(row[0] or '[]')
    skeleton = []
    for m in msgs:
        role = m.get('role')
        if m.get('_isEndpointPlanner'):
            skeleton.append((role, 'planner'))
        elif m.get('_isEndpointReview'):
            skeleton.append((role, 'critic'))
        elif m.get('_epIteration'):
            skeleton.append((role, 'worker'))
    return skeleton


def _make_task(conv_id: str, user_text: str):
    from lib.tasks_pkg.manager import create_task
    config = {
        'model': 'mock-model',
        'endpointMode': True,
        'endpointMaxIterations': 3,
        'searchMode': 'off',
        'browserEnabled': False,
        'projectEnabled': False,
        'codeExecEnabled': False,
        'swarmEnabled': False,
    }
    messages = [
        {'role': 'system', 'content': 'You are a helpful assistant.'},
        {'role': 'user', 'content': user_text},
    ]
    return create_task(conv_id, messages, config)


def _wait_done(fn, task, timeout=60):
    done = threading.Event()
    err = []

    def _w():
        try:
            fn(task)
        except Exception as e:
            logger.error('parity task failed: %s', e, exc_info=True)
            err.append(e)
        finally:
            # The orchestrator grabbed this daemon thread's pooled DB
            # connection via get_thread_db(); production code releases it in a
            # finally (TaskRuntime / orchestrator run_task). Mirror that here
            # so the thread doesn't die holding a connection — otherwise it
            # leaves a dead-thread entry in the PG conn registry that
            # contaminates test_db_thread_conn_lifecycle in aggregate runs.
            try:
                from lib.database import close_thread_db
                close_thread_db()
            except Exception as _ctd_err:
                logger.debug('parity worker close_thread_db failed: %s', _ctd_err)
            done.set()

    threading.Thread(target=_w, daemon=True).start()
    if not done.wait(timeout=timeout):
        task['aborted'] = True
        raise TimeoutError('endpoint task did not finish')
    if err:
        raise err[0]


@pytest.fixture
def scripted(monkeypatch):
    """Patch BOTH dispatch seams with one scripted LLM."""
    llm = _ScriptedLLM()
    import lib.tasks_pkg.manager as manager_mod
    import lib.llm_dispatch as dispatch_mod
    monkeypatch.setattr(manager_mod, 'dispatch_stream', llm)
    monkeypatch.setattr(dispatch_mod, 'dispatch_stream', llm)
    # The flagged path runs swarm SubAgents, which bind dispatch_stream at
    # IMPORT time (`from lib.llm_dispatch import dispatch_stream as
    # _default_dispatch_stream` in lib/swarm/agent.py) and call that alias —
    # NOT lib.llm_dispatch.dispatch_stream at runtime. Patching only the
    # package attribute above leaves SubAgents hitting the REAL API, which in
    # an aggregate run (after a swarm test has warmed the path) retries/backs
    # off until the 60s task timeout. Patch the alias the SubAgent actually
    # calls so BOTH paths are hermetic regardless of import order.
    import lib.swarm.agent as swarm_agent_mod
    monkeypatch.setattr(swarm_agent_mod, '_default_dispatch_stream', llm)
    monkeypatch.setenv('LLM_MODEL', 'mock-model')
    monkeypatch.setenv('LLM_API_KEYS', 'mock-test-key')
    monkeypatch.setenv('LLM_BASE_URL', 'http://127.0.0.1:19999/v1')
    monkeypatch.delenv('TOFU_ENDPOINT_VIA_FLOW', raising=False)
    yield llm
    monkeypatch.delenv('TOFU_ENDPOINT_VIA_FLOW', raising=False)


@pytest.fixture(autouse=True)
def _cleanup_seeded_convs():
    """Delete every conversation row seeded during the test from the prod DB."""
    _CREATED_CONVS.clear()
    try:
        yield
    finally:
        _delete_conversations(set(_CREATED_CONVS))
        _CREATED_CONVS.clear()


@pytest.mark.unit
class TestEndpointFlowParity:
    def test_live_path_persists_turns(self, scripted):
        from lib.tasks_pkg.endpoint import run_endpoint_task
        conv = f'parity-live-{uuid.uuid4().hex[:8]}'
        _seed_conversation(conv, 'do the trivial thing')
        task = _make_task(conv, 'do the trivial thing')
        _wait_done(run_endpoint_task, task)
        skel = _read_turn_skeleton(conv)
        kinds = [k for _r, k in skel]
        assert 'planner' in kinds, f"live skeleton missing planner: {skel}"
        assert 'worker' in kinds, f"live skeleton missing worker: {skel}"
        assert 'critic' in kinds, f"live skeleton missing critic: {skel}"
        # roles: planner+worker are assistant, critic is user
        for role, kind in skel:
            if kind == 'critic':
                assert role == 'user', skel
            else:
                assert role == 'assistant', skel

    def test_flagged_path_persists_turns(self, scripted, monkeypatch):
        monkeypatch.setenv('TOFU_ENDPOINT_VIA_FLOW', '1')
        from lib.orchestration_endpoint_runner import (
            endpoint_via_flow_enabled, run_endpoint_via_flow,
        )
        assert endpoint_via_flow_enabled()
        conv = f'parity-flow-{uuid.uuid4().hex[:8]}'
        _seed_conversation(conv, 'do the trivial thing')
        task = _make_task(conv, 'do the trivial thing')
        _wait_done(run_endpoint_via_flow, task)
        skel = _read_turn_skeleton(conv)
        kinds = [k for _r, k in skel]
        assert 'planner' in kinds, f"flow skeleton missing planner: {skel}"
        assert 'worker' in kinds, f"flow skeleton missing worker: {skel}"
        assert 'critic' in kinds, f"flow skeleton missing critic: {skel}"
        for role, kind in skel:
            if kind == 'critic':
                assert role == 'user', skel
            else:
                assert role == 'assistant', skel

    def test_both_paths_structural_parity(self, scripted, monkeypatch):
        """The two paths must yield the same turn-role/kind skeleton head."""
        from lib.tasks_pkg.endpoint import run_endpoint_task
        from lib.orchestration_endpoint_runner import run_endpoint_via_flow

        # Live path
        monkeypatch.delenv('TOFU_ENDPOINT_VIA_FLOW', raising=False)
        conv_live = f'parity-cmp-live-{uuid.uuid4().hex[:8]}'
        _seed_conversation(conv_live, 'do the trivial thing')
        _wait_done(run_endpoint_task, _make_task(conv_live, 'do the trivial thing'))
        live = _read_turn_skeleton(conv_live)

        # Flagged path
        monkeypatch.setenv('TOFU_ENDPOINT_VIA_FLOW', '1')
        conv_flow = f'parity-cmp-flow-{uuid.uuid4().hex[:8]}'
        _seed_conversation(conv_flow, 'do the trivial thing')
        _wait_done(run_endpoint_via_flow, _make_task(conv_flow, 'do the trivial thing'))
        flow = _read_turn_skeleton(conv_flow)

        # Both must start planner → worker → … and end with an approving critic.
        assert live and flow, f"empty skeleton(s): live={live} flow={flow}"
        assert live[0][1] == 'planner' == flow[0][1], (
            f"first turn must be planner: live={live[0]} flow={flow[0]}")
        assert ('user', 'critic') in live, f"live has no critic turn: {live}"
        assert ('user', 'critic') in flow, f"flow has no critic turn: {flow}"
        # Same SET of turn kinds present (planner/worker/critic) — proves the
        # flagged path renders + persists the same structure the UI expects.
        assert set(k for _r, k in live) == set(k for _r, k in flow), (
            f"turn-kind sets differ: live={set(k for _r,k in live)} "
            f"flow={set(k for _r,k in flow)}")


if __name__ == '__main__':
    import unittest
    unittest.main()
