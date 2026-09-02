"""pt_a21cd6eb 交付① — bounded 429-saturation escalation guards.

2026-08-01 incident: a VU carrier spun 3900+ cycles / ~75min on a shared-key
model whose every slot was per-minute 429-saturated — zero tokens, zero
fallback attempts — while the fallback model was demonstrably healthy.
`dispatch_chat` / `dispatch_stream` counted 429s as free retries forever
(api.py: "429 loops forever"), and `strict_model=True` pinned the slot pool,
so llm_fallback never got a signal. The escalation raises
``RateLimitError(is_saturation=True)`` once every candidate slot has been
continuously saturated past ``TOFU_429_SATURATION_SECS``.

The default budget remains disabled because a 429 is a waitable capacity
signal, not evidence that the model is dead. Deployments may explicitly set a
positive ``TOFU_429_SATURATION_SECS`` to opt into bounded escalation.
"""

import threading
import time as _real_time

import pytest

import lib.llm_dispatch.api as api
from lib.llm_errors import RateLimitError


# ── fakes ────────────────────────────────────────────────────────────

class _FakeClock:
    """Deterministic replacement for api.py's `time` module reference."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, secs):
        self.sleeps += 1
        self.now += secs


class _FakeSlot:
    def __init__(self, key_name='k1', model='m1'):
        self.key_name = key_name
        self.model = model
        self.api_key = 'fake-key'
        self.base_url = ''
        self.extra_headers = None
        self.oauth = ''
        self.protocol = 'openai'
        self.provider_id = 'fake'
        self.thinking_format = ''
        self.adapter = None  # E4 commit 2383ae9a added slot.adapter
        self.stream_only = False
        self.capabilities = {'text'}
        self.consecutive_errors = 0
        self.cooldown_until = 0
        self.cooldown_reason = ''
        self.error_calls = []
        self.success_calls = []

    def record_error(self, **kw):
        self.error_calls.append(kw)

    def record_success(self, latency, **kw):
        self.success_calls.append((latency, kw))


class _FakeDispatcher:
    def __init__(self, slot):
        self.slots = [slot]

    def pick_and_reserve(self, **kw):
        return self.slots[0]

    def has_capable_slots(self, *a, **kw):
        return True

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'

    def sticky_cooldown_remaining_s(self, *a, **kw):
        return None

    def note_shared_contention(self, slot):
        pass


@pytest.fixture
def fake_env(monkeypatch):
    """Wire a fake dispatcher + deterministic clock into api.py."""
    slot = _FakeSlot()
    clock = _FakeClock()
    monkeypatch.setattr(api, 'get_dispatcher', lambda: _FakeDispatcher(slot))
    monkeypatch.setattr(api, 'time', clock)
    monkeypatch.setenv('TOFU_429_SATURATION_SECS', '120')
    return slot, clock


# ── dispatch_stream ──────────────────────────────────────────────────

@pytest.mark.unit
class TestStreamSaturationEscalation:

    def test_escalates_after_budget(self, monkeypatch, fake_env):
        slot, clock = fake_env
        monkeypatch.setattr('lib.llm.stream_chat',
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RateLimitError('slow down', status_code=429)))
        with pytest.raises(RateLimitError) as ei:
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='m1', strict_model=True, log_prefix='[T]')
        err = ei.value
        assert err.is_saturation is True
        assert err.is_quota is False, (
            'saturation must NOT mark quota — the keys are healthy')
        assert err.status_code == 429
        assert clock.now > 120, f'escalated too early: clock={clock.now}'
        assert all(not c.get('is_quota_exhausted') for c in slot.error_calls), (
            'saturation path must never feed the key-exhaustion channel')

    def test_under_budget_429_then_success(self, monkeypatch, fake_env):
        slot, clock = fake_env
        calls = {'n': 0}

        def _chat(*a, **kw):
            calls['n'] += 1
            if calls['n'] <= 3:
                raise RateLimitError('slow down', status_code=429)
            return ('ok-text', 'stop',
                    {'prompt_tokens': 3, 'completion_tokens': 2})

        monkeypatch.setattr('lib.llm.stream_chat', _chat)
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}],
            prefer_model='m1', strict_model=True, log_prefix='[T]')
        assert finish == 'stop' and msg == 'ok-text'
        assert clock.now < 120


# ── dispatch_chat ────────────────────────────────────────────────────

@pytest.mark.unit
class TestChatSaturationEscalation:

    def test_escalates_after_budget(self, monkeypatch, fake_env):
        slot, clock = fake_env
        monkeypatch.setattr('lib.llm.chat',
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RateLimitError('slow down', status_code=429)))
        with pytest.raises(RateLimitError) as ei:
            api.dispatch_chat(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='m1', strict_model=True, log_prefix='[T]')
        assert ei.value.is_saturation is True
        assert clock.now > 120

    def test_under_budget_429_then_success(self, monkeypatch, fake_env):
        slot, clock = fake_env
        calls = {'n': 0}

        def _chat(*a, **kw):
            calls['n'] += 1
            if calls['n'] <= 3:
                raise RateLimitError('slow down', status_code=429)
            return ('ok-text', {'completion_tokens': 2})

        monkeypatch.setattr('lib.llm.chat', _chat)
        content, usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}],
            prefer_model='m1', strict_model=True, log_prefix='[T]')
        assert content == 'ok-text'
        assert clock.now < 120
        assert usage['_dispatch']['429_retries'] == 3
        assert usage['_dispatch']['upstream_429_retries'] == 3
        assert usage['_dispatch']['slot_wait_cycles'] == 0

    def test_env_zero_restores_legacy_infinite_rotation(
            self, monkeypatch, fake_env):
        slot, clock = fake_env
        monkeypatch.setenv('TOFU_429_SATURATION_SECS', '0')
        calls = {'n': 0}

        def _chat(*a, **kw):
            calls['n'] += 1
            if calls['n'] <= 5:
                raise RateLimitError('slow down', status_code=429)
            return ('ok-text', {'completion_tokens': 2})

        monkeypatch.setattr('lib.llm.chat', _chat)
        # Each 0.3s sleep advances the fake clock 500s instead — way past
        # any budget — so only env=0 lets the loop reach the success call.
        clock.sleep = lambda s: setattr(clock, 'now', clock.now + 500)
        content, usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}],
            prefer_model='m1', strict_model=True, log_prefix='[T]')
        assert content == 'ok-text'
        assert clock.now > 120, 'test did not actually pass the budget mark'


# ── waitable default ────────────────────────────────────────────────

@pytest.mark.unit
def test_default_budget_waits_indefinitely(monkeypatch):
    """No env keeps capacity throttling on the selected model."""
    monkeypatch.delenv('TOFU_429_SATURATION_SECS', raising=False)
    assert api._saturation_budget_secs() == 0


@pytest.mark.unit
def test_invalid_budget_falls_back_to_waitable_default(monkeypatch):
    monkeypatch.setenv('TOFU_429_SATURATION_SECS', 'not-a-number')
    assert api._saturation_budget_secs() == 0


# ── llm_fallback composition ─────────────────────────────────────────

@pytest.mark.unit
class TestFallbackSwallowsSaturation:

    def test_saturation_triggers_model_swap(self, monkeypatch):
        import lib.tasks_pkg.llm_fallback._call as fb_pkg
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback

        events = []

        def _fake_stream(task, body, tag='', on_tool_call_ready=None):
            if body.get('model') == 'claude-opus-5':
                raise RateLimitError(
                    '429 saturation: all candidate slots continuously '
                    'rate-limited for 121s', is_saturation=True,
                    status_code=429, reason='saturation:121s')
            return ({'role': 'assistant', 'content': 'recovered'},
                    'stop', {'prompt_tokens': 5, 'completion_tokens': 3})

        monkeypatch.setattr(fb_pkg, 'stream_llm_response', _fake_stream)
        monkeypatch.setattr(fb_pkg, '_get_fallback_model', lambda task: 'kimi-k3')
        monkeypatch.setattr(fb_pkg, '_flag_empty_stop_for_retry',
                            lambda *a, **kw: False)
        monkeypatch.setattr(fb_pkg, '_emit_round_usage', lambda *a, **kw: None)
        monkeypatch.setattr(fb_pkg, 'append_event',
                            lambda task, ev: events.append(ev))

        task = {'id': 't-sat-0001', 'convId': 'conv-sat', 'config': {},
                'content': '', 'thinking': '',
                'events': [], 'events_lock': threading.Lock()}
        res = _llm_call_with_fallback(
            task, {'model': 'claude-opus-5'}, 'claude-opus-5', 0, 512,
            False, None, [{'role': 'user', 'content': 'hi'}],
            'low', False, {}, [])
        assert res['model'] == 'kimi-k3'
        assert task['_fallback_model'] == 'kimi-k3'
        assert task['_fallback_from'] == 'claude-opus-5'
        assert any('回退' in (e.get('detail') or '') or 'fallback' in
                   (e.get('detail') or '').lower() for e in events), (
            f'no fallback phase event emitted: {events}')
