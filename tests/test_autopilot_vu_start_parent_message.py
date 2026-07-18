"""Backend regression: `autopilot_vu_start` carries the parent's SETTLED
finish metadata as `parentMessage`.

WHY
---
When autopilot kicks in, the frontend EARLY-FINALIZES the parent worker bubble
at `autopilot_vu_start` so the VU can own the single `#streaming-msg` substrate.
The parent worker's SETTLED finish metadata (finishReason / usage / cost),
however, is committed to `task['_committedMsg']` by the pre-emit
`_sync_result_to_conversation` and only shipped later on the parent `done`
event — which the backend deliberately WITHHOLDS until the whole VU stream
completes (so the follow-up baton can ride on it). Result: the parent bubble's
finish bar showed ONLY the model tag for the entire VU turn (12–52s), reported
as "the finish tag bar for the previous agent's result is always incomplete".

THE FIX (this suite locks it in): `maybe_run_autopilot` attaches
`task['_committedMsg']` — the EXACT committed dict — onto the
`autopilot_vu_start` event as `parentMessage`, so the frontend can complete the
parent's finish bar AT HANDOFF. The authoritative copy still rides `done`
verbatim (harmless no-op repaint).

SKIP-PATH FALLBACK ("under no circumstances incomplete"): when `_committedMsg`
is unset (freshness guard / CAS-exhaustion / inline), the hook builds a MINIMAL
`parentMessage` from the task's OWN settled fields — `finishReason` / `usage` /
`model` / `provider_id` / `apiRounds` — which the orchestrator finalize stamps
on the task BEFORE this hook runs. So vu_start ALWAYS carries enough to complete
the bar whenever the task actually has finish data. The ONLY circumstance that
still omits `parentMessage` is a task with genuinely NOTHING to show (no
finishReason AND no usage — e.g. an errored turn with no metering); there the
bar legitimately waits for the `done` re-render.

Guards: (1) committed dict → parentMessage IS that dict; (2) skip path WITH
settled task fields → parentMessage built from them (finishReason+usage
present); (3) NC — genuinely-empty task (no finishReason, no usage) → NO
parentMessage, proving the payload is sourced from real data, never fabricated.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg import autopilot as ap


def _wire(monkeypatch):
    """Stub the eligibility guards + the VU run so maybe_run_autopilot reaches
    the vu_start emission deterministically and then bails cleanly (no follow-up
    spawn, no DB writes)."""
    import lib.tasks_pkg.manager as mgr

    captured: list = []
    monkeypatch.setattr(mgr, 'append_event',
                        lambda task, event: captured.append(event))

    # Eligibility gauntlet → all pass.
    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda cid: False)
    monkeypatch.setattr(ap, '_successor_already_running', lambda task, cid: False)
    monkeypatch.setattr(ap, '_get_or_persist_run_id', lambda cid: 'run-1')

    # VU produces no reply → bail right after vu_start (emits vu_cancel, returns
    # None). We only care that vu_start was emitted with the right payload.
    monkeypatch.setattr(ap, 'run_virtual_user', lambda task, vu_msg_id: None)

    return captured


def _make_task(committed=None, settled=None):
    task = {
        'id': 'parent-abc123',
        'convId': 'conv1',
        'messages': [{'role': 'user', 'content': 'hi'},
                     {'role': 'assistant', 'content': 'done'}],
        'config': {},
        'events': [],
        'events_lock': threading.Lock(),
        'aborted': False,
    }
    if committed is not None:
        task['_committedMsg'] = committed
    # Task-level settled fields the orchestrator finalize stamps BEFORE the
    # autopilot hook (finishReason/usage/model/provider_id/apiRounds). The
    # skip-path fallback sources parentMessage from these when _committedMsg
    # is absent.
    if settled:
        task.update(settled)
    return task


_COMMITTED = {
    'role': 'assistant',
    '_msgId': 'worker-1',
    'content': 'Here is the worker reply.',
    'model': 'aws.claude-opus-4.8',
    'finishReason': 'end_turn',
    'usage': {'input_tokens': 4200, 'output_tokens': 380},
    'cost': {'costCny': 0.0123},
}


def _vu_start(captured):
    for e in captured:
        if e.get('type') == 'autopilot_vu_start':
            return e
    return None


def test_vu_start_carries_committed_parent_message(monkeypatch):
    captured = _wire(monkeypatch)
    ap.maybe_run_autopilot(_make_task(committed=_COMMITTED))

    ev = _vu_start(captured)
    assert ev is not None, f'no autopilot_vu_start emitted; got {captured}'
    assert 'parentMessage' in ev, \
        f'vu_start must carry parentMessage; got {ev}'
    pm = ev['parentMessage']
    # It is the EXACT committed dict — the finish-bar-bearing fields are there.
    assert pm['finishReason'] == 'end_turn'
    assert pm['usage']['output_tokens'] == 380
    assert pm['cost']['costCny'] == 0.0123
    assert pm is _COMMITTED, 'parentMessage should be the committed dict itself'


def test_skip_path_falls_back_to_task_settled_fields(monkeypatch):
    """Skip path (no `_committedMsg`) but the task HAS settled finish fields →
    vu_start carries a MINIMAL parentMessage built from them, so the parent bar
    is STILL complete at handoff. This closes the last "sometimes incomplete"
    circumstance — vu_start never omits the finish payload when the task has
    one."""
    captured = _wire(monkeypatch)
    ap.maybe_run_autopilot(_make_task(
        committed=None,
        settled={
            'finishReason': 'end_turn',
            'usage': {'input_tokens': 4200, 'output_tokens': 380},
            'model': 'aws.claude-opus-4.8',
            'provider_id': 'aws',
            'apiRounds': [{'round': 0, 'usage': {'output_tokens': 380}}],
        }))

    ev = _vu_start(captured)
    assert ev is not None, f'no autopilot_vu_start emitted; got {captured}'
    assert 'parentMessage' in ev, \
        f'skip path with settled fields must still carry parentMessage; got {ev}'
    pm = ev['parentMessage']
    # The bar-drawing trio (✓ + token-tag + cost-tag via calcCostCny) is present.
    assert pm['finishReason'] == 'end_turn'
    assert pm['usage']['output_tokens'] == 380
    assert pm['model'] == 'aws.claude-opus-4.8'
    assert pm['provider_id'] == 'aws', \
        'provider_id must ride so the frontend can compute the cost-tag'
    assert pm is not None and 'role' in pm
    # It is a FRESHLY built dict, NOT the (absent) committed one.
    assert pm.get('_committedMsg') is None


def test_genuinely_empty_task_omits_parent_message(monkeypatch):
    """The ONLY legitimate omit: no `_committedMsg` AND no settled finish data
    (no finishReason, no usage — e.g. an errored turn with no metering). vu_start
    carries NO `parentMessage`; the bar legitimately waits for the `done`
    re-render. Proves the payload is sourced from REAL data, never fabricated."""
    captured = _wire(monkeypatch)
    ap.maybe_run_autopilot(_make_task(committed=None, settled=None))

    ev = _vu_start(captured)
    assert ev is not None, f'no autopilot_vu_start emitted; got {captured}'
    assert 'parentMessage' not in ev, \
        f'genuinely-empty task must omit parentMessage; got {ev}'
