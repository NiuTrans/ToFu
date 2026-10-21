"""An owned internal envelope has one model projection, including legacy turns."""
import json

import pytest

from lib.tools.result_envelope import tool_result_error, typed_tool_error
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for, dispatch

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('owned_failure', [False, True])
def test_internal_error_plumbing_is_hidden_but_source_documents_are_preserved(
        mode, owned_failure, observations):
    failure = typed_tool_error('source_refused', retryable=False,
        message='The source refused this request.', next_action='Choose another authorized source.').to_envelope_text()

    def handler(task, tc, name, call_id, args, number, row, *rest):
        return call_id, failure if owned_failure else str(failure), False

    row, messages, captured = dispatch(task_for(handler, mode))
    text = messages[0]['content']
    if owned_failure:
        assert captured[0].execution_status == row['status'] == 'error'
        assert 'contractVersion' not in text and 'visibleBytes' not in text and 'freshness' not in text
        error = tool_result_error(text)
        assert error and error.code == 'source_refused' and error.retryable is False
        assert error.next_action == 'Choose another authorized source.'
        assert row['toolResultEvidence']['projectionKind'] == 'error'
        assert 'compactionLayer' not in row, 'hiding internal fields withholds no source evidence'
    else:
        assert captured[0].execution_status == 'ok' and row['status'] == 'done'
        assert json.loads(text) == json.loads(str(failure))
    assert captured[0].text() == str(failure)
