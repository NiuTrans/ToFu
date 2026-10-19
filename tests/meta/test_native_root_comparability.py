"""A root timing cohort cannot compare tasks with different provider work."""
import copy
import pytest
from benchmarks.native_agent_root import comparison_receipt

pytestmark = pytest.mark.unit


def pair():
    row = dict(arm='python', status='done', content='answer',
        usage={'prompt_tokens':3,'completion_tokens':2,'total_tokens':5},
        requests=[{'model':'fixture','messages':[{'role':'user','content':'question'}],
                   'tools':[{'type':'function','function':{'name':'read','parameters':{}}}]}])
    other = copy.deepcopy(row)
    other['arm'] = 'native'
    return [row, other]


@pytest.mark.parametrize('change', ['message', 'tool', 'usage', 'content', 'status', 'json_type'])
def test_changed_provider_or_durable_work_refuses_measurement(change):
    rows = pair()
    candidate = rows[1]
    if change == 'message':
        candidate['requests'][0]['messages'][0]['content'] = 'different question'
    elif change == 'tool':
        candidate['requests'][0]['tools'] = []
    elif change == 'usage':
        candidate['usage']['prompt_tokens'] = 4
    elif change == 'content':
        candidate['content'] = 'partial answer'
    elif change == 'json_type':
        rows[0]['requests'][0]['enabled'] = True
        candidate['requests'][0]['enabled'] = 1
    else:
        candidate['status'] = 'error'
    receipt = comparison_receipt(rows)
    assert not receipt['measurement_permitted']
    assert not receipt['performance_admitted']


def test_equivalence_alone_cannot_admit_default_or_performance():
    rows = pair()
    rows[0]['usage'].update(request_dispatch_seq=1, stream_elapsed_ms=30)
    rows[1]['usage'].update(request_dispatch_seq=7, stream_elapsed_ms=10)
    receipt = comparison_receipt(rows)
    assert receipt['measurement_permitted']
    assert not receipt['default_selection_admitted']
    assert not receipt['performance_admitted']
