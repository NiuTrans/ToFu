"""Validate worker vocabulary and project automation documents.
Entries: load_worker, automation_runtime, automation_catalog_json. Reads share the invocation input snapshot."""

import json
from .inputs import ContractInputs


def load_worker(inputs: ContractInputs) -> dict:
    document = inputs.contract('worker_job_lifecycle_v1.json')
    states = document.get('states')
    terminals = document.get('terminalStates')
    if (document.get('format') != 'tofu.worker-job-lifecycle/v1' or not isinstance(states, list)
            or len(states) != len(set(states)) or any(not isinstance(s,str) or not s.isidentifier() for s in states)
            or terminals != ['succeeded', 'failed', 'cancelled'] or not set(terminals) <= set(states)
            or document.get('claimableStates') != ['queued', 'suspended']):
        raise ValueError('invalid canonical worker lifecycle')
    lease = document.get('leaseAuthority', {})
    if (not isinstance(lease, dict) or lease.get('leaseIndexMarker') != 255
            or lease.get('epochCharacters') != 32 or lease.get('maximumLeaseProbesPerKind') != 4
            or any(not isinstance(lease.get(key), str) or not lease[key]
                   for key in ('clock', 'lifetime', 'callerTime', 'restart', 'legacy'))):
        raise ValueError('invalid worker lease authority contract')
    server = lease.get('serverClock')
    if (lease.get('maximumLeaseMilliseconds') != 300_000 or not isinstance(server, dict)
            or server.get('format') != 'tofu.worker-lease-clock/v1'
            or server.get('minimumPostgresMajor') != 17 or server.get('maximumStateBytes') != 128
            or server.get('fields') != ['format', 'epoch', 'nowMs', 'utcMs', 'stateBytes']
            or not isinstance(lease.get('eligibilityClock'), str) or not lease['eligibilityClock']):
        raise ValueError('invalid shared server worker clock contract')
    return document


def automation_runtime(inputs: ContractInputs) -> dict:
    document = inputs.contract('automation_v1.schema.json')['x-runtime']
    for key in ('terminalRunStates', 'activeEffectStepStates', 'cancellablePendingStepStates'):
        values = document[key]
        if (not isinstance(values, list) or not values or len(set(values)) != len(values)
                or any(not isinstance(value, str) or not value.isidentifier() for value in values)):
            raise ValueError('Invalid automation lifecycle vocabulary')
    return document


def automation_catalog_json(inputs: ContractInputs) -> str:
    return json.dumps(inputs.contract('automation_actions_v1.json'),
                      sort_keys=True,ensure_ascii=False,separators=(',', ':'))
