"""Identity benchmark must reject unequal state or diagnostic work before timing."""
from types import SimpleNamespace

import pytest

from benchmarks.tool_call_identity_compare import assert_parity

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('drift', ['row', 'receipt', 'diagnostic'])
def test_identity_measure_refuses_changed_occurrence_evidence(drift):
    baseline = SimpleNamespace(_repair_current_tool_call_identity=lambda *args, **kwargs: None)
    candidate = SimpleNamespace()

    def corrupt(task, messages, calls, index, item, **kwargs):
        if drift == 'row':
            item[5]['toolCallId'] = 'wrong occurrence'
        elif drift == 'receipt':
            task['_tool_call_id_receipts']['unexpected'] = {'status': 'done'}
        else:
            candidate.logger.warning('unexpected diagnostic')

    candidate._repair_current_tool_call_identity = corrupt
    with pytest.raises(ValueError, match='Identity or diagnostic drift'):
        assert_parity(baseline, candidate, ('single_fresh', 1, 8))
