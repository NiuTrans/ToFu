"""Complete producer execution state retained independently of output previews.

Capture, receipt_execution_status and the streaming ExecutionStateReducer use
the research-program contract. No model verdict, prose substring or truncated
JSON prefix proves job completion; this observation is not a quiescence certificate.
"""
from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

from jsonschema import Draft202012Validator

STATES = {
    'planned': 'planned',
    'queued': 'queued', 'pending': 'queued', 'submitted': 'queued', 'accepted': 'queued',
    'running': 'running', 'in_progress': 'running',
    'completed': 'completed', 'done': 'completed', 'succeeded': 'completed',
    'failed': 'failed', 'error': 'failed', 'rejected': 'failed',
    'cancelled': 'failed', 'canceled': 'failed', 'aborted': 'failed',
}
STATE_VALUES = frozenset(STATES.values())


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                         'research_program_v1.schema.json').read_text())
    return Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/executionObservation'})


def normalize_execution_observation(value):
    if not _validator().is_valid(value):
        raise ValueError('Invalid Research execution observation')
    return deepcopy(value)


def extract_execution_observation(payload):
    if not isinstance(payload, Mapping):
        return None
    candidates = [(key, payload.get(key)) for key in ('job', 'result', 'data')]
    candidates.append(('', payload))
    for container, candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        identifiers = ('job_id', 'remote_job_id', 'id') if container == 'job' else ('job_id', 'remote_job_id')
        job_id = next((candidate[key] for key in identifiers if isinstance(candidate.get(key), str)
                       and candidate[key].strip() and len(candidate[key]) <= 240), None)
        state = next((STATES[value] for key in ('execution_status', 'job_status', 'state', 'status')
                      if (value := str(candidate.get(key) or '').strip().lower()) in STATES), None)
        # A named job object or explicit job handle is more specific than an
        # outer API/command success. Unknown state remains unresolved, including
        # malformed/oversized handles; identity is never invented or truncated.
        if state is None and (container == 'job' or any(key in candidate for key in ('job_id', 'remote_job_id'))):
            state = 'planned'
        if state is not None:
            observation = {'state': state}
            if job_id is not None:
                observation['job_id'] = job_id
            return observation
    return None


def provider_scope_for_tool(tool, bindings=()):
    provider = next((row.get('provider') for row in bindings
                     if row.get('tool') == tool and row.get('provider')), None)
    if provider:
        return 'provider:' + str(provider)
    components = str(tool).split('__', 2)
    if len(components) == 3 and components[0] == 'mcp' and components[1]:
        return 'mcp:' + components[1]
    return 'tool:' + str(tool)


class ExecutionStateReducer:
    """Fold chronological execution observations without retaining receipt history.

    Only active/failed scoped jobs remain in memory. A successful completed poll
    removes its job entry; transport failures cannot clear a producer observation.
    This derives observed state, never process quiescence or scientific validity.
    """
    def __init__(self):
        self._jobs = {}
        self._last = 'planned'

    def observe(self, receipt, state):
        if not isinstance(state, str) or state not in STATE_VALUES:
            raise ValueError('Invalid Research reduced execution state')
        observation = receipt.get('execution_observation')
        if receipt.get('status') == 'done' and isinstance(observation, Mapping):
            observation = normalize_execution_observation(observation)
            if observation.get('job_id'):
                if observation['state'] != state:
                    raise ValueError('Research reduced state conflicts with producer observation')
                scope = observation.get('provider_scope') or provider_scope_for_tool(receipt.get('tool') or '')
                key = scope, observation['job_id']
                if state == 'completed':
                    self._jobs.pop(key, None)
                else:
                    self._jobs[key] = state
        self._last = state

    @property
    def retained_jobs(self):
        return len(self._jobs)

    @property
    def status(self):
        states = set(self._jobs.values())
        return next((state for state in ('running', 'queued', 'planned', 'failed')
                     if state in states), self._last)


def reduce_execution_state(receipt_states):
    """Consume (receipt, parsed state) pairs once, including complete paged streams."""
    reducer = ExecutionStateReducer()
    for receipt, state in receipt_states:
        reducer.observe(receipt, state)
    return reducer.status


def receipt_execution_status(receipt, *, remote):
    """Transport success is not remote job completion."""
    if receipt.get('status') in {'error', 'rejected'}:
        return 'failed'
    if receipt.get('status') != 'done':
        return 'planned'
    try:
        observed = json.loads(str(receipt.get('result_excerpt') or ''))
    except (TypeError, ValueError):
        observed = None
    execution = receipt.get('execution_observation')
    if execution is not None:
        return normalize_execution_observation(execution)['state']
    legacy = extract_execution_observation(observed)
    if legacy is not None:
        return legacy['state']
    return 'planned' if remote else 'completed'



def execution_state_parser(bindings=()):
    """Freeze tool membership once for a chronological receipt pass."""
    from lib.paper.tools import paper_effective_tool_name
    evidence_tools = {'run_command', paper_effective_tool_name('run_command')} | {
        str(row.get('tool') or '') for row in bindings if row.get('capability') in {
            'experiment.execute', 'experiment.status', 'experiment.artifacts',
            'compute.submit', 'compute.status', 'evaluation.run'}}
    remote_tools = {str(row.get('tool') or '') for row in bindings if row.get('capability') in {
        'compute.submit', 'compute.status', 'experiment.status'}}
    def parse(receipt):
        observation = receipt.get('execution_observation')
        if (receipt.get('tool') in evidence_tools
                or (isinstance(observation, Mapping) and observation.get('job_id'))):
            return receipt_execution_status(receipt, remote=receipt.get('tool') in remote_tools)
        return None
    return parse


def iter_execution_states(receipts, bindings=()):
    """Parse relevant receipts once; captured scoped jobs remain relevant across bindings."""
    parse = execution_state_parser(bindings)
    for receipt in receipts:
        state = parse(receipt)
        if state is not None:
            yield receipt, state
