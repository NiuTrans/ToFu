"""Lazy reader for process composition, distinct from durable rule policy.

Entry point: runtime_policy. ``capacityResource`` names the fixed idle-reuse
hint (never a job ceiling); this contract owns only automation control-loop
timings and lifecycle.
"""
from functools import lru_cache
import json
from pathlib import Path
from types import MappingProxyType


@lru_cache(maxsize=1)
def runtime_policy():
    path = Path(__file__).resolve().parents[2] / 'contracts/automation_runtime_v1.json'
    value = json.loads(path.read_text())
    if value.get('contract') != 'tofu.automation-runtime/v1':
        raise ValueError('Unknown automation runtime contract')
    for key in ('workerLeaseMilliseconds', 'heartbeatIntervalMilliseconds',
                'failureBackoffMilliseconds', 'actionWaitMilliseconds',
                'horizonReanchorMilliseconds'):
        if type(value.get(key)) is not int or value[key] <= 0:
            raise ValueError('Automation runtime timing must be a positive integer: ' + key)
    if value['heartbeatIntervalMilliseconds'] >= value['workerLeaseMilliseconds']:
        raise ValueError('Automation heartbeat must precede lease expiry')
    operations = value.get('claimWakeOperations')
    if (not isinstance(operations, list) or not operations
            or any(not isinstance(name, str) or not name for name in operations)
            or len(set(operations)) != len(operations)):
        raise ValueError('Automation claim wake operations must be unique names')
    value['claimWakeOperations'] = tuple(operations)
    return MappingProxyType(value)
