"""Behavioural tests for the SYNC ``dispatch_stream`` (lib/llm_dispatch/_api_stream.py; facade lib/llm_dispatch/api.py).

The async sibling (``async_dispatch_stream``) already has a behavioural suite in
``test_async_dispatch_stream.py``; the sync path only had a signature test.  This
file pins the sync retry/exclusion state machine so the upcoming
``_StreamRetryState`` extraction (shared between the sync + async loops) is
covered on BOTH sides:

  - success returns (msg, finish, usage), records slot success, injects
    ``usage['_dispatch']`` metadata;
  - a 429 (RateLimitError, non-quota) is retried for FREE (does not count
    toward max_retries) then succeeds on the next slot;
  - a quota-exhausted 429 (is_quota=True) excludes the KEY and counts as a
    hard attempt;
  - a PermissionError excludes the (key, model) PAIR (not the whole model),
    then fails over;
  - an EndpointUnreachableError cools the slot + excludes the pair, then
    fails over to a live slot;
  - AbortedError propagates immediately (no retry) and releases the slot.

Run:  pytest tests/test_dispatch_stream.py -m unit
"""
from __future__ import annotations

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
    """Hands out a queued list of slots; None entries simulate 'no slot'.

    Exposes ``slots`` (the sync PermissionError handler inspects
    ``dispatcher.slots`` to decide whether to escalate a pair-exclusion to a
    key-exclusion) — default empty so that escalation does NOT fire and we
    test the common pair-exclusion path.
    """

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
            slot.record_request()  # mirror real pick_and_reserve(reserve=True)
        return slot

    def has_capable_slots(self, *a, **kw):
        return bool(self._slots)

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # 429 retries call time.sleep(0.3) — make them instant so tests are fast.
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)


@pytest.mark.unit
class TestDispatchStreamSuccess:
    def test_success_returns_tuple_and_records_slot(self, monkeypatch):
        from lib.llm_dispatch import api

        slot = _make_slot()
        disp = _FakeDispatcher([slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_stream(body, **kwargs):
            kwargs['on_content']('hello ')
            kwargs['on_content']('world')
            return 'hello world', 'stop', {'completion_tokens': 7}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        chunks = []
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}],
            on_content=chunks.append, log_prefix='[t]')

        assert msg == 'hello world'
        assert finish == 'stop'
        assert chunks == ['hello ', 'world']
        assert slot.inflight == 0
        assert slot.last_success_time > 0
        assert slot.consecutive_errors == 0
        assert usage['_dispatch']['model'] == 'qwen-plus'
        assert usage['_dispatch']['key'] == 'k0'
        assert usage['_dispatch']['429_retries'] == 0
        assert usage['_dispatch']['queue_wait_ms'] >= 0
        assert usage['_dispatch']['queue_wait_measurement'] == \
            'dispatcher_backpressure_only'

    def test_distinct_large_conversations_never_cross_gate(
            self, monkeypatch):
        """Retired BIG_PREFIX knobs cannot reintroduce cross-conv waiting.

        Under the former default, the third distinct 200k-token prefix on the
        same selected key waited the residency budget even though selection had
        already finished and the request could not reroute.
        """
        from lib.llm_dispatch import api
        from lib.llm_dispatch.conv_affinity import conv_affinity
        from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY

        attempts = [_make_slot(key='k0') for _ in range(3)]
        all_slots = [_make_slot(key='k0'), _make_slot(key='k1')]
        dispatcher = _FakeDispatcher(attempts, all_slots=all_slots)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
        monkeypatch.setenv('TOFU_CACHE_SETTLE', '0')
        monkeypatch.setenv('TOFU_CONV_STICKY_HOLD', '0')
        monkeypatch.setenv('TOFU_BIG_PREFIX_GATE', '1')
        monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '1')
        monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_MAX', '2')
        monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_WAIT_MS', '400')

        import lib.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, 'stream_chat',
            lambda *_args, **_kwargs: (
                'ok', 'stop', {'completion_tokens': 1}),
        )
        body = {
            'model': 'qwen-plus',
            'messages': [{'role': 'user', 'content': 'large prompt'}],
            'max_tokens': 128,
            'stream': True,
            ADMITTED_INPUT_TOKENS_KEY: 200_000,
        }

        started = time.perf_counter()
        for conv_id in ('conv-a', 'conv-b', 'conv-c'):
            with conv_affinity(conv_id):
                assert api.dispatch_stream(body)[0] == 'ok'
        elapsed = time.perf_counter() - started

        assert elapsed < 0.2, (
            'distinct conversations regained a retired cross-prefix wait: '
            f'{elapsed:.3f}s')

    def test_semantic_retry_avoids_failed_pair_on_first_pick(self, monkeypatch):
        """A fresh dispatch must apply the prior attempt's rotate hint now.

        The analyser emits ``avoid_pairs`` only after an unusable stream.  If
        the new dispatch hides that set until it has its own failure, the first
        pick repeats the poisoned slot and the advertised rotation is fictive.
        """
        from lib.llm_dispatch import api

        failed = _make_slot(model='kimi-k3', key='k0')
        alternate = _make_slot(model='kimi-k3', key='k1')

        class _AvoidAwareDispatcher:
            slots = [failed, alternate]

            def __init__(self):
                self.first_exclusions = None

            def pick_and_reserve(self, **kwargs):
                excluded = set(kwargs.get('exclude_pairs') or set())
                if self.first_exclusions is None:
                    self.first_exclusions = excluded
                for slot in self.slots:
                    if (slot.key_name, slot.model) not in excluded:
                        slot.record_request()
                        return slot
                return None

            def has_capable_slots(self, *args, **kwargs):
                return True

            def summarize_slots(self, *args, **kwargs):
                return 'avoid-aware'

        dispatcher = _AvoidAwareDispatcher()
        monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)

        import lib.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, 'stream_chat',
            lambda *_args, **_kwargs: (
                'recovered', 'stop', {'completion_tokens': 1}),
        )

        _msg, _finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}],
            prefer_model='kimi-k3', strict_model=True,
            avoid_pairs={('k0', 'kimi-k3')}, log_prefix='[rotate]',
        )

        assert dispatcher.first_exclusions == {('k0', 'kimi-k3')}
        assert usage['_dispatch']['key'] == 'k1'


