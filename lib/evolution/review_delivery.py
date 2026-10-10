"""Submit reviewer-authored JSON through the existing live terminal reply port.

The owner-bound review launch is durable authority; preparation itself is
ephemeral. Ordinary terminal checkpoints and run adjudication own persistence,
source coverage, verdicts and settlement. No new review loop or replay path.
"""
from __future__ import annotations

from copy import deepcopy
import json

from lib.storage.errors import StorageError
from lib.tasks_pkg.tool_delivery import arguments_digest, bind_delivery, require_current_sole_call
from .contracts import EvolutionContractError, EvolutionStateError
from .coordination import authorize_task

NAME = 'evolution_review'
_SOURCE_KINDS = {'result_source': 'deliverable', 'tool_evidence_source': 'tool_evidence',
                 'artifact_evidence_source': 'artifact_evidence',
                 'background_evidence_source': 'background_evidence'}
_BINDING_FIELDS = ('run_id', 'stage_launch', 'attempt', 'episode_id', 'objective',
    'acceptance_requirements', 'exploration_hypothesis', 'evidence_review_version', 'context_seed',
    'scientific_protocol', 'scientific_required_outcome', 'require_requirement_originals',
    *_SOURCE_KINDS)


class _ReviewInspectionIncomplete(EvolutionStateError):
    """Native bounded source progress must reach the reviewer without clipping."""


def tool_definition():
    from .hypothesis_review import review_schema
    from .contracts import load_schema
    schema = deepcopy(load_schema('evolution_run_v1.schema.json')['$defs']['review_submit_request'])
    schema['properties']['review'] = review_schema({}, include_optional_hypothesis=True)
    return {'type': 'function', 'function': {'name': NAME,
        'description': 'Submit the independent review after inspecting frozen originals. '
            'Use as the sole tool call in a round. Supply the review JSON required by '
            'the current review request, including its exact requirements, evidence audit '
            'and hypothesis identity where required. Pass, fail and unknown are valid '
            'judgments; missing proof remains unknown. Submission ends the live review '
            'through ordinary settlement; native source coverage and adjudication still '
            'decide acceptance. Do not repair executor work.', 'parameters': schema}}


def build(ctx):
    binding = ctx.cfg.get('evolution') or {}
    return [tool_definition()] if binding.get('run_id') and binding.get('stage') == 'reviewing' else []


def _current(task):
    from .source_tools import _review_source, scientific_review_binding_matches
    from .runs import _review_source_tools_available
    run, role, _ = authorize_task(task)
    if (role != 'reviewing' or task.get('status') in {'done', 'error', 'aborted', 'interrupted'}
            or not _review_source_tools_available(run)):
        raise EvolutionStateError('submission requires the current source-backed reviewer')
    if not scientific_review_binding_matches(run, owner=task['_userId']):
        raise EvolutionStateError('scientific review differs from its frozen executor protocol')
    for key, kind in _SOURCE_KINDS.items():
        if run.get(key) and _review_source(run, task['_userId'], kind) != run[key]:
            raise EvolutionStateError('review source identity changed')
    return run


