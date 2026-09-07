"""Owner-routing contracts for background slide production."""

from __future__ import annotations

from contextlib import contextmanager

import pytest


pytestmark = pytest.mark.unit


def test_owner_chat_route_mints_pins_and_disposes(monkeypatch):
    import lib.model_routing as routing
    from lib.production.owner_routing import owner_chat_route

    observed = {}

    class FakeGroup:
        pin_id = 'owner-route-pin'

    class FakeRepository:
        pass

    def fake_mint(repository, boundary, capability, **kwargs):
        observed['repository'] = repository
        observed['owner'] = boundary.owner_user_id
        observed['capability'] = capability
        observed['mint_kwargs'] = kwargs
        return 'configured-model', FakeGroup()

    @contextmanager
    def fake_pin(pin_id):
        observed['entered_pin'] = pin_id
        yield

    monkeypatch.setattr(routing, 'ModelRoutingRepository', FakeRepository)
    monkeypatch.setattr(routing, 'mint_capability_slot_group', fake_mint)
    monkeypatch.setattr(
        routing, 'dispose_routed_slot_group',
        lambda group: observed.setdefault('disposed', group.pin_id))
    monkeypatch.setattr('lib.llm_dispatch.provider_pin.provider_pin', fake_pin)

    with owner_chat_route(
            41, tenant_id='tenant-a', prefer_model='wanted-model',
            owner_tag='slides:41') as route:
        assert route.pin_id == 'owner-route-pin'
        assert route.routed_model == 'configured-model'

    assert observed['owner'] == 41
    assert observed['capability'] == 'text'
    assert observed['mint_kwargs']['prefer_model'] == 'wanted-model'
    assert observed['mint_kwargs']['owner_tag'] == 'slides:41'
    assert observed['entered_pin'] == 'owner-route-pin'
    assert observed['disposed'] == 'owner-route-pin'


def test_slide_author_forwards_owner_and_enters_worker_pin(monkeypatch):
    from lib.slides.author import _llm

    observed = {}

    @contextmanager
    def fake_pin(pin_id):
        observed['pin'] = pin_id
        yield

    def fake_dispatch(messages, **kwargs):
        observed['messages'] = messages
        observed['kwargs'] = kwargs
        return 'ok', {}

    monkeypatch.setattr('lib.llm_dispatch.provider_pin.provider_pin', fake_pin)
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', fake_dispatch)

    result = _llm(
        [{'role': 'user', 'content': 'hello'}], max_tokens=32,
        owner_user_id=41, provider_pin_id='worker-pin')

    assert result == ('ok', {})
    assert observed['pin'] == 'worker-pin'
    assert observed['kwargs']['owner_user_id'] == 41


def test_build_deck_threads_owner_route_into_stage_context(tmp_path, monkeypatch):
    import lib.production.owner_routing as owner_routing
    import lib.slides.recipe as recipe
    from lib.production.owner_routing import OwnerChatRoute

    observed = {}

    @contextmanager
    def fake_owner_route(owner_user_id, **kwargs):
        observed['owner_user_id'] = owner_user_id
        observed['route_kwargs'] = kwargs
        yield OwnerChatRoute('job-pin', 'owner-default-model')

    def fake_run_stages(stages, ctx, **kwargs):
        observed['ctx'] = dict(ctx)
        return {
            'outline': {'title': 'Deck', 'scenario': 'general', 'pages': []},
            'design': {'theme_id': 'minimal-editorial'},
            'author': {'total': 0, 'authored': 0},
            'render': {'previews': []},
            'layout_qa': {},
            'visual_qa': {},
            'export': {'pptx_path': str(tmp_path / 'deck.pptx'), 'bytes': 1},
        }

    monkeypatch.setattr(owner_routing, 'owner_chat_route', fake_owner_route)
    monkeypatch.setattr(recipe, 'run_stages', fake_run_stages)

    result = recipe.build_deck_from_topic(
        '美团大模型发展', str(tmp_path / 'job'), max_pages=3,
        owner_user_id=41)

    assert result['pptx_path'].endswith('deck.pptx')
    assert observed['owner_user_id'] == 41
    assert observed['ctx']['owner_user_id'] == 41
    assert observed['ctx']['provider_pin_id'] == 'job-pin'
    assert observed['ctx']['qa_model'] == 'owner-default-model'


