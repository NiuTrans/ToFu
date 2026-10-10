"""Project cursor ACK requires verified model consumption and current binding."""
from types import SimpleNamespace

import pytest

from lib.llm.stream_result import ProviderStreamResult
from lib.tasks_pkg.orchestrator import _llm_round_call as call

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('result_kind', ['complete', 'partial', 'missing', 'break'])
@pytest.mark.parametrize('binding_current', [True, False])
def test_project_page_ack_uses_verified_provider_return_and_revalidated_binding(
        monkeypatch, result_kind, binding_current):
    from lib.agent_core import store
    from lib.conversations.project_coordination import ProjectCoordinationRepository

    message = {'role': 'assistant', 'content': 'answer'}
    stream = ProviderStreamResult.from_legacy(
        message, 'premature_close' if result_kind == 'partial' else 'stop', None)
    result = {
        'assistant_msg': message, 'finish_reason': stream.finish_reason,
        'usage': None, 'model': 'model', 'preset': None, 'thinking_enabled': False,
        '_loop_action': 'break' if result_kind == 'break' else 'proceed',
        '_loop_exit_reason': 'failure',
    }
    if result_kind != 'missing':
        result['stream_result'] = stream
    monkeypatch.setattr(call, '_llm_call_with_fallback', lambda *a, **kw: result)
    monkeypatch.setattr(call, 'flush_deferred_peer_and_steer', lambda *a, **kw: None)
    monkeypatch.setattr(ProjectCoordinationRepository, 'read', lambda self, path: {
        'projectPath': path, 'availability': 'available',
        'conversationId': 'coordinator' if binding_current else 'replacement',
    })
    from lib.tasks_pkg.persistence_store import DefaultConversationStore
    acknowledgements = []
    monkeypatch.setattr(store, 'get_conversation_store', lambda: SimpleNamespace(
        confirm_project_context_consumption=DefaultConversationStore().confirm_project_context_consumption,
        confirm_project_context_delivery=lambda pending: acknowledgements.append(pending)))
    pending = {'projectPath': '/owned', 'conversationId': 'coordinator',
               'userId': 1, 'deliveryToken': 'page'}
    task = {'_userId': 1, 'convId': 'coordinator', '_projectNarrativeDelivery': pending}
    rs = SimpleNamespace(model='model', tool_call_happened=False, preset=None,
                         thinking_enabled=False, accumulated_usage={}, api_rounds=0,
                         last_usage=None)
    action = call.run_llm_call_with_fallback(
        task, rs, {}, [], [], SimpleNamespace(on_tool_call_ready=None),
        round_num=0, tid='context', max_tokens=100)
    if result_kind == 'complete':
        assert acknowledgements == ([pending] if binding_current else [])
        assert '_projectNarrativeDelivery' not in task
    else:
        assert acknowledgements == []
        assert task['_projectNarrativeDelivery'] == pending
    assert action == ('break' if result_kind == 'break' else 'proceed')
