"""Route-missing HTTP 400 = model-level dispatch exclusion (mtgrjqtuhzi4i9).

The sankuai AIGC gateway answers an unserved model with HTTP 400
"不支持的模型类型(model=…)". Before this fix the dispatch layer filed it as
a generic deterministic 400: pair-exclusion burnt every remaining key of the
dead model, and — arriving LAST — it masked the preferred model's actionable
rejection (kimi-k3's invalid-tool-schema 400), which is what the user
actually saw bubble up as "[LLM error at round 3]".

The classifier now raises ``ModelRouteMissingError`` (a ``BadRequestError``
subclass, so first-400-wins and turn-level fallback keep working), and the
dispatch handlers exclude the whole MODEL for the rest of the dispatch and
process-suppress the provider/wire-model route until dispatcher rebuild.
Catalog or settings refresh rebuilds that pool; the settings cell probe shares
the same marker table.
"""

import pytest

import lib.llm_dispatch.api as api
from lib.llm_errors import (
    ROUTE_MISSING_MARKERS,
    BadRequestError,
    ModelRouteMissingError,
    PermissionError_,
    _classify_http_error,
)

pytestmark = pytest.mark.unit


# ── classifier ───────────────────────────────────────────────────────

class TestRouteMissingClassifier:

    def test_aigc_chinese_route_missing_raises_typed_error(self):
        body = ('{"status":400,"message":"不支持的模型类型(model=gpt-5.4-nano)",'
                '"data":null,"ext":{"error":{"source":"AIGC","service":"aigc",'
                '"stage":"validation"}}}')
        with pytest.raises(ModelRouteMissingError) as exc:
            _classify_http_error(400, body, 'gpt-5.4-nano', '[T]')
        assert exc.value.model == 'gpt-5.4-nano'
        # First-400-wins + turn-level fallback key off BadRequestError.
        assert isinstance(exc.value, BadRequestError)

    @pytest.mark.parametrize('marker', ROUTE_MISSING_MARKERS)
    def test_every_marker_matches(self, marker):
        with pytest.raises(ModelRouteMissingError):
            _classify_http_error(400, '{"message":"%s"}' % marker, 'm', '[T]')

    def test_schema_conflict_body_stays_plain_bad_request(self):
        body = ('{"error":{"message":"Invalid request: tools.function.parameters '
                'is not a valid moonshot flavored json schema, details: <At path '
                "'properties.jobs.items': conflicting keywords found in anyOf "
                'with parent: keywords (properties) are defined on the parent '
                'schema and inside anyOf>"}}')
        with pytest.raises(BadRequestError) as exc:
            _classify_http_error(400, body, 'kimi-k3', '[T]')
        assert not isinstance(exc.value, ModelRouteMissingError)

    def test_probe_and_dispatch_share_one_marker_authority(self):
        from lib import provider_probe
        assert provider_probe._ROUTE_MISSING_MARKERS is ROUTE_MISSING_MARKERS


class TestRouteMissingRuntimeSuppression:

    def test_unserved_wire_id_is_disabled_until_dispatcher_rebuild(self):
        from lib.llm_dispatch.dispatcher import LLMDispatcher
        from lib.llm_dispatch.slot import Slot

        first = Slot(
            key_name='k0', api_key='fake-k0', model='moonshotai/kimi-k3',
            provider_id='sankuai', capabilities={'text'})
        second = Slot(
            key_name='k1', api_key='fake-k1', model='moonshotai/kimi-k3',
            provider_id='sankuai', capabilities={'text'})
        dispatcher = LLMDispatcher()
        dispatcher.slots = [first, second]
        dispatcher._initialized = True

        changed = dispatcher.mark_model_route_missing(
            provider_id='sankuai', model='moonshotai/kimi-k3',
            error='不支持的模型类型')

        assert changed == 2
        assert not first.is_available and not second.is_available
        assert not dispatcher.has_capable_slots('text')


# ── dispatch harness ─────────────────────────────────────────────────

class _FakeSlot:
    def __init__(self, key_name, model):
        self.key_name = key_name
        self.model = model
        self.api_key = 'fake-key'
        self.base_url = ''
        self.extra_headers = None
        self.oauth = ''
        self.protocol = 'openai'
        self.provider_id = 'fake'
        self.thinking_format = ''
        self.adapter = None
        self.stream_only = False
        self.capabilities = {'text'}
        self.consecutive_errors = 0
        self.cooldown_until = 0
        self.cooldown_reason = ''
        self.released = 0

    def release(self):
        self.released += 1

    def record_error(self, **kw):
        pass

    def record_success(self, latency, **kw):
        pass


