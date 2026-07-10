"""Premature-close → per-upstream health-scoring edge (lib/llm_dispatch/api.py).

``lib/llm/_sse_core.py::SSEAccumulator.finalize`` DETECTS a gateway closing the
SSE stream before ``[DONE]`` (sets ``usage['_missing_done']`` — but only when
the close was neither a client abort nor a clean finish). Historically that was
only *logged*, so the offending upstream kept getting re-picked. This suite pins
the fix: ``dispatch_stream`` / ``async_dispatch_stream`` now route that anomaly
through ``slot.record_truncation`` (the SAME soft-failure path the translate
retry loop uses), so after 3 consecutive premature closes the slot is cooled
with the existing exponential-backoff / 300s cap — no new hyperparameter.

Triple-neuter (baseline → negative → restore):
  - POSITIVE   : 3 premature closes → consecutive_errors bumps + cooldown set,
                 and both the sync + async paths behave identically.
  - NC-1       : disable the ``_cool_slot_on_premature_close`` call → the bad
                 slot stays hot → the positive assertion FAILS (proves the wire
                 is load-bearing, not the logging).
  - NC-2       : a CLEAN close (``saw_done`` → no ``_missing_done``) and a
                 CLIENT ABORT must NOT cool the slot → the over-cool guard;
                 breaking it (cooling on a clean close) makes this FAIL.

Run:  pytest tests/test_premature_close_cooldown.py -m unit
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_slot(model='qwen-plus', key='k0'):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-1234', model=model,
                capabilities={'text'})


class _FakeDispatcher:
    """Hands out a queued list of slots (mirrors test_dispatch_stream.py)."""

    def __init__(self, slots, all_slots=None):
        self._slots = list(slots)
        self.slots = list(all_slots or [])
        self.picks = 0

    def pick_and_reserve(self, **kwargs):
        self.picks += 1
        if not self._slots:
            return None
        slot = self._slots.pop(0)
        if slot is not None:
            slot.record_request()
        return slot

    def has_capable_slots(self, *a, **kw):
        return bool(self._slots)

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'


def _premature_usage():
    """The usage dict shape ``_sse_core.finalize`` emits on a premature close."""
    return {'completion_tokens': 3, '_missing_done': True,
            '_stream_anomaly': True, '_chunks_received': 2,
            'stream_elapsed_ms': 6100, 'trace_id': 'deadbeef'}


def _clean_usage():
    """A healthy stream: [DONE] seen → finalize never sets _missing_done."""
    return {'completion_tokens': 7, '_chunks_received': 40,
            'stream_elapsed_ms': 3200, 'trace_id': 'cafef00d'}


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)


def _run_sync_stream(monkeypatch, slot, usage):
    """Drive one sync dispatch_stream call whose upstream returns ``usage``."""
    from lib.llm_dispatch import api
    disp = _FakeDispatcher([slot])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

    def _fake_stream(body, **kwargs):
        return 'partial', 'stop', dict(usage)

    import lib.llm as llm_mod
    monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)
    return api.dispatch_stream([{'role': 'user', 'content': 'hi'}], log_prefix='[t]')


@pytest.mark.unit
class TestPrematureCloseCooling:
    def test_three_premature_closes_cool_the_slot(self, monkeypatch):
        slot = _make_slot()
        # Each call = a fresh dispatch that lands on the SAME slot and premature-
        # closes. After 3 consecutive soft failures the slot must be cooled.
        for i in range(3):
            _run_sync_stream(monkeypatch, slot, _premature_usage())
            assert slot.consecutive_errors == i + 1, (
                'premature close #%d should bump consecutive_errors' % (i + 1))
        assert slot.cooldown_until > time.time(), (
            'slot must be cooled after 3 premature closes')
        assert slot.total_errors >= 3
        # score() hard-blocks a cooled slot.
        assert slot.score() == float('inf')

    def test_single_premature_close_records_but_not_yet_cooled(self, monkeypatch):
        slot = _make_slot()
        _run_sync_stream(monkeypatch, slot, _premature_usage())
        assert slot.consecutive_errors == 1
        # <3 consecutive → not yet cooled (matches record_truncation threshold).
        assert slot.cooldown_until <= time.time()


@pytest.mark.unit
class TestOverCoolGuard:
    def test_clean_close_does_not_cool(self, monkeypatch):
        """NC-2 guard: a stream that saw [DONE] has no _missing_done → the slot
        stays hot. This is the failure mode we most fear (cooling a healthy
        upstream), so it is asserted explicitly."""
        slot = _make_slot()
        for _ in range(5):
            msg, finish, usage = _run_sync_stream(monkeypatch, slot, _clean_usage())
            assert msg == 'partial'
        assert slot.consecutive_errors == 0, 'clean closes must never cool a slot'
        assert slot.cooldown_until <= time.time()
        assert slot.last_success_time > 0

    def test_client_abort_does_not_cool(self, monkeypatch):
        """A client abort makes _sse_core skip _missing_done (guarded by
        ``not aborted``). Simulate by omitting the flag → no cooling."""
        slot = _make_slot()
        aborted_usage = {'_chunks_received': 5, 'stream_elapsed_ms': 900}
        for _ in range(4):
            _run_sync_stream(monkeypatch, slot, aborted_usage)
        assert slot.consecutive_errors == 0
        assert slot.cooldown_until <= time.time()


@pytest.mark.unit
class TestAsyncPathParity:
    def test_async_premature_close_cools_the_slot(self, monkeypatch):
        """The async sibling shares _sse_core.finalize → same _missing_done flag.
        Both dispatch sites must be wired or the fix is half-done."""
        from lib.llm_dispatch import api

        slot = _make_slot(key='ka')
        disp = _FakeDispatcher([slot, _make_slot(key='ka'), _make_slot(key='ka')])
        # Re-point every pick at the SAME slot object so consecutive_errors
        # accumulate on it across three dispatches.
        disp._slots = [slot, slot, slot]
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        async def _fake_astream(body, **kwargs):
            return 'partial', 'stop', _premature_usage()

        import lib.llm.astream as astream_mod
        monkeypatch.setattr(astream_mod, 'async_stream_chat', _fake_astream)

        async def _drive():
            for _ in range(3):
                await api.async_dispatch_stream(
                    [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        asyncio.run(_drive())
        assert slot.consecutive_errors == 3
        assert slot.cooldown_until > time.time()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