@pytest.mark.unit
class TestFirstOutputCallbacks:
    def test_thinking_first_stamps_once_and_forwards_every_channel(
            self, monkeypatch):
        from lib.llm_dispatch import api

        monkeypatch.setattr(api.time, 'time', lambda: 10.025)
        observed = {'thinking': [], 'content': [], 'tool': []}
        ttft, thinking, content, tool = api._first_output_callbacks(
            10.0,
            observed['thinking'].append,
            observed['content'].append,
            observed['tool'].append,
        )

        thinking('reasoning')
        content('answer')
        tool({'id': 'call-1'})

        assert ttft[0] == pytest.approx(25.0)
        assert observed == {
            'thinking': ['reasoning'],
            'content': ['answer'],
            'tool': [{'id': 'call-1'}],
        }

    def test_tool_only_output_stamps_without_consumer(self, monkeypatch):
        from lib.llm_dispatch import api

        monkeypatch.setattr(api.time, 'time', lambda: 4.75)
        ttft, _thinking, _content, tool = api._first_output_callbacks(
            4.0, None, None, None)

        tool({'id': 'call-only'})

        assert ttft[0] == pytest.approx(750.0)


@pytest.mark.unit
class TestDispatchStreamRetry:
    def test_429_then_success_is_free(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        # The non-quota 429 path probes is_key_enabled — keep the key enabled
        # so the routine-backpressure branch (free retry) is exercised.
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError('429 too many requests')
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]', max_retries=1)

        # max_retries=1 yet a 429 still succeeded → the 429 retry was FREE
        # (didn't count toward hard_attempts).
        assert msg == 'ok'
        assert calls['n'] == 2
        assert usage['_dispatch']['429_retries'] >= 1
        assert usage['_dispatch']['queue_wait_ms'] >= 0
        assert slot1.total_errors >= 1
        assert slot2.last_success_time > 0

    def test_quota_429_excludes_key_and_counts_hard(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                e = RateLimitError('quota exhausted')
                e.is_quota = True
                raise e
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        retries = []
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            on_retry=lambda **kw: retries.append(kw))

        assert msg == 'ok'
        assert calls['n'] == 2
        # Quota exhaustion is a HARD attempt with a 'Key balance exhausted'
        # retry notice (distinct from the free 429 path).
        assert any('balance' in (r.get('reason') or '').lower() for r in retries)

    def test_credential_delivery_anomaly_is_gateway_bounded(self, monkeypatch):
        """Contradictory missing-key 401s rotate briefly, never enter the
        durable permission exclusion path, and cannot wait forever."""
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot = _make_slot(model='kimi-k3', key='kA')
        dispatcher = _FakeDispatcher([slot, slot, slot, slot], all_slots=[slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
        monkeypatch.setattr(
            'lib.key_stats.record_gateway_error', lambda *a, **kw: None)

        calls = {'n': 0}

        def _missing_key(_body, **_kwargs):
            calls['n'] += 1
            raise RateLimitError(
                'API HTTP 401: missing api key',
                is_gateway=True,
                is_credential_delivery_anomaly=True,
                reason='credential_delivery_anomaly',
                status_code=401,
            )

        monkeypatch.setattr('lib.llm.stream_chat', _missing_key)

        with pytest.raises(RateLimitError) as raised:
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='kimi-k3', strict_model=True,
                log_prefix='[auth-delivery]',
            )

        assert calls['n'] == 4
        assert dispatcher.picks == 4
        assert raised.value.is_gateway is True
        assert raised.value.credential_delivery_anomaly_attempts == 4
        assert raised.value.credential_delivery_anomaly_limit == 4
        assert slot.gateway_errors == 4
        assert slot.total_errors == 0
        assert slot.inflight == 0

    def test_nonstream_credential_delivery_anomaly_uses_same_cap(
            self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot = _make_slot(model='kimi-k3', key='kA')
        dispatcher = _FakeDispatcher([slot, slot, slot, slot], all_slots=[slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
        monkeypatch.setattr(
            'lib.key_stats.record_gateway_error', lambda *a, **kw: None)

        def _missing_key(messages, **_kwargs):
            raise RateLimitError(
                'API HTTP 401: missing api key',
                is_gateway=True,
                is_credential_delivery_anomaly=True,
                reason='credential_delivery_anomaly',
                status_code=401,
            )

        monkeypatch.setattr('lib.llm.chat', _missing_key)

        with pytest.raises(RateLimitError) as raised:
            api.dispatch_chat(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='kimi-k3', strict_model=True,
                log_prefix='[auth-delivery-chat]',
            )

        assert dispatcher.picks == 4
        assert raised.value.credential_delivery_anomaly_attempts == 4
        assert slot.gateway_errors == 4
        assert slot.total_errors == 0
        assert slot.inflight == 0


@pytest.mark.unit
class TestDispatchStreamPermissionPairExclusion:
    def test_permission_excludes_pair_then_fails_over(self, monkeypatch):
        """401/403 on (key, model) must exclude only that PAIR, letting another
        key serving the same model still be tried (the pair-exclusion fix)."""
        from lib.llm_dispatch import api
        from lib.llm import PermissionError_

        # Two keys, SAME model — pair-exclusion must route to the 2nd key.
        denied = _make_slot(model='gpt-4o', key='kA')
        allowed = _make_slot(model='gpt-4o', key='kB')
        disp = _FakeDispatcher([denied, allowed])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise PermissionError_('403 forbidden')
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert calls['n'] == 2
        assert denied.total_errors >= 1
        assert allowed.last_success_time > 0


@pytest.mark.unit
class TestDispatchStreamUnreachableFailover:
    def test_unreachable_cools_slot_and_fails_over(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import EndpointUnreachableError

        dead = _make_slot(key='dead')
        live = _make_slot(key='live')
        disp = _FakeDispatcher([dead, live])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise EndpointUnreachableError(
                    'endpoint unreachable: connect timeout',
                    base_url='http://10.0.0.1:8080/v1')
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert calls['n'] == 2
        assert dead.cooldown_until > time.time()
        assert dead.total_errors >= 1
        assert live.last_success_time > 0


@pytest.mark.unit
class TestDispatchStreamAllUnreachable:
    def test_all_unreachable_raises_friendly_error(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import EndpointUnreachableError

        dead = _make_slot(model='glm5.1-FP8', key='dead')
        disp = _FakeDispatcher([dead])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_stream(body, **kwargs):
            raise EndpointUnreachableError(
                'endpoint unreachable: connect timeout',
                base_url='http://10.0.0.1:8080/v1')

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        with pytest.raises(EndpointUnreachableError) as ei:
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='glm5.1-FP8', strict_model=True,
                max_retries=3, log_prefix='[t]')
        msg = str(ei.value)
        assert 'unreachable' in msg.lower()
        assert 'glm5.1-FP8' in msg


@pytest.mark.unit
class TestSettleStreamResultHelper:
    """Pin the one-shot settlement shared by sync and async dispatch loops."""

    def _state(self, hard=0, r429=0):
        from lib.llm_dispatch.api import _StreamRetryState
        st = _StreamRetryState()
        st.hard_attempts = hard
        st._429_count = r429
        return st

    def test_stamps_dispatch_metadata_and_records_success(self):
        from lib.llm_dispatch.api import _settle_stream_result
        slot = _make_slot(model='gpt-4o', key='kZ')
        usage = {'completion_tokens': 11}
        st = self._state(hard=1, r429=2)
        _settle_stream_result(slot, usage, latency=123.4, ttft=45.6,
                              state=st, cache_conv_id='', tag='[t]')
        assert usage['_dispatch']['model'] == 'gpt-4o'
        assert usage['_dispatch']['key'] == 'kZ'
        assert usage['_dispatch']['latency_ms'] == 123
        assert usage['_dispatch']['ttft_ms'] == 45.6
        assert usage['_dispatch']['first_content_at_unix_ns'] >= \
            usage['_dispatch']['stream_started_at_unix_ns']
        assert usage['_dispatch']['stream_completed_at_unix_ns'] >= \
            usage['_dispatch']['first_content_at_unix_ns']
        assert usage['_dispatch']['attempt'] == 2      # hard_attempts + 1
        assert usage['_dispatch']['429_retries'] == 2
        assert slot.last_success_time > 0
        assert slot.consecutive_errors == 0

    def test_non_dict_usage_is_tolerated(self):
        from lib.llm_dispatch.api import _settle_stream_result
        slot = _make_slot()
        st = self._state()
        # Some providers return None usage — must not raise, must still record.
        _settle_stream_result(slot, None, latency=10.0, ttft=None,
                              state=st, cache_conv_id='', tag='[t]')
        assert slot.last_success_time > 0

    def test_output_tokens_from_output_tokens_key(self):
        from lib.llm_dispatch.api import _settle_stream_result
        slot = _make_slot()
        # Anthropic-shape usage uses output_tokens, not completion_tokens.
        usage = {'output_tokens': 9}
        _settle_stream_result(slot, usage, latency=5.0, ttft=None,
                              state=self._state(), cache_conv_id='', tag='[t]')
        assert usage['_dispatch']['model'] == slot.model
        assert usage['_dispatch']['ttft_ms'] is None
        assert usage['_dispatch']['first_content_at_unix_ns'] is None

    def test_codex_success_arms_unmetered_write_visibility(self, monkeypatch):
        from lib.llm_dispatch import cache_settle
        from lib.llm_dispatch.api import _settle_stream_result

        observed = []
        recorded = []
        monkeypatch.setattr(
            cache_settle, 'observe_codex_cache',
            lambda conv_id, usage: observed.append((conv_id, usage)))
        monkeypatch.setattr(
            cache_settle, 'record_stream_end',
            lambda conv_id, **kwargs: recorded.append((conv_id, kwargs)))

        slot = _make_slot(model='gpt-5.6-luna', key='codex-key')
        slot.oauth = 'codex'
        usage = {
            'prompt_tokens': 5066,
            'prompt_tokens_details': {'cached_tokens': 0},
        }
        _settle_stream_result(
            slot, usage, latency=5.0, ttft=None,
            state=self._state(), cache_conv_id='conv-codex', tag='[t]')

        assert observed == [('conv-codex', usage)]
        assert recorded[0][0] == 'conv-codex'
        assert recorded[0][1]['cache_profile'] == 'codex'
        assert recorded[0][1]['pending_write'] is True

    def test_generic_warm_small_tail_disarms_visibility_hold(self, monkeypatch):
        from lib.llm_dispatch import cache_settle
        from lib.llm_dispatch.api import _settle_stream_result

        recorded = []
        monkeypatch.setattr(
            cache_settle, 'record_stream_end',
            lambda conv_id, **kwargs: recorded.append((conv_id, kwargs)))

        usage = {
            'prompt_tokens': 100_000,
            'prompt_tokens_details': {'cached_tokens': 98_000},
        }
        _settle_stream_result(
            _make_slot(model='kimi-k3', key='sankuai-key'), usage,
            latency=5.0, ttft=None, state=self._state(),
            cache_conv_id='conv-kimi', tag='[t]')

        assert recorded[0][0] == 'conv-kimi'
        assert recorded[0][1]['cache_profile'] == ''
        assert recorded[0][1]['pending_write'] is False

    @pytest.mark.parametrize('stream_state', [
        'semantic_progress_timeout',
        'malformed_stream',
        'empty_response',
        'tool_payload_missing',
    ])
    def test_invalid_stream_settles_slot_once_without_provisional_success(
            self, stream_state):
        from types import SimpleNamespace

        from lib.llm.stream_result import ProviderStreamState
        from lib.llm_dispatch.api import _settle_stream_result

        slot = _make_slot()
        slot.inflight = 1
        usage = {
            # Deliberately stale compatibility data: the typed result must win.
            '_stream_state': 'provider_finished',
            'stream_elapsed_ms': 301_234,
            '_chunks_received': 974,
        }
        typed_result = SimpleNamespace(
            state=ProviderStreamState(stream_state))

        _settle_stream_result(
            slot, usage, latency=301_234, ttft=10.0,
            state=self._state(), cache_conv_id='', tag='[t]',
            stream_result=typed_result,
        )

        assert slot.inflight == 0
        assert slot.last_success_time == 0
        assert slot.consecutive_errors == 1
        assert stream_state in slot.last_error_msg

    def test_client_abort_releases_reservation_without_health_mutation(self):
        from types import SimpleNamespace

        from lib.llm.stream_result import ProviderStreamState
        from lib.llm_dispatch.api import _settle_stream_result

        slot = _make_slot()
        slot.inflight = 1
        slot.consecutive_errors = 2
        _settle_stream_result(
            slot, {'_stream_state': 'provider_finished'},
            latency=5.0, ttft=None, state=self._state(),
            cache_conv_id='', tag='[t]',
            stream_result=SimpleNamespace(
                state=ProviderStreamState.CLIENT_ABORTED),
        )

        assert slot.inflight == 0
        assert slot.consecutive_errors == 2
        assert slot.last_success_time == 0


def test_cooldown_polling_is_not_a_retry_attempt():
    from lib.llm_dispatch.api import _StreamRetryState

    state = _StreamRetryState()
    state.note_cooldown_cycle()
    state.note_cooldown_cycle()

    assert state.capacity_wait_cycles == 2
    assert state.total_attempts == 0
    assert state._429_count == 0
    assert state.wait_status().kind == 'waiting_slot'

    state.note_free_429()
    assert state.capacity_wait_cycles == 3
    assert state.total_attempts == 1
    assert state._429_count == 1


@pytest.mark.unit
class TestDispatchStreamAbort:
    def test_abort_propagates_and_releases_slot(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm import AbortedError

        slot = _make_slot()
        disp = _FakeDispatcher([slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_stream(body, **kwargs):
            raise AbortedError('user aborted')

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        with pytest.raises(AbortedError):
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
        assert slot.inflight == 0


@pytest.mark.unit
class TestDispatchStreamQuotaScope:
    """End-to-end at the dispatch seam: RateLimitError.status_code decides
    the key_stats stop granularity (owner 2026-07-29: HTTP 402 = the
    gateway ACCOUNT's credit pool is dead → key-wide stop; a 429-quota
    body stays vendor-ambiguous → per-model stop)."""

    @pytest.fixture
    def isolated_key_stats(self, monkeypatch, tmp_path):
        import lib.key_stats as ks
        snapshot = {k: ks._cache[k] for k in
                    ('day', 'stats', 'overrides', 'loaded')}
        monkeypatch.setattr(ks, '_STATS_PATH',
                            str(tmp_path / 'key_stats.json'))
        monkeypatch.setattr(ks, '_list_siblings',
                            lambda pid: ['default::k1', 'default::k2'])
        ks._cache.update(day='', stats={}, overrides={}, loaded=False)
        yield ks
        ks._cache.update(snapshot)

    def _run_quota_dispatch(self, monkeypatch, status_code):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError('quota exhausted',
                                     is_quota=True,
                                     status_code=status_code)
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
        assert msg == 'ok' and calls['n'] == 2

    def test_402_quota_stops_entire_key(self, monkeypatch,
                                        isolated_key_stats):
        ks = isolated_key_stats
        self._run_quota_dispatch(monkeypatch, status_code=402)
        row = ks.get_today_stats('default', 'k1')
        assert row['exhausted'] is True, (
            'a 402 quota error must flip the KEY-WIDE exhausted flag — '
            'the account credit pool is dead for EVERY model on the key')
        assert row['exhausted_models'] == {}
        assert ks.is_key_enabled('default', 'k1') is False
        assert ks.is_key_enabled('default', 'k1', model='qwen-plus') is False

    def test_429_quota_stops_only_the_model(self, monkeypatch,
                                            isolated_key_stats):
        ks = isolated_key_stats
        self._run_quota_dispatch(monkeypatch, status_code=429)
        row = ks.get_today_stats('default', 'k1')
        assert row['exhausted'] is False, (
            'a 429-quota body is vendor-ambiguous and must NOT key-wide '
            'stop the key (2026-07-28 aggregating-gateway contract)')
        assert set(row['exhausted_models']) == {'qwen-plus'}
        assert ks.is_key_enabled('default', 'k1', model='other-model') is True


@pytest.mark.unit
class TestQuotaScopeCallSites:
    """Every RateLimitError handler in the dispatch layer must wire the
    402-vs-429 distinction into record_error — a call site that forgets
    `is_account_quota=` silently degrades account-level 402s back to
    per-model convergence (the 2026-07-29 key_2 incident shape)."""

    def test_every_quota_record_error_passes_account_scope(self):
        from tests._source_scan import strip_comments

        import glob

        live = ''
        for path in sorted(glob.glob('lib/llm_dispatch/_api_*.py')):
            with open(path, encoding='utf-8') as f:
                live += strip_comments(f.read(), lang='python') + '\n'

        calls = []
        idx = 0
        while True:
            j = live.find('record_error(', idx)
            if j < 0:
                break
            depth = 0
            k = j + len('record_error')
            for k in range(k, len(live)):
                ch = live[k]
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        break
            calls.append(live[j:k + 1])
            idx = k + 1

        quota_sites = [c for c in calls if 'is_quota_exhausted=' in c]
        assert len(quota_sites) >= 3, (
            f'expected >=3 quota record_error call sites in the dispatch '
            f'layer (_api_chat.py/_api_stream.py), found '
            f'{len(quota_sites)} — the scan must stay non-vacuous')
        for c in quota_sites:
            assert 'is_account_quota=' in c, (
                'a quota record_error call site does not pass '
                'is_account_quota= — the 402 key-wide rule is unwired there')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
if __name__ == '__main__':
    pytest.main([__file__, '-v'])