def test_slides_engine_passes_task_owner_to_recipe(tmp_path, monkeypatch):
    import lib.slides.engine as engine
    import lib.slides.recipe as recipe
    import lib.slides.runtime as runtime

    observed = {}

    def fake_build(topic, workdir, **kwargs):
        observed['topic'] = topic
        observed['workdir'] = workdir
        observed['kwargs'] = kwargs
        return {
            'pptx_path': str(tmp_path / 'deck.pptx'), 'pages': 1,
            'authored_pages': 1, 'bytes': 4096,
        }

    monkeypatch.setattr(recipe, 'build_deck_from_topic', fake_build)
    monkeypatch.setattr(engine, '_write_manifest', lambda *args: None)
    monkeypatch.setattr(engine, '_emit', lambda *args: None)
    monkeypatch.setattr(runtime._slides_runtime, 'mark_running', lambda *args: None)
    monkeypatch.setattr(runtime._slides_runtime, 'finish', lambda *args, **kwargs: None)

    engine.run_slides_task({
        'task_id': 'slides-owner-test', 'topic': '美团大模型发展',
        'workdir': str(tmp_path), 'user_id': 41, 'max_pages': 3,
        'size': (1280, 720), 'creative_mode': 'director',
    })

    assert observed['kwargs']['owner_user_id'] == 41
    assert observed['kwargs']['creative_mode'] == 'director'


def test_route_group_skips_one_uninitializable_candidate(monkeypatch):
    from types import SimpleNamespace

    from lib.llm_dispatch.slot import Slot
    from lib.model_routing import ModelRef, NativeModelSelection, OwnerBoundary
    import lib.model_routing.dispatch_adapter as adapter

    def candidate(number):
        return SimpleNamespace(
            model={'creator_id': 'creator', 'model_id': f'model-{number}'},
            provider={'provider_id': f'provider-{number}'},
            provider_id=f'provider-{number}',
            provider_access={},
            offering={
                'offering_id': f'offering-{number}',
                'pending_model_id': '', 'capabilities': ['text'],
                'actual_pricing': {},
            },
            deployment={
                'deployment_id': f'deployment-{number}',
                'wire_model_id': f'wire-{number}',
            },
            connection={
                'connection_id': f'connection-{number}',
                'base_url': f'https://provider-{number}.example/v1',
                'extra_headers': {}, 'protocol': 'openai', 'adapter': {},
            },
            credential={
                'credential_id': f'credential-{number}',
                'secret_reference': '', 'kind': 'api_key',
            },
            score=(number,), selection_reasons=(),
        )

    candidates = [candidate(1), candidate(2)]
    monkeypatch.setattr(adapter, 'compile_candidates',
                        lambda *args, **kwargs: candidates)
    monkeypatch.setattr(adapter, 'compile_model_fallback_candidates',
                        lambda *args, **kwargs: [])

    calls = []

    def fake_mint(**kwargs):
        calls.append(kwargs['wire_model_id'])
        if kwargs['wire_model_id'] == 'wire-1':
            raise ValueError('first endpoint is unreachable')
        return SimpleNamespace(slot=Slot(
            key_name='ephemeral-test', api_key='', model='wire-2',
            logical_model='model-2', capabilities={'text'},
            provider_id=kwargs['provider_pin_id']))

    monkeypatch.setattr(adapter, 'mint_ephemeral_slot', fake_mint)

    class Repository:
        def get(self, boundary):
            return SimpleNamespace(document={})

        def resolve_secret(self, boundary, reference):
            return ''

    group = adapter.mint_routed_slot_group(
        Repository(), OwnerBoundary.create(41),
        NativeModelSelection(ModelRef('creator', 'model-1'), None, ''))

    assert calls == ['wire-1', 'wire-2']
    assert len(group.handles) == 1
    assert group.candidates == [candidates[1]]
    assert group.primary.slot.route_deployment_id == 'deployment-2'
    assert group.primary.slot.route_snapshot['degradation_reasons'] == [
        '1 earlier route candidate(s) could not be initialized']