def _require_review_inspection(task, run, authored):
    """Unread sources cannot turn an unfinished inspection into executor repair."""
    from . import runs
    from .source_tools import _review_read_progress, requirement_originals_match
    checked_sources = {}
    if (run.get('require_requirement_originals') and any(
            item['outcome'] == 'pass' and not item.get('original_sources')
            for item in authored.get('requirement_results') or [])
            and not any(row['outcome'] == 'fail' for row in authored['criterion_results'] + (authored.get('requirement_results') or []))):
        raise _ReviewInspectionIncomplete('Final requirement evidence manifest is unfinished; link exact delivered original_sources for every passing requirement in this reviewer.')
    for item in authored.get('requirement_results') or []:
        if (item.get('original_sources') and not requirement_originals_match(
                task, run, item['original_sources'], checked_sources=checked_sources,
                require_delivery=False)):
            raise EvolutionStateError(f"{item['requirement_id']}: requirement original reference is outside authorized frozen originals; use evolution_source to read current or owned program originals and correct it in this reviewer")
    unfinished_links = [item for item in authored.get('requirement_results') or []
        if item['outcome'] == 'pass' and item.get('original_sources')
        and not requirement_originals_match(task, run, item['original_sources'],
            checked_sources=checked_sources, require_delivery=True)]
    if unfinished_links:
        projected = deepcopy(authored)
        runs._enforce_review_evidence(task, run, projected)
        if not any(item['outcome'] == 'fail' for item in projected['criterion_results']):
            raise _ReviewInspectionIncomplete('Required original inspection is unfinished; finish delivering the referenced originals in this reviewer before positive submission.')
    sources = [run[key] for key in _SOURCE_KINDS if run.get(key)]
    from .source_tools import history_read_progress, review_next_action
    progress = _review_read_progress(task, run, sources) if sources else None
    history = history_read_progress(task, run, owner=task['_userId'])
    action = review_next_action(progress, history)
    if action == 'submit_independent_review':
        return
    if (action == 'submit_unknown_review'
            and any(row['criterion_id'] == 'objective' and row['outcome'] == 'unknown'
                    for row in authored['criterion_results'])):
        # Missing immutable history cannot be finished by repeating read calls.
        # Native adjudication still preserves unknown and refuses acceptance.
        return
    projected = deepcopy(authored)
    runs._enforce_review_evidence(task, run, projected)
    if any(item['outcome'] == 'fail' for item in projected['criterion_results']):
        # A material negative judgment can be established by a counterexample;
        # it does not need the positive acceptance coverage of every source.
        return
    raise _ReviewInspectionIncomplete('Review inspection is unfinished; continue reading missing '
        'sources in this reviewer instead of submitting an uninspected judgment. '
        + json.dumps({'review_next_action':action,'current':progress,'historical':history}, ensure_ascii=False, separators=(',', ':')))


def prepare(task, arguments, *, call_id, round_num):
    from . import runs
    runs._validate(arguments, 'review_submit_request')
    require_current_sole_call(task, call_id, NAME, arguments)
    # Preserve duplicate-key refusal when the current provider carrier retains
    # raw JSON arguments. Already-decoded carriers cannot recover lost syntax.
    message = next(m for m in reversed(task['_compact_messages']) if m.get('role') == 'assistant')
    raw = message['tool_calls'][0]['function']['arguments']
    if isinstance(raw, str):
        if json.loads(raw, object_pairs_hook=runs._review_object) != arguments:
            raise EvolutionContractError('review arguments changed during dispatch')
    run = _current(task)
    authored = deepcopy(arguments['review'])
    runs._validate_review_submission(authored, run)
    _require_review_inspection(task, run, authored)
    content = json.dumps(authored, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(content) > runs.review_output_bound(run, include_original_sources=runs.review_has_original_sources(authored), task=task):
        raise EvolutionContractError('review exceeds output bound')
    binding = {key: deepcopy(run.get(key)) for key in _BINDING_FIELDS}

    def verify():
        try:
            current = _current(task)
            return {key: current.get(key) for key in _BINDING_FIELDS} == binding
        except (EvolutionContractError, EvolutionStateError, StorageError, OSError, ValueError, TypeError):
            return False

    acknowledgement = 'review_submission:' + arguments_digest(arguments)
    bind_delivery(task, call_id=call_id, name=NAME, round_num=round_num,
        arguments=arguments, content=content, acknowledgement=acknowledgement, verify=verify)
    return {'run_id': run['run_id'], 'task_id': task['id'], 'review_prepared': True,
        'acknowledgement': acknowledgement, 'acceptance': 'pending native review adjudication'}


def handle(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
           cfg, project_path, project_enabled, all_tools=None):
    from .tool_handler import _entry
    try:
        result = prepare(task, fn_args or {}, call_id=tc_id, round_num=round_entry['llmRound'])
    except _ReviewInspectionIncomplete as exc:
        return [_entry(fn_name, 'review submission refused', str(exc), fetched=False)]
    except (EvolutionContractError, EvolutionStateError, StorageError, OSError, ValueError, TypeError) as exc:
        return [_entry(fn_name, 'review submission refused', str(exc)[:400], fetched=False)]
    return [_entry(fn_name, 'review submission prepared', json.dumps(result, ensure_ascii=False))]
