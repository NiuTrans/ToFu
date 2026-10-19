"""Discoverable working-fact contracts; domain schema is the sole payload owner."""
from copy import deepcopy

from lib.memory.working_facts.contract import contract
from lib.tools.contracts import ToolContractV2, ToolErrorContract


def _definition(name):
    definitions = contract()['$defs']
    def inline(value):
        if isinstance(value, dict):
            if '$ref' in value:
                return inline(definitions[value['$ref'].split('/')[-1]])
            return {key: inline(child) for key, child in value.items()}
        if isinstance(value, list):
            return [inline(child) for child in value]
        return value
    return inline(deepcopy(definitions[name]))


def _record_parameters():
    schema = _definition('WorkingFactRecord')
    schema['properties'].pop('action')
    schema['required'].remove('action')
    return schema


_PROPOSAL_ERRORS = (
    ToolErrorContract('working_fact_not_found', False,
        'Read current facts without an ids filter. To create a new fact, omit id; '
        'to update, use an existing id returned for this conversation.'),
    ToolErrorContract('working_facts_invalid_request', False,
        'Correct the arguments. Observation text must equal the exact source quote; '
        'use derived for a paraphrase or inference. Do not replay the rejected write unchanged.'),
    ToolErrorContract('working_facts_conflict', False,
        'Read current facts and source versions before proposing an update; do not retry a write blindly.'),
    ToolErrorContract('working_facts_forbidden', False,
        'Keep owner decisions intact. Read the current facts; submit a new sourced proposal only if authorized.'),
)

READ_WORKING_FACTS = ToolContractV2(
    name='read_working_facts', parameters=_definition('WorkingFactQuery'),
    model_description=('Read this conversation’s versioned working facts and source freshness. '
        'Set include_tool_sources=true to list recent canonical tool observations available for quoted facts. '
        'Reuse exact observed or owner-assessed records instead of repeatedly reading unchanged source bodies. '
        'Derived claims need review; stale/unavailable records require new evidence. These records never grant tool authorization.'),
    permission='read', idempotency='read_only', effect_recovery='read_only', ptc_eligible=True,
    search_metadata=('working facts source versions dependencies evidence', '工作事实 来源 版本 用户纠正'),
    decoded_result_schema=_definition('WorkingFactsView'),
    errors=(*_PROPOSAL_ERRORS, ToolErrorContract('working_facts_unavailable', True,
        'Retry the read when evidence is available. An unavailable result does not prove withdrawal.'),),
)
RECORD_WORKING_FACT = ToolContractV2(
    name='record_working_fact', parameters=_record_parameters(),
    model_description=('Save or revise a conversation working fact using the revision from read_working_facts. '
        'An observation must exactly quote one current artifact, acceptance criterion, file source, historical tool result, or observed fact. '
        'Criterion sources use targetKind=turn for ordinary task requirements; omitted targetKind means artifact. '
        'File sources require the id/version/path returned by read_files with bind_sources=true plus an exact quote. '
        'Use a tool source descriptor from read_working_facts(include_tool_sources=true) plus an exact quote. '
        'Tool observations are historical, not a check of current external state. '
        'Use derived for an inference: saving does not verify it. Sources bind exact digests/revisions. '
        'Owner-confirmed or corrected records cannot be overwritten; submit a new sourced proposal instead.'),
    permission='write', idempotency='none', ptc_eligible=False,
    search_metadata=('record working fact exact evidence dependency', '记录事实 引用证据 依赖'),
    decoded_result_schema=_definition('WorkingFactsView'),
    errors=(*_PROPOSAL_ERRORS, ToolErrorContract('working_facts_unavailable', False,
        'Read current facts and source versions to reconcile the outcome; do not infer failure or retry a write blindly.'),),
)
WORKING_FACT_CONTRACTS = (READ_WORKING_FACTS, RECORD_WORKING_FACT)
