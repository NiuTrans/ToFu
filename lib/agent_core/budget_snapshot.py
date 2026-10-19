"""Closed consumption snapshot codec shared by execution and Flow recovery.

This is evidence about the existing ExecutionBudget, not another account or an
admission grant. Recovery owners must validate lineage before restoring it.
"""
from copy import deepcopy

from lib.agent_core.execution_limits import validate_execution_limits


FORMAT = 'tofu.execution-budget-snapshot/v1'
COUNTERS = ('tokens', 'root_tokens', 'tool_calls', 'model_ordinal', 'tool_ordinal')


def validate_snapshot(value):
    if (not isinstance(value, dict) or set(value) != {
            'format', 'owner_user_id', 'root_task_id', 'limits', 'complete', 'settled', 'usage_known', *COUNTERS}
            or value['format'] != FORMAT or type(value['owner_user_id']) is not int
            or value['owner_user_id'] < 1 or not isinstance(value['root_task_id'], str)
            or not value['root_task_id'] or len(value['root_task_id']) > 512
            or any(type(value[key]) is not bool for key in ('complete', 'settled', 'usage_known'))
            or (value['complete'] and not (value['settled'] and value['usage_known']))
            or any(type(value[key]) is not int or not 0 <= value[key] <= 18446744073709551615
                   for key in COUNTERS)
            or value['root_tokens'] > value['tokens']
            or not isinstance(value['limits'], dict)):
        raise ValueError('Execution budget snapshot is invalid')
    validate_execution_limits(value['limits'])
    if set(value['limits']) != {'max_tokens', 'max_tool_calls', 'deadline'}:
        raise ValueError('Execution budget snapshot policy is incomplete')
    return deepcopy(value)