class _RoutingDispatcher:
    """Pick honoring the exclusion kwargs the dispatch loops pass."""

    def __init__(self, slots):
        self.slots = list(slots)
        self.attempts = []

    def _admissible(self, exclude_models, exclude_keys, exclude_pairs,
                    prefer_model):
        ex_models = set(exclude_models or ())
        ex_keys = set(exclude_keys or ())
        ex_pairs = set(exclude_pairs or ())
        candidates = [s for s in self.slots
                      if s.model not in ex_models
                      and s.key_name not in ex_keys
                      and (s.key_name, s.model) not in ex_pairs]
        if prefer_model:
            preferred = [s for s in candidates if s.model == prefer_model]
            if preferred:
                candidates = preferred
        return candidates

    def pick_and_reserve(self, *, capability=None, prefer_model=None,
                         exclude_models=None, exclude_keys=None,
                         exclude_pairs=None, strict_model=False, **kw):
        candidates = self._admissible(
            exclude_models, exclude_keys, exclude_pairs,
            prefer_model if not strict_model else prefer_model)
        if not candidates:
            return None
        slot = candidates[0]
        self.attempts.append((slot.key_name, slot.model))
        return slot

    def has_capable_slots(self, *a, **kw):
        # Unit-harness under-fidelity: answering False ends the call at the
        # first None pick instead of emulating the 60s re-admission cycle.
        return False

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'

    def sticky_cooldown_remaining_s(self, *a, **kw):
        return None

    def note_shared_contention(self, slot):
        pass


# ── dispatch_chat: model exclusion + first-400-wins end to end ───────

class TestChatRouteMissingExclusion:

    def test_dead_model_keys_are_skipped_and_first_400_wins(self, monkeypatch):
        kimi_schema = BadRequestError(
            'invalid tool schema: conflicting keywords in anyOf with parent')
        nano_route = ModelRouteMissingError(
            '不支持的模型类型(model=gpt-5.4-nano)', 'gpt-5.4-nano')
        slots = [_FakeSlot('k1', 'kimi-k3'), _FakeSlot('k0', 'kimi-k3'),
                 _FakeSlot('k0', 'gpt-5.4-nano'),
                 _FakeSlot('k1', 'gpt-5.4-nano')]
        dispatcher = _RoutingDispatcher(slots)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)

        def _chat(*args, **kw):
            model = dispatcher.attempts[-1][1]
            raise kimi_schema if model == 'kimi-k3' else nano_route

        monkeypatch.setattr('lib.llm.chat', _chat)
        with pytest.raises(BadRequestError) as exc:
            api.dispatch_chat(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='kimi-k3', max_retries=6, log_prefix='[T]')
        # The actionable rejection wins over the last-ditch fallback's 400.
        assert exc.value is kimi_schema
        # k1:gpt-5.4-nano is NEVER attempted: the first route-missing 400
        # excluded the whole model (pre-fix: pair-exclusion would try it).
        assert dispatcher.attempts == [
            ('k1', 'kimi-k3'), ('k0', 'kimi-k3'), ('k0', 'gpt-5.4-nano')]
        assert slots[3].released == 0
        assert all(s.released == 1 for s in slots[:3])


# ── dispatch_stream: same exclusion on the streaming lane ────────────

class TestStreamRouteMissingExclusion:

    def test_route_alias_400_does_not_mask_valid_route_403(
            self, monkeypatch):
        permission = PermissionError_(
            'model entitlement denied', status_code=403)
        stale_alias = ModelRouteMissingError(
            '不支持的模型类型(model=moonshotai/kimi-k3)',
            'moonshotai/kimi-k3')
        slots = [_FakeSlot('k0', 'kimi-k3'),
                 _FakeSlot('k0', 'moonshotai/kimi-k3')]
        dispatcher = _RoutingDispatcher(slots)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)

        def _stream(*args, **kw):
            model = dispatcher.attempts[-1][1]
            raise permission if model == 'kimi-k3' else stale_alias

        monkeypatch.setattr('lib.llm.stream_chat', _stream)
        with pytest.raises(PermissionError_) as exc:
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='kimi-k3', max_retries=3, log_prefix='[T]')

        assert exc.value is permission
        assert exc.value.status_code == 403
        assert dispatcher.attempts == [
            ('k0', 'kimi-k3'), ('k0', 'moonshotai/kimi-k3')]

    def test_route_missing_model_is_skipped_and_healthy_model_serves(
            self, monkeypatch):
        nano_route = ModelRouteMissingError(
            '不支持的模型类型(model=dead-m)', 'dead-m')
        slots = [_FakeSlot('k1', 'dead-m'), _FakeSlot('k0', 'dead-m'),
                 _FakeSlot('k1', 'good-m')]
        dispatcher = _RoutingDispatcher(slots)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)

        def _stream(*args, **kw):
            model = dispatcher.attempts[-1][1]
            if model == 'dead-m':
                raise nano_route
            return ('ok-text', 'stop',
                    {'prompt_tokens': 3, 'completion_tokens': 2})

        monkeypatch.setattr('lib.llm.stream_chat', _stream)
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}],
            prefer_model='dead-m', max_retries=6, log_prefix='[T]')
        assert (msg, finish) == ('ok-text', 'stop')
        # k0:dead-m never tried — one route-missing 400 excluded the model.
        assert dispatcher.attempts == [('k1', 'dead-m'), ('k1', 'good-m')]
