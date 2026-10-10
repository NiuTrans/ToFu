"""Scoped recovery projection; checkpoints own outcomes, logs are evidence only.

Entry point: ``recover_agent_result`` for tool reads without a live result.
Persistence owns durable records; master owns checkpoint outcome normalization.
No directory scans, session mutation, or worker restart occurs on a result read.
"""

from __future__ import annotations

from lib.identity import require_user_id
from lib.log import get_logger
from lib.swarm.integration._config import swarm_key_for
from lib.swarm.integration._logs import _read_agent_log
from lib.swarm.master import _checkpoint_outcome, _last_assistant_text
from lib.tasks_pkg.manager import task_user_id

logger = get_logger(__name__)


def recover_agent_result(agent_id: str, *, task_id: str,
                         task: dict | None) -> dict | None:
    """Read only this owner's conversation (or exact task) recovery evidence.

    An authorized durable row locates the spawning task across turns. Missing
    rows permit only the exact current task's log, never a global filename
    search. Owner/scope mismatch denies even that fallback without disclosure.
    """
    try:
        owner = task_user_id(task)
    except ValueError:
        return None
    from lib.swarm import persistence

    key = swarm_key_for(task)
    try:
        row = persistence.load_session(key)
        if row is None and key != task_id:
            row = persistence.load_session(task_id)
    except Exception:
        # Do not leak storage paths/provider details or infer successful work
        # from an unavailable authority. Exact-task logs can still be evidence.
        logger.warning('[Swarm:%s] recovery checkpoint unavailable', task_id,
                       exc_info=True)
        row = None

    checkpoint = None
    source_task_id = task_id
    if row is not None:
        if not isinstance(row, dict):
            return None
        config = row.get('config')
        if not isinstance(config, dict):
            return None
        try:
            if require_user_id(config.get('user_id'), context='swarm recovery') != owner:
                return None
        except ValueError:
            return None
        conv_id = task.get('convId') or ''
        if (conv_id and row.get('conv_id') != conv_id) or (
                not conv_id and row.get('task_id') != task_id):
            return None
        parent_cfg = config.get('parent_cfg') or {}
        tenant_id = task.get('_tenant_id', (task.get('config') or {}).get('_tenant_id'))
        if not isinstance(parent_cfg, dict) or (
                parent_cfg.get('_tenant_id') != tenant_id):
            return None
        agents = row.get('agents')
        matches = [a for a in agents if isinstance(a, dict)
                   and a.get('agent_id') == agent_id] if isinstance(agents, list) else []
        if len(matches) > 1:
            return None  # Ambiguous identity must not select an arbitrary result.
        if matches:
            checkpoint = matches[0]
            source_task_id = row.get('task_id') or task_id

    checkpoint = checkpoint or {}
    result = checkpoint.get('result')
    result = result if isinstance(result, dict) else {}
    outcome = _checkpoint_outcome(checkpoint)
    answer = result.get('final_answer')
    answer = answer if isinstance(answer, str) else ''
    # A stored answer (successful or partial) is already the best evidence;
    # do not read or repeat its raw reasoning stream alongside it.
    log = _read_agent_log(source_task_id, agent_id) if not answer else None
    if not checkpoint and log is None:
        return None
    payload = {
        'status': 'ok',
        'found': True,
        'agent_id': agent_id,
        'agent_status': outcome,
        'source': 'persisted' if checkpoint else 'disk',
        'recovered': True,
        'persisted_status': checkpoint.get('status') or 'unknown',
        'final_answer': answer if outcome == 'completed' else '',
        'note': ('Recovered evidence, not a live worker. Only an explicit '
                 'error-free completed checkpoint establishes success; '
                 'partial_output is not a verified final answer.'),
    }
    if outcome != 'completed' and answer:
        payload['partial_output'] = answer
    elif log is not None and log[0].strip():
        # Never substitute streamed reasoning/content for a canonical answer.
        payload['partial_output'] = log[0]
    elif not answer and isinstance(checkpoint.get('messages'), list):
        partial = _last_assistant_text(checkpoint['messages'])
        if partial:
            payload['partial_output'] = partial
    if checkpoint.get('role'):
        payload['role'] = checkpoint['role']
    if isinstance(result.get('error_message'), str) and result['error_message']:
        payload['error'] = result['error_message']
    if outcome == 'unknown':
        payload['note'] += (
            ' No terminal outcome is verified. The worker may be awaiting '
            'restart recovery; do not infer success or automatically duplicate it.')
    return payload
