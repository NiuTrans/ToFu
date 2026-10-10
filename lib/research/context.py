"""Action-specific research context projections and token admission.

Durable workspace bytes are never edited by projection. Omitted text is marked;
compile/publication requiring exact sources fail before dispatch if they do not
fit, rather than compiling a silently truncated manuscript.
"""
from __future__ import annotations

from copy import deepcopy
import json

ACTION_OUTPUT_TOKEN_CEILING = 8_000
UNKNOWN_MODEL_CONTEXT_WINDOW = 32_000
CONTEXT_SAFETY_RESERVE = 2048


class ContextCapacityExceeded(ValueError):
    """The complete wire request exceeds the selected model's input capacity."""


def action_window(model: str) -> int:
    from lib.model_info._context import context_profile
    return int(context_profile(model).get('window') or UNKNOWN_MODEL_CONTEXT_WINDOW)


def output_budget(model: str) -> int:
    return min(ACTION_OUTPUT_TOKEN_CEILING, max(512, action_window(model) // 8))


def project_workspace(workspace, action):
    context = deepcopy(dict(workspace))
    context.pop('publication', None)
    context.pop('capability_bindings', None)  # sent once, outside workspace
    omitted = []
    if action not in {'manuscript', 'compile', 'publish'}:
        context.pop('source_files', None)
    for run in context.get('runs') or []:
        for receipt in run.get('tool_receipts') or []:
            if receipt.get('result_excerpt'):
                receipt.pop('result_excerpt', None)
                receipt['excerpt_omitted'] = True
                omitted.append('historical_receipt_excerpts')
    context['context_projection'] = {
        'omitted': sorted(set(omitted)),
        'note': 'Use recorded artifact references to inspect original evidence; omitted text is not verified.',
    }
    return context


def admit_messages(messages, *, model, tools=None, max_output=None, input_limit=None):
    """Fail closed before a paid call, counting tools and output reservation."""
    from lib.token_counter import count_text
    wire = json.dumps({'messages': messages, 'tools': tools or []},
                      ensure_ascii=False, separators=(',', ':'), default=str)
    tokens = count_text(wire, model=model)
    reserve = output_budget(model) if max_output is None else max_output
    if type(reserve) is not int or reserve<0:
        raise ValueError('research output reservation must be a nonnegative integer')
    if input_limit is not None and (type(input_limit) is not int or input_limit<1):
        raise ValueError('research input limit must be a positive integer')
    ceiling = action_window(model) - reserve - CONTEXT_SAFETY_RESERVE
    if input_limit is not None:ceiling=min(ceiling,input_limit)
    if tokens > ceiling:
        raise ContextCapacityExceeded(f'research context requires {tokens} input tokens; budget {ceiling}. '
                         'Reduce the working set or split the action; exact sources were not truncated.')
    return tokens


def prepare_action_messages(messages, *, model, tools, user_id):
    """Preserve oversized bound originals through existing owner-scoped reads."""
    from lib.identity import require_user_id
    user_id = require_user_id(user_id, context='research source continuation owner')
    try:
        admit_messages(messages, model=model, tools=tools)
        return messages
    except ContextCapacityExceeded:
        tool_names = {row.get('function', {}).get('name') for row in tools}
        if 'read_tool_artifact' not in tool_names:
            raise
        payload = json.loads(messages[1]['content'])
        source = payload.get('research_artifacts') or {}
        if not source.get('source_task_id'):
            raise
        original = json.dumps(source, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        import hashlib
        descriptor = {
            'source_task_id': source['source_task_id'],
            'source_digest': hashlib.sha256(original.encode()).hexdigest(),
            'total_chars': len(original), 'delivery': 'read_tool_artifact',
            'instruction': 'Read this original with read_tool_artifact using artifact_ref and follow returned cursors. Unread evidence is unobserved; this descriptor is not scientific support.',
        }
        projected = deepcopy(payload)
        projected['research_artifacts'] = descriptor
        prepared = deepcopy(messages)
        # Reserve room for the fixed-length semantic reference before storing.
        descriptor['artifact_ref'] = 'tool-result:' + '0' * 64
        projected['research_artifacts'] = descriptor
        prepared[1]['content'] = json.dumps(projected, ensure_ascii=False, separators=(',', ':'))
        admit_messages(prepared, model=model, tools=tools)
        from lib.tool_result_artifacts import ToolResultArtifactRepository
        stored = ToolResultArtifactRepository().put(user_id=user_id, content=original,
                                                   media_type='application/json')
        artifact_ref = str((stored or {}).get('artifactRef') or '')
        if not artifact_ref:
            raise ValueError('original research evidence retention was not acknowledged')
        descriptor['artifact_ref'] = artifact_ref
        prepared[1]['content'] = json.dumps(projected, ensure_ascii=False, separators=(',', ':'))
        admit_messages(prepared, model=model, tools=tools)
        return prepared


def restore_source_reference(messages, *, user_id, artifact_ref, load_source):
    """Reconstruct only the frozen source cache; never rerun its producer."""
    from lib.identity import require_user_id
    user_id = require_user_id(user_id, context='research source restoration owner')
    descriptor = json.loads(messages[1]['content']).get('research_artifacts') or {}
    if (descriptor.get('delivery') != 'read_tool_artifact'
            or not artifact_ref or descriptor.get('artifact_ref') != artifact_ref):
        return False
    from lib.tool_result_artifacts import ToolResultArtifactRepository
    repository = ToolResultArtifactRepository()
    if repository.read_range(user_id=user_id, artifact_ref=artifact_ref, limit=1) is not None:
        return False
    source = load_source()
    original = json.dumps(source, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    import hashlib
    if (source.get('source_task_id') != descriptor.get('source_task_id')
            or hashlib.sha256(original.encode()).hexdigest() != descriptor.get('source_digest')
            or len(original) != descriptor.get('total_chars')):
        raise ValueError('original research evidence changed; reference restoration refused')
    stored = repository.put(user_id=user_id, content=original, media_type='application/json')
    if (stored or {}).get('artifactRef') != artifact_ref:
        raise ValueError('original research reference restoration was not acknowledged')
    return True
