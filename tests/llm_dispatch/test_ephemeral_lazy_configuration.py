"""Request-owned routes do not initialize an unrelated configured authority."""
import pytest

from lib.llm_dispatch import ephemeral, factory
from lib.llm_dispatch.dispatcher import LLMDispatcher

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('configured_outcome', ['success', 'failure'])
def test_mint_is_independent_and_later_configured_rebuild_preserves_owner(
        monkeypatch, configured_outcome):
    dispatcher = LLMDispatcher()
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(ephemeral, '_handles', {})
    monkeypatch.setenv('TOFU_EPHEMERAL_PREFLIGHT', '0')
    entered = []

    def configure():
        entered.append(True)
        if configured_outcome == 'failure':
            raise RuntimeError('configured authority unavailable')

    monkeypatch.setattr(dispatcher, '_build_slots', configure)
    monkeypatch.setattr(dispatcher, '_load_benchmark_data', lambda: None)
    handle = ephemeral.mint_ephemeral_slot(
        base_url='http://127.0.0.1:1/v1', api_key='', model_id='owned-model')
    try:
        assert entered == [] and not dispatcher._initialized
        assert dispatcher.slots == [handle.slot]
        if configured_outcome == 'failure':
            with pytest.raises(RuntimeError, match='configured authority unavailable'):
                dispatcher.initialize()
        else:
            dispatcher.initialize()
        assert entered == [True]
        assert dispatcher.slots == [handle.slot]
        assert ephemeral.count_ephemeral_slots() == 1
    finally:
        ephemeral.dispose_ephemeral_slot(handle)
    assert dispatcher.slots == [] and ephemeral.count_ephemeral_slots() == 0
