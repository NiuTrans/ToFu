"""Owner-route vision capability projection."""

from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


def test_vision_probe_uses_pinned_group_when_logical_models_collide(
        monkeypatch):
    """A request group cannot inherit another credential's vision flag."""
    from lib.llm_dispatch.provider_pin import provider_pin
    from lib.model_info import model_supports_vision

    dispatcher = SimpleNamespace(slots=[
        SimpleNamespace(
            provider_id='operator-group',
            model='operator-wire',
            logical_model='shared-model',
            capabilities={'text', 'vision'},
        ),
        SimpleNamespace(
            provider_id='owner-group',
            model='owner-wire',
            logical_model='shared-model',
            capabilities={'text'},
        ),
    ])
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: dispatcher)

    assert model_supports_vision('shared-model') is True
    with provider_pin('owner-group'):
        assert model_supports_vision('shared-model') is False

