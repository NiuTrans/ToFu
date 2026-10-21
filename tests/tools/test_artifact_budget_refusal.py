"""A whole-batch budget refusal remains a result, including media-capable reads."""
import json
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tools.result_value import capture_result

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('operation,batch_field', [
    ('read_tool_artifact', 'reads'), ('search_tool_artifact', 'searches')])
def test_whole_batch_metadata_refusal_does_not_require_a_success_item_list(
        operation, batch_field, monkeypatch):
    monkeypatch.setattr(handler, '_RESPONSE_TOKEN_BUDGET', 256)
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(handler, 'task_user_id', lambda task: 17)
    queries = []

    def missing(**arguments):
        queries.append(arguments['artifact_ref'])
        return None

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository',
        lambda: SimpleNamespace(read_range=missing, search=missing))
    settled = []
    monkeypatch.setattr(handler, '_finalize_tool_round',
        lambda *args, **kwargs: settled.append(kwargs['status']))
    calls = [{'artifact_ref': 'tool-result:' + key * 64,
              **({'query': 'evidence'} if batch_field == 'searches' else {})}
             for key in ('a', 'b')]
    _, content, _ = handler._handle_tool_result_artifact(
        {'model': ''}, {}, operation, 'batch', {batch_field: calls}, 1, {}, {}, '', False)
    result = json.loads(capture_result(content).text())
    assert result['status'] == 'error'
    assert result['error']['code'] == 'response_metadata_too_large'
    assert result['error']['retryable'] is False and result['error']['nextAction']
    assert settled == ['error']
    assert sorted(queries) == sorted(item['artifact_ref'] for item in calls)
