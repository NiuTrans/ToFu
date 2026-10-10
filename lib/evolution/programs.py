"""Durable exploration programs composed from the existing evolution run port.

The program owns an immutable objective, a bounded frontier, frozen launch
intents and sequential, immutable experiment receipts. Runs own all execution,
review, task journals and learning. No thread, model loop or database dialect
lives here. Entry points: create_program, advance_program, command_program,
append_steps, get_program, experiment_page. Identity is explicit throughout.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import time

from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError
from . import records, runs, replanning
from .contracts import EvolutionContractError, EvolutionStateError, validate_program as _validate, load_schema

SCHEMA = 'evolution_program_v1.schema.json'
_FROZEN_GENERATION_FIELDS = ('candidate_digest', 'baseline_digest')
PROGRAMS = 'evolution.programs.v1'
EXPERIMENTS = 'evolution.program_experiments.v1'
REGISTRY = 'evolution.program_registry.v1'
EXPANSIONS = 'evolution.program_expansions.v1'
logger = get_logger(__name__)
TERMINAL = {'completed', 'cancelled'}
MEMORY_WINDOW = 6
MEMORY_PREVIEW_CHARS = 800


def _key(program_id, user_id):
    return records.entity_key(program_id, user_id, kind='program')


def _read(program_id, user_id):
    value, version = records.get_versioned_record(PROGRAMS, _key(program_id, user_id))
    if value is None:
        raise EvolutionStateError('unknown exploration program for this owner')
    return value, version


def _save(value, version, user_id):
    value['updated_at'] = int(time.time())
    _validate(value)
    key = _key(value['program_id'], user_id)
    expected = deepcopy(value)
    def exact_readback():
        stored, observed_version = records.get_versioned_record(PROGRAMS, key)
        return stored == expected and observed_version == version + 1
    try:
        acknowledged = records.put_record(PROGRAMS, key, value,
            kind='program', expected_version=version)
    except StorageError as error:
        if error.code == 'database_conflict' or not exact_readback():
            raise
        return version + 1
    if acknowledged != version + 1 or not exact_readback():
        raise StorageError('database_conflict', 'Programme state publication was not exactly acknowledged; reload authority')
    return acknowledged


def get_program(program_id, *, user_id):
    """Durable authority only; reading never launches work or claims progress."""
    return _read(program_id, user_id)[0]


def list_programs(*, user_id, limit=50):
    programs = records.list_records(PROGRAMS, user_id, kind='program', limit=limit)
    summaries = []
    for program in sorted(programs, key=lambda row: row['updated_at'], reverse=True):
        summaries.append(_program_summary(program))
    return summaries


def _program_summary(program):
    current = program['current']
    summary = {key: program[key] for key in
               ('program_id', 'status', 'settled_through', 'created_at', 'updated_at')}
    summary.update(objective=program['objective'][:180], pending_steps=len(program['queue']),
                   current_title=current['step']['title'] if current else '')
    return _validate(summary, 'summary')


def program_page(*, user_id, after='', before='', limit=20):
    """Stable identifier order; one live key range, never a full catalog scan."""
    _validate({'after': after, 'before': before, 'limit': limit}, 'catalog_request')
    uid = require_user_id(user_id, context='exploration program owner')
    effective_limit = limit
    while True:
        try:
            rows = records.list_record_page(PROGRAMS, uid, kind='program', limit=effective_limit + 1,
                after_key=_key(after, uid) if after else '', before_key=_key(before, uid) if before else '')
            break
        except StorageError as error:
            if not records.is_record_page_resource_refusal(error) or effective_limit == 1:
                raise
            effective_limit = max(1, effective_limit // 2)
    extra = len(rows) > effective_limit
    page = rows[:effective_limit]
    for row in page:
        if row['key'] != _key(row['value']['program_id'], uid):
            raise StorageError('database_protocol_error', 'Exploration catalog identity mismatch')
    if before:
        page.reverse()
    summaries = [_program_summary(row['value']) for row in page]
    first = summaries[0]['program_id'] if summaries else before or after
    last = summaries[-1]['program_id'] if summaries else before or after
    return _validate({'programs': summaries, 'order': 'program_id_asc',
        'next_cursor': last if (bool(before) or extra) else None,
        'previous_cursor': first if (extra if before else bool(after)) else None}, 'catalog_page')


def _default_steps():
    return [
        {'kind': 'discovery', 'title': 'Establish the problem and evidence',
         'instruction': 'Investigate who experiences the problem, concrete incidents, '
                        'current workarounds and the cost of leaving it unsolved. '
                        'Inspect actual project artifacts and sources; separate observed '
                        'facts from assumptions and imagined demand. Identify unmet needs '
                        'and overlooked application scenarios, including emerging ones. '
                        'Describe the user, triggering situation, failed workaround and '
                        'observed consequence. Look for repeated friction or needs users '
                        'cannot yet articulate; treat inferred demand as a hypothesis. '
                        'Treat a requested feature or preferred technology as a proposed '
                        'means, not evidence of the underlying need. Within the original '
                        'objective, compare candidate problems by observed frequency, '
                        'severity and current alternatives; investigate alternative '
                        'explanations and low-effort nontechnical workarounds. Distinguish '
                        'a causal bottleneck from its symptoms. State what observation '
                        'would refute the selected problem and the cheapest way to obtain '
                        'it. Missing evidence means unknown; emerging needs remain '
                        'conditional hypotheses rather than invented observations. '
                        'Save a reproducible protocol covering the original requirements. '
                        'Use evolution_explore to recall prior experiments and schedule '
                        'evidence-backed follow-up experiments when new questions emerge.',
         'checks': ['The protocol names observable checks for every original requirement.',
                    'Problem claims name the affected audience, triggering situation, failed '
                    'workaround and observed consequence with inspected evidence; inferred '
                    'or emerging demand is explicitly unvalidated.',
                    'Problem selection examines frequency, severity, alternative explanations '
                    'and low-effort current workarounds. It distinguishes observed causes from '
                    'symptoms, states an observation that would refute the need and a protocol '
                    'to obtain it; missing support remains unknown.']},
        {'kind': 'baseline', 'title': 'Measure the baseline',
         'require_command_execution': True,
         'instruction': 'Run the protocol against the current state. Save reproducible '
                        'commands, inputs, outputs and measured failure cases. Do not '
                        'report assumed measurements. Include the existing workaround '
                        'and a user-relevant outcome such as time, errors, cost or task '
                        'completion; record setup and operating effort where relevant. '
                        'Include competent present alternatives such as a workflow change '
                        'or nontechnical workaround, rather than only a weak technical rival. '
                        'Label observations as direct execution of a named pinned implementation, '
                        'archived observation, or an authored model/simulation. Keep these '
                        'populations and their denominators separate; a modeled-rule aggregate '
                        'is not an application accuracy measurement. Qualify the reference '
                        'comparator with adversarial scope and identity cases, missing or '
                        'ambiguous prerequisites, and explicit abstention before calling it '
                        'competent. Compare competent baselines and candidates with the same '
                        'available information, actions and cost accounting. Baseline-only '
                        'work cannot establish candidate superiority, equivalence/null, or '
                        'negligible overhead; those claims need candidate/control measurements.',
         'checks': ['Baseline results trace to actual commands or inspected artifacts.',
                    'User-relevant baseline outcomes and the current workaround are explicit; '
                    'unmeasured costs remain unknown.',
                    'Every result names its implementation/version and observation type; '
                    'aggregate denominators keep direct, archived and modeled populations separate.',
                    'Reference qualification includes scope/identity counterexamples and '
                    'missing or ambiguous prerequisites; unresolved qualifications block performance claims.',
                    'Candidate superiority, equivalence/null and overhead claims require '
                    'measured candidate/control comparisons; baseline-only work cannot establish them.']},
        {'kind': 'experiment', 'title': 'Explore and implement alternatives',
         'instruction': 'Construct competing technical mechanisms and application '
                        'scenarios, including approaches that do not yet exist. Explain '
                        'the enabling change, assumptions, who benefits and why current '
                        'solutions cannot provide that value. For each emerging scenario, '
                        'name its triggering situation, required technical or behavioral '
                        'conditions, and which prerequisites remain untested. Claims '
                        'that no solution exists need inspected comparison sources. '
                        'Test enabling-change necessity: could a competent present '
                        'alternative supply the same user value without that change, '
                        'under equal information, actions and cost accounting? If so, '
                        'revise the claimed advantage, retaining the counterexample. '
                        'A simulation that assumes the enabling change cannot prove '
                        'its availability or necessity. '
                        'Keep speculative branches with explicit enabling conditions '
                        'when a prerequisite does not exist yet. Name an observable '
                        'result that would falsify each mechanism or scenario. If a '
                        'test cannot run, record its protocol and missing prerequisite; '
                        'do not present it as a measured result. '
                        'Compare to the measured '
                        'baseline. Use the cheapest discriminating prototype or test '
                        'to falsify each hypothesis. Preserve negative results. Add '
                        'follow-up experiments with evolution_explore when findings '
                        'open a new question; implement the strongest supported approach.',
         'checks': ['The selected approach has evidence and rejected alternatives have reasons.',
                    'New mechanisms and scenarios distinguish hypotheses from validated consumer value.',
                    'Each competing mechanism names a causal explanation, assumptions, baseline '
                    'comparison and an observable falsifier, tested or explicitly untested.',
                    'Each emerging scenario names beneficiaries, trigger, enabling conditions, '
                    'unresolved prerequisites and an observable falsifier; feasibility or novelty '
                    'alone does not establish demand or net user benefit.',
                    'Enabling change necessity is compared against competent present '
                    'alternatives at equal information, actions and cost. Claimed limitations '
                    'cite inspected comparisons; an assumed prerequisite cannot prove its '
                    'availability or necessity. Protocol-only future work records the '
                    'discriminating test and unknowns without claiming a measured advantage.']},
        {'kind': 'synthesis', 'title': 'Stress and integrate the result',
         'instruction': 'Before concluding, use evolution_explore action=validation_context and then action=preregister to freeze the next validation protocol, paired seeds, numeric direction/threshold/SE margin and required outcome. Inspect and freeze dataset/split identities, matched per-system budget and both candidate/baseline source/configuration generation digests in measurement_scope before independent validation. Missing identities must be established before registration; changing a generation requires a new protocol. Inspect missing facts rather than fabricate digests. Ground choices in the objective and inspected evidence; do not guess missing values. A proposal is not a result. '
                        'Integrate the selected changes. Exercise failure, restart, '
                        'concurrency and usability boundaries relevant to the objective. '
                        'Fix regressions and preserve the reproduction artifacts. '
                        'Compare the integrated workflow with the user-relevant baseline, '
                        'including adoption effort and operating costs. State who '
                        'benefits, by how much, under which tested conditions; leave '
                        'unsupported demand or benefit unresolved.',
         'checks': ['Relevant integration and failure checks have observed results.',
                    'Consumer-value conclusions trace to observed baseline comparisons; '
                    'prototypes and technical benchmarks alone do not establish demand.']},
        {'kind': 'experiment', 'title': 'Independently reproduce and validate the integrated result',
         'require_command_execution': True, 'requires_scientific_protocol': True,
         'requires_frozen_measurement_scope': True,
         'required_observation_format': 'tofu.scientific-observations/v2',
         'instruction': 'Freeze the integrated candidate and competent baseline before validation: '
                        'record source/version digests, data/input identities, environment, '
                        'commands, seeds, metric definitions and matched resource budgets. '
                        'From this fresh task, actually rerun that frozen protocol using new '
                        'execution outputs; old reports, cached outputs and model assertions '
                        'are reference material, not reproduction. Keep fitting, tuning and '
                        'selection data separate from held-out validation. Compare paired '
                        'candidate/control measurements with uncertainty and all attempted '
                        'runs, including failures; examine scope, distribution and machine '
                        'conditions relevant to the objective. For papers, test central claims '
                        'on held-out data and include ablations or a discriminating control; '
                        'for systems, include correctness, failure/restart and performance '
                        'under relevant resource/load conditions. A negative or null result '
                        'may be valid: report it and narrow claims honestly rather than '
                        'cherry-picking favorable reruns. Do not tune on validation outcomes '
                        'while calling them held out. Audit validation feedback exposure across '
                        'candidate selection, repairs and earlier experiments. Reusing the same '
                        'data after outcome-driven changes is adaptive evaluation, even with '
                        'fresh commands, new seeds or a new candidate digest. Record every '
                        'exposure and selection decision. Establish untouched independent '
                        'confirmation data or an inspected, justified adaptive-validity procedure '
                        'before making held-out generalization claims; ordinary paired standard '
                        'errors alone do not resolve selection bias. If neither is available, '
                        'retain the result as exploratory and leave independence unknown. '
                        'Do not rewrite an existing frozen split: propose a separate confirmation '
                        'experiment through the ordinary frontier admission. If the candidate changes, freeze the '
                        'new generation and repeat validation; earlier results apply only '
                        'to their original generation. Missing prerequisites remain explicit '
                        'unknowns and cannot pass. Save the frozen inputs, actual command '
                        'outputs and result discrepancies so independent review can inspect '
                        'the evidence; a reproducible command alone is an unrun plan.',
         'checks': ['Reproduction identifies frozen candidate/control versions, input/data '
                    'identities, environment, commands, seeds, metrics and matched budgets.',
                    'Fresh reproduction has observed execution outputs for the frozen '
                    'candidate/control; prior reports, cached measurements and model '
                    'assertions cannot substitute for new execution.',
                    'Execution provenance connects the measured command to the actual '
                    'candidate/control code, configuration, data and split used. Matching '
                    'file fingerprints or reported digests alone do not prove input use; '
                    'unresolved mismatches and missing provenance remain unknown.',
                    'Held-out validation is separate from fitting, tuning and selection; '
                    'relevant controls or ablations distinguish the central explanation.',
                    'Validation feedback exposure and adaptive selection history are documented. '
                    'Held-out generalization claims require untouched independent confirmation '
                    'or an inspected justified adaptive-validity procedure; repeated outcomes, '
                    'new seeds and candidate digests alone cannot establish independence. '
                    'Unresolved selection bias remains unknown even when numerical checks pass.',
                    'Paired results report uncertainty and every attempted run, including '
                    'failures, negative/null findings and discrepancies; supported claims '
                    'are bounded by tested scope rather than selected favorable reruns.',
                    'Reproduction applies to the current integrated generation; edits '
                    'invalidate older validation, and missing prerequisites remain unknown.']},
        {'kind': 'final', 'title': 'Audit and deliver the complete objective',
         'instruction': 'Audit every original requirement against the current artifacts '
                        'and runtime. Complete missing work. Deliver usable artifacts '
                        'and evidence. Partial or unverified work cannot pass.',
         'checks': ['Every original acceptance requirement has current supporting evidence.',
                    'Final claims distinguish observed problem evidence, tested mechanisms, '
                    'unvalidated emerging scenarios and unresolved demand or net benefit; '
                    'novelty claims cite inspected comparisons or remain unverified.']},
    ]


def _required_measurement_scope_fields(step):
    """Project the admission policy from the retained protocol contract."""
    if not step.get('requires_frozen_measurement_scope'):
        return []
    scope = load_schema('research_program_v1.schema.json')['$defs']['protocol']['properties']['measurement_scope']
    return list(dict.fromkeys([*scope['required'], *_FROZEN_GENERATION_FIELDS]))


def _require_frozen_measurement_scope(step, protocol):
    """Enforce the launch-stamped scope policy; digests are plans, not authenticity."""
    if step.get('required_observation_format') and protocol.get('observation_format') != step['required_observation_format']:
        raise EvolutionStateError('required validation observation format was not preregistered')
    if not step.get('requires_frozen_measurement_scope'):
        return
    from lib.research.observation_audit import normalize_measurement_scope
    raw = protocol.get('measurement_scope')
    missing = [field for field in _required_measurement_scope_fields(step)
               if not isinstance(raw, Mapping) or field not in raw]
    if missing:
        raise EvolutionStateError('validation requires frozen measurement_scope fields: ' + ', '.join(missing))
    try:
        normalize_measurement_scope(raw)
    except ValueError as exc:
        raise EvolutionStateError('validation requires a frozen dataset, split and matched budget scope') from exc


def _check_frontier(steps):
    finals = [index for index, step in enumerate(steps) if step['kind'] == 'final']
    for step in steps:
        if step.get('scientific_protocol'):
            _require_frozen_measurement_scope(step, step['scientific_protocol'])
    if finals != [len(steps) - 1]:
        raise EvolutionContractError('the frontier must end with exactly one final objective audit')


def _external_steps(steps):
    if any('validation_parent_sequence' in step or 'failed_candidate_index' in step for step in steps):
        raise EvolutionContractError('validation generation lineage is admitted only through reviewed replanning')
    if any(step['kind'] == 'replan' for step in steps):
        raise EvolutionContractError('replan steps are admitted only from settled failure evidence')


def _research_source_payload(reference, *, user_id):
    from lib.research import records as research_records
    try:
        source = research_records.load_completed_idea_source(reference['task_id'],
            user_id=user_id,idea_id=reference['idea_id'])
    except ValueError as error:
        raise EvolutionStateError('owned research source unavailable') from error
    artifacts = source['result']
    selected = next(idea for idea in artifacts['accepted']
                    if isinstance(idea,dict) and str(idea.get('id') or '') == reference['idea_id'])
    payload = {'task_id':reference['task_id'],'idea_id':reference['idea_id'],
        'direction':source['direction'],'lang':source['lang'],
        'selected_idea':deepcopy(selected),'survey_md':artifacts.get('survey_md') or '',
        'open_gaps':deepcopy(artifacts.get('open_gaps') or {})}
    encoded = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    return payload, encoded, hashlib.sha256(encoded.encode()).hexdigest()


def read_research_source(program, *, user_id, source_digest=None, offset=0):
    """Exact prior research pages; no execution or scientific acceptance grant."""
    reference = program.get('research_source')
    if reference is None:
        raise EvolutionStateError('program has no bound research source')
    _, encoded, digest = _research_source_payload(reference,user_id=user_id)
    if digest != reference['source_digest'] or (source_digest is not None and source_digest != digest):
        raise EvolutionStateError('original research source identity changed')
    if type(offset) is not int or not 0 <= offset <= len(encoded):
        raise EvolutionContractError('research source offset is beyond the original')
    from .exploration_tools import CHUNK_CHARS
    end = min(len(encoded),offset+CHUNK_CHARS)
    return {'source_digest':digest,'offset':offset,'total_chars':len(encoded),
        'content':encoded[offset:end],'next_offset':end if end < len(encoded) else None,
        'evidence_status':'Original discovery proposal and grounding; scientific validity remains unverified.'}


def create_program(payload, *, user_id, idempotency_key):
    uid = require_user_id(user_id, context='exploration program owner')
    _validate(payload, 'request')
    runs.validate_review_reserve(payload)
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 160:
        raise EvolutionContractError('Idempotency-Key is required (1–160 characters)')
    request = deepcopy(payload)
    request['objective'] = request['objective'].strip()
    if request.get('project_path'):
        request['project_path'] = runs.resolve_project_path(request['project_path'])
    research_source = None
    if request.get('research_source') is not None:
        payload, _, source_digest = _research_source_payload(request['research_source'],user_id=uid)
        research_source = {key:payload[key] for key in ('task_id','idea_id','direction','lang')}
        research_source['source_digest'] = source_digest
    steps = request.get('steps') or _default_steps()
    _check_frontier(steps)
    _external_steps(steps)
    identity = {**request, **({'research_source_identity':research_source} if research_source is not None else {})}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    program_id = 'program-' + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    key = _key(program_id, uid)
    existing = records.get_record(PROGRAMS, key)
    if existing:
        if existing['request_digest'] != digest:
            raise EvolutionStateError('Idempotency-Key belongs to another exploration request')
        return existing
    from .review_evidence import VERSION as evidence_version
    now = int(time.time())
    value = {'schema_version': 1, 'program_id': program_id,
             **({'research_source':research_source} if research_source is not None else {}),
             'objective': request['objective'],
             **({'final_evidence_version':1} if evidence_version >= 3 else {}),
             'acceptance': request.get('acceptance') or list(runs.RUBRIC.values()),
             'request_digest': digest,
             'auto_replan': request.get('auto_replan', True),
             'advance_mode': request.get('advance_mode', 'continuous'),
             'started_at': None,
             'execution_options': {key: request[key] for key in
                 ('budget', 'review_reserve', 'model', 'role_models', 'project_path', 'max_attempts', 'max_review_recoveries', 'max_review_format_retries') if key in request},
             'status': 'ready', 'queue': steps, 'current': None,
             'settled_through': 0, 'tool_calls_complete': True, 'actuals': {key: 0 for key in
                 ('tokens', 'wall_clock_seconds', 'tool_calls')},
             'error': '', 'created_at': now, 'updated_at': now}
    try:
        _save(value, 0, uid)
    except StorageError as exc:
        if exc.code != 'database_conflict':
            raise
        existing = get_program(program_id, user_id=uid)
        if existing['request_digest'] != digest:
            raise EvolutionStateError('Idempotency-Key belongs to another exploration request')
        return existing
    return value


def _experiment_key(program_id, sequence, user_id):
    return records.entity_key(f'{program_id}.{sequence}', user_id, kind='experiment')


def _experiment_window(program_id, user_id, after, limit):
    if type(after) is not int or after < 0:
        raise EvolutionContractError('after must be a nonnegative integer')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise EvolutionContractError('limit must be an integer between 1 and 50')
    program=get_program(program_id,user_id=user_id)
    return program,min(program['settled_through'],after+limit)


def _experiment_receipts(program_id, user_id, after, end):
    for sequence in range(after+1,end+1):
        value=records.get_record(EXPERIMENTS,_experiment_key(program_id,sequence,user_id))
        if value is None:
            raise EvolutionStateError(f'experiment receipt {sequence} is unavailable')
        if value.get('program_id')!=program_id or value.get('sequence')!=sequence:
            raise EvolutionStateError('experiment receipt identity changed')
        yield value


def iter_experiments(program_id, *, user_id, after=0, limit=20):
    """Lazy owner-scoped window; one authority read, no prefetch or retained cache."""
    _,end=_experiment_window(program_id,user_id,after,limit)
    return _experiment_receipts(program_id,user_id,after,end)


def experiment_page(program_id, *, user_id, after=0, limit=20):
    """Sequence cursors give constant-sized reads independent of lifetime history."""
    program,end=_experiment_window(program_id,user_id,after,limit)
    items=list(_experiment_receipts(program_id,user_id,after,end))
    return {'experiments':items,'next_cursor':end if end<program['settled_through'] else None,
            'settled_through':program['settled_through']}



def continuation_source(program):
    """Immutable mission and hypothesis for the admitted/current experiment."""
    step = program['current']['step'] if program['current'] else program['queue'][0]
    source = {'program_id': program['program_id'], 'original_objective': program['objective'],
        'original_acceptance': deepcopy(program['acceptance']),
        'current_experiment': {key: step[key] for key in ('kind', 'title')}}
    if step.get('hypothesis'):
        source['current_hypothesis'] = deepcopy(step['hypothesis'])
    return source


def _fit_context(seed):
    """Budget presentation only; immutable requirements and receipts never shrink."""
    def encode(value):
        return json.dumps(value, ensure_ascii=False)
    encoded = encode(seed)
    if len(encoded) <= 24000:
        return encoded
    def previews(width):
        rows = deepcopy(seed['recent_experiments'])
        for row in rows:
            result, error = row['result_preview'], row['error']
            row['result_preview'] = result[:width]
            row['preview_truncated'] = row['preview_truncated'] or len(result) > width
            row['error'] = error[:width]
            row['error_truncated'] = len(error) > width
        return rows
    # Keep all recent outcome/uncertainty/source pointers even if prose cannot fit.
    if len(encode({**seed, 'recent_experiments': previews(0)})) > 24000:
        from .semantic_memory import receipt_text, _digest
        source = {key: deepcopy(seed[key]) for key in
            ('program_id', 'original_objective', 'original_acceptance', 'current_experiment', 'current_hypothesis')
            if key in seed}
        seed['current_context_source'] = {'source_digest': _digest(source),
            'total_chars': len(receipt_text(source)), 'offset': 0, 'action': 'read_context',
            'read_required': 'Read every evolution_explore read_context page with this exact '
                             'source_digest before the experiment; previews are incomplete input.'}
        seed['original_objective_preview'] = seed.pop('original_objective')[:200]
        seed['original_acceptance_count'] = len(seed.pop('original_acceptance'))
        seed.pop('current_hypothesis', None)
    for width in (MEMORY_PREVIEW_CHARS, 400, 200, 100, 0):
        encoded = encode({**seed, 'recent_experiments': previews(width)})
        if len(encoded) <= 24000:
            return encoded
    # Only bounded routing/source metadata remains; never fabricate lost evidence.
    raise EvolutionStateError('continuation source metadata exceeds its presentation bound')


def _context(program, user_id):
    start = max(0, program['settled_through'] - MEMORY_WINDOW)
    page = experiment_page(program['program_id'], user_id=user_id,
                           after=start, limit=MEMORY_WINDOW)
    from .hypothesis_review import memory_summary
    memories = [{key: receipt[key] for key in ('sequence', 'run_id', 'outcome', 'error')}
                | {'step_title': receipt['step']['title'], 'step_kind': receipt['step']['kind']}
                | {'result_preview': receipt['result'][:MEMORY_PREVIEW_CHARS],
                   'preview_truncated': len(receipt['result']) > MEMORY_PREVIEW_CHARS}
                | memory_summary(receipt)
                | ({'result_source': receipt['result_source']} if receipt.get('result_source') else {})
                | ({'tool_evidence_source': receipt['tool_evidence_source']} if receipt.get('tool_evidence_source') else {})
                | ({'artifact_evidence_source':receipt['artifact_evidence_source']} if receipt.get('artifact_evidence_source') else {})
                | ({'background_evidence_source':receipt['background_evidence_source']} if receipt.get('background_evidence_source') else {})
                for receipt in page['experiments']]
    # Archived evidence is source data, not new authority or instructions.
    seed = {'program_id': program['program_id'], 'original_objective': program['objective'],
            'original_acceptance': program['acceptance'],
            'settled_through': program['settled_through'], 'recent_experiments': memories,
            'older_experiments_omitted': start,
            'history_endpoint': f'/api/v4/evolution/programs/{program["program_id"]}/experiments'}
    if program.get('research_source') is not None:
        seed['research_source'] = {**program['research_source'],
            'read_action':'read_research_source',
            'read_required':'Read the exact prior research pages before using them as grounding. Follow next_offset with source_digest; prior proposals are unverified.'}
    current_step = program['current']['step'] if program['current'] else program['queue'][0]
    # Checks already live in the frozen run payload; do not duplicate their
    # full text into the bounded continuation window.
    seed['current_experiment'] = {key: current_step[key] for key in ('kind', 'title')}
    seed['exploration_tools'] = {
        'tool': 'evolution_explore',
        'current_context': 'read_context reads the complete original objective, acceptance and '
                           'current hypothesis without model consumption; follow next_offset with '
                           'the exact source_digest. Read a referenced current_context_source fully.',
        'recall': 'Literal lookup is free. semantic_recall compares meaning across languages '
                  'using the current model and shared budget; follow {after,offset} cursors. '
                  'source_kind=deliverable, tool_evidence, artifact_evidence or background_evidence scans complete frozen originals '
                  'beyond receipt previews; keep that source kind while following cursors. '
                  'Repeated source/query pairs reuse a cached judgment. Read full receipts and reviews.',
        'source_read': 'evolution_source reads complete terminal originals named by result_source. '
                       'Supply program_id, settled sequence, run_id, task_id and exact source_sha256; '
                       'follow next_offset beyond previews. Originals are source reports, not new verification.',
        'propose': 'Add falsifiable problem/mechanism/scenario hypotheses before the final audit. '
                   'Cite evidence and label assumptions; keep the original objective unchanged.',
        'replan': 'During failure diagnosis, submit evidence-backed follow-ups. Independent '
                  'review must pass before admission; original failed checks remain required.',
    }
    if program['current'] is None and program['queue'][0]['kind'] == 'replan':
        seed['failure_diagnosis'] = {'failed_sequence': program['queue'][0]['recovery_of'],
            'read_required': 'Read the full archived receipt and review using evolution_explore.',
            'original_failed_step_retained': True}
    if program['current'] is None and program['queue'][0].get('hypothesis'):
        seed['current_hypothesis'] = program['queue'][0]['hypothesis']
    return _fit_context(seed)


def _remaining_budget(program):
    """Unknown consumption cannot authorize a fresh finite allowance."""
    budget = program['execution_options'].get('budget') or {}
    if budget.get('max_tokens') and program.get('actuals_complete') is False:
        raise EvolutionStateError('program token consumption is unknown')
    if budget.get('max_tool_calls') and program.get('tool_calls_complete',
            program.get('actuals_complete', True)) is False:
        raise EvolutionStateError('program tool consumption is unknown')
    remaining = {}
    for key in runs.default_budget():
        cap = budget.get(key) or 0
        started_at = program.get('started_at', program['created_at'])
        consumed = ((max(0, int(time.time()) - started_at) if started_at is not None else 0)
                    if key == 'max_wall_clock_seconds'
                    else program['actuals'][key.removeprefix('max_')])
        if cap and cap <= consumed:
            raise EvolutionStateError(f'program limit reached: {key}')
        remaining[key] = cap - consumed if cap else 0
    try:
        runs.validate_review_reserve({**program['execution_options'], 'budget': remaining})
    except EvolutionContractError as exc:
        raise EvolutionStateError(f'program review reserve unavailable: {exc}') from exc
    return remaining


def _step_requirements(program, step):
    """One final-audit requirement list serves launch and completion binding."""
    return program['acceptance'] + step['checks'] if step['kind'] == 'final' else step['checks']


def _launch_intent(program, user_id):
    step = program['queue'][0]
    sequence = program['settled_through'] + 1
    objective = (program['objective'] if step['kind'] == 'final' else
                 f'Complete this experiment: {step["title"]}\n{step["instruction"]}')
    if len(objective) > 4000:
        raise EvolutionContractError('experiment objective and checks exceed 4000 characters')
    options = deepcopy(program['execution_options'])
    if program.get('auto_replan', False) and 'max_attempts' not in options:
        # Bound repetitions of one technique, never the whole exploration.
        # Explicit unlimited attempts and already frozen requests remain exact.
        options['max_attempts'] = replanning.REPAIR_WINDOW
    budget = options.get('budget') or {}
    remaining = _remaining_budget(program)
    requirements = _step_requirements(program, step)
    payload = {**options, 'objective': objective, 'acceptance_requirements': requirements,
               'budget': remaining}
    if step['kind'] == 'final' and program.get('final_evidence_version') == 1:
        payload['require_requirement_originals'] = True
    _require_frozen_measurement_scope(step, step.get('scientific_protocol') or {})
    _check_validation_generation(program, step, step.get('scientific_protocol') or {}, step.get('scientific_required_outcome'), user_id=user_id)
    if step.get('requires_scientific_protocol') and not step.get('scientific_protocol'):
        raise EvolutionStateError('validation protocol was not preregistered before launch')
    if step['kind']=='synthesis':
        targets=[future for future in program['queue'][1:] if future.get('requires_scientific_protocol')]
        if targets:payload['validation_preregistration_target']={'program_id':program['program_id'],'target_step_sha256':validation_step_digest(targets[0])}
    if step.get('scientific_protocol'):
        payload['scientific_protocol']=deepcopy(step['scientific_protocol'])
        payload['require_command_execution']=True
    if step.get('scientific_required_outcome'):
        payload['scientific_required_outcome']=step['scientific_required_outcome']
    if step.get('require_command_execution'):
        payload['require_command_execution'] = True
    request_id = f'{program["program_id"]}:experiment:{sequence}'
    return {'sequence': sequence, 'step': deepcopy(step), 'payload': payload,
            'context_seed': _context(program, user_id),
            'deadline_at': program.get('started_at', program['created_at']) + budget['max_wall_clock_seconds']
                           if budget.get('max_wall_clock_seconds') else None,
            'run_id': 'run-' + hashlib.sha256(request_id.encode()).hexdigest()[:32]}


def _archive(program, run, user_id):
    intent = program['current']
    if intent['step'].get('required_observation_format') and run['status'] == 'completed':
        expected = intent['step'].get('scientific_protocol') or {}
        requirements = _step_requirements(program, intent['step'])
        if (expected.get('observation_format') != intent['step']['required_observation_format']
                or intent['payload'].get('scientific_protocol') != expected
                or run.get('scientific_protocol') != expected
                or run.get('scientific_required_outcome') != intent['step'].get('scientific_required_outcome')
                or intent['payload'].get('scientific_required_outcome') != intent['step'].get('scientific_required_outcome')
                or run.get('require_command_execution') is not True
                or intent['payload'].get('require_command_execution') is not True
                or run.get('acceptance_requirements') != requirements
                or intent['payload'].get('acceptance_requirements') != requirements):
            raise EvolutionStateError('validation acceptance binding changed; completion was not archived')
    if (program.get('final_evidence_version') == 1 and intent['step']['kind'] == 'final'
            and run['status'] == 'completed'):
        expected = _step_requirements(program, intent['step'])
        if (intent['payload'].get('require_requirement_originals') is not True
                or run.get('require_requirement_originals') is not True
                or intent['payload']['objective'] != program['objective']
                or run.get('objective') != program['objective']
                or intent['payload'].get('acceptance_requirements') != expected
                or run.get('acceptance_requirements') != expected):
            raise EvolutionStateError('final acceptance binding changed; completion was not archived')
    if run.get('exploration_hypothesis') is not None:
        from .hypothesis_review import validate_assessment
        if run['exploration_hypothesis'] != intent['step'].get('hypothesis'):
            raise EvolutionStateError('experiment hypothesis binding changed')
        if run.get('review') is not None:
            validate_assessment(run, run['review'])
    receipt = {'schema_version': 1, 'program_id': program['program_id'],
               'sequence': intent['sequence'], 'step': intent['step'],
               'run_id': run['run_id'], 'outcome': run['status'],
               'result': run['result'], 'review': run['review'],
               'actuals': run['actuals'], 'error': run['error'],
               'created_at': run['updated_at']}
    if run.get('result_source'):
        receipt['result_source'] = deepcopy(run['result_source'])
    if run.get('tool_evidence_source'):
        receipt['tool_evidence_source'] = deepcopy(run['tool_evidence_source'])
    if run.get('artifact_evidence_source'):
        receipt['artifact_evidence_source'] = deepcopy(run['artifact_evidence_source'])
    if run.get('background_evidence_source'):
        receipt['background_evidence_source'] = deepcopy(run['background_evidence_source'])
    key = _experiment_key(program['program_id'], intent['sequence'], user_id)
    previous = records.get_record(EXPERIMENTS, key)
    # Old committed receipts remain exact; a new pointer must also reproduce
    # identically if settlement committed its receipt before the header CAS.
    if (not previous or previous.get('result_source')) and not receipt.get('result_source') and run.get('domain') != 'research':
        executed = next((row for row in reversed(run.get('receipts', [])) if row['stage'] == 'executing'), None)
        if executed:
            from .receipt_sources import deliverable_reference
            source = deliverable_reference(receipt, user_id=user_id, execution_task_id=executed['task_id'])
            if source:
                receipt['result_source'] = source
    if run.get('actuals_complete') is False:
        receipt['actuals_complete'] = False
    proposal = replanning.proposal_for(program, run['run_id'])
    if proposal:
        receipt['replan'] = deepcopy(proposal)
    _validate(receipt, 'receipt')
    if previous:
        if previous != receipt:
            raise EvolutionStateError('experiment receipt is immutable')
        return receipt
    if receipt.get('result_source'):
        from .receipt_sources import read_original_page
        read_original_page(receipt, 'deliverable', user_id=user_id, limit=1)
    try:
        records.put_record(EXPERIMENTS, key, receipt, kind='experiment', expected_version=0)
    except StorageError as exc:
        if exc.code != 'database_conflict' or records.get_record(EXPERIMENTS, key) != receipt:
            raise
    return receipt


def _advance_cancelled(program, version, run, user_id):
    """Drain the cancelled run without launching or losing consumed stages."""
    if run is None:
        return program
    if run['status'] not in runs.TERMINAL:
        runs.cancel_run(run['run_id'], user_id=user_id)
    run = runs.advance_run(run['run_id'], user_id=user_id)
    current = program['current']
    accounting = current.get('cancellation_accounting') or {
        'actuals': {key: 0 for key in program['actuals']},
        'prior_complete': program.get('actuals_complete', True),
    }
    delta = {key: run['actuals'][key] - accounting['actuals'][key]
             for key in program['actuals']}
    if any(value < 0 for value in delta.values()):
        raise EvolutionStateError('cancelled run consumption cannot regress')
    program['actuals'] = {key: program['actuals'][key] + delta[key]
                         for key in program['actuals']}
    # A missing current-stage receipt is unsettled consumption, even when an
    # earlier stage is complete. A proved owner loss closes it as unknown.
    task_id = run.get('task_id')
    closed = bool(task_id and (
        any(row['task_id'] == task_id for row in run.get('receipts', []))
        or any(row['task_id'] == task_id and row['episode_id'] == run.get('episode_id')
               for row in run.get('stage_losses', []))))
    program['tool_calls_complete'] = (program.get('tool_calls_complete',
        program.get('actuals_complete', True)) and closed and not bool(run.get('stage_losses')))
    program['actuals_complete'] = (accounting['prior_complete'] and closed
                                   and run.get('actuals_complete', True))
    if closed:
        _archive(program, run, user_id)
        program['settled_through'] = current['sequence']
        program['current'] = None
        program.pop('replan_proposal', None)
    else:
        current['cancellation_accounting'] = {
            'actuals': dict(run['actuals']),
            'prior_complete': accounting['prior_complete'],
        }
    _save(program, version, user_id)
    return program


def advance_program(program_id, *, user_id):
    """At most one launch or settlement per call; CAS conflicts ask the caller to retry.

    A launch intent commits before submission. Retries use exactly its frozen
    request and context, including after ambiguous storage responses. A missing
    runtime task is reconciled by runs, never restarted here.
    """
    program, version = _read(program_id, user_id)
    current = program['current']
    if current is None:
        if program['status'] != 'active':
            return program
        if not program['queue']:
            raise EvolutionStateError('active program has no final audit')
        try:
            program['current'] = _launch_intent(program, user_id)
        except EvolutionStateError as exc:
            program.update(status='needs_attention', error=str(exc)[:1000])
            _save(program, version, user_id)
            return program
        _save(program, version, user_id)
        # The next call performs the frozen submission. No external effects
        # occur in the same call that claims the intent.
        return program

    run = runs.get_run(current['run_id'], user_id=user_id)
    if program['status'] == 'cancelled':
        return _advance_cancelled(program, version, run, user_id)
    if program['status'] not in ('active', 'paused'):
        return program
    if run is None:
        if program['status'] != 'active':
            return program
        # Re-read the fence immediately before entering the execution port.
        latest = get_program(program_id, user_id=user_id)
        if latest['status'] != 'active' or latest['current'] != current:
            return latest
        runs.submit_run(current['payload'], user_id=user_id,
            idempotency_key=f'{program_id}:experiment:{current["sequence"]}',
            context_seed=current['context_seed'], deadline_at=current['deadline_at'],
            **({'exploration_hypothesis': current['step']['hypothesis']}
               if current['step'].get('hypothesis') else {}))
        latest = get_program(program_id, user_id=user_id)
        if latest['status'] == 'cancelled':
            runs.cancel_run(current['run_id'], user_id=user_id)
        return latest
    run = runs.advance_run(run['run_id'], user_id=user_id)
    if run['status'] not in runs.TERMINAL:
        return get_program(program_id, user_id=user_id)
    # Cancellation receipts can still change usage; do not archive a mutable
    # cancelled run as immutable experiment evidence.
    if run['status'] == 'cancelled':
        program.update(status='needs_attention', error='experiment_cancelled; inspect execution')
        _save(program, version, user_id)
        return program
    _archive(program, run, user_id)
    program['settled_through'] = current['sequence']
    program['actuals'] = {key: program['actuals'][key] + run['actuals'][key]
                         for key in program['actuals']}
    program['tool_calls_complete'] = (program.get('tool_calls_complete',
        program.get('actuals_complete', True)) and not bool(run.get('stage_losses')))
    if run.get('actuals_complete') is False:
        program['actuals_complete'] = False
    scientific_failure_audit=None
    if (program.get('auto_replan',False) and current['step']['kind']!='replan'
            and run.get('error')=='scientific_outcome_requirement_unmet'):
        # Only owner-bound immutable terminal evidence can admit diagnosis.
        from .stage_outcomes import read_metadata
        executed=next((row for row in reversed(run.get('receipts',[])) if row['stage']=='executing'),None)
        outcome=read_metadata(run['run_id'],executed['task_id'],user_id=user_id) if executed else None
        if outcome and outcome.get('scientific_protocol')==run.get('scientific_protocol'):
            if outcome.get('background_evidence') and outcome.get('background_command_execution_observed'):
                scientific_failure_audit=outcome.get('background_scientific_audit')
            elif not outcome.get('pending_background_commands') and outcome.get('command_execution_observed'):
                scientific_failure_audit=outcome.get('scientific_audit')
    if run['status'] == 'completed':
        if current['step']['kind'] == 'replan':
            try:
                proposed = replanning.proposal_for(program, run['run_id'])
                failed_index = None
                if proposed and proposed.get('refresh_validation_generation'):
                    parent = _validation_generation_parent(program, proposed['failed_sequence'], user_id=user_id)
                    failed_index = _prepare_failed_candidate_index(program, parent, user_id=user_id)
                program['queue'] = replanning.admitted_queue(program, proposed, failed_index=failed_index)
                program['error'] = ''
            except EvolutionStateError as exc:
                program.update(status='needs_attention', error=str(exc))
        else:
            program['queue'].pop(0)
            if current['step']['kind'] == 'final':
                program.update(status='completed', error='')
    elif (program.get('auto_replan', False) and current['step']['kind'] != 'replan'
          and replanning.definitive_failure(run,scientific_audit=scientific_failure_audit)):
        if len(program['queue']) < 32:
            program['queue'].insert(0, replanning.diagnosis_step(current['sequence']))
            program['error'] = ''
        else:
            program.update(status='needs_attention', error='replan_frontier_full; failed evidence retained')
    else:
        program.update(status='needs_attention', error=run['error'] or
                       'experiment did not pass; inspect evidence before retrying')
    if program['status'] == 'active' and program.get('advance_mode') == 'checkpoint':
        # Archive/review settlement is independent of permission to admit the
        # next experiment. The durable pause survives reapers and restarts.
        program['status'] = 'paused'
    program['current'] = None
    program.pop('replan_proposal', None)
    _save(program, version, user_id)
    return program


def command_program(program_id, payload, *, user_id):
    _validate(payload, 'command')
    action = payload['action']
    if action == 'advance':
        return advance_program(program_id, user_id=user_id)
    program, version = _read(program_id, user_id)
    legacy_activation_ambiguous = ('started_at' not in program and (
        program['status'] != 'ready' or program['current'] is not None
        or program['settled_through'] > 0))
    if action == 'start' and (program.get('started_at') is not None or legacy_activation_ambiguous):
        # Initial activation is a one-time grant, not a checkpoint continuation.
        # Legacy paused states cannot prove nonactivation: require explicit resume
        # without changing their creation-time clock or frozen launch intent.
        return program
    if program['status'] in TERMINAL:
        if action == 'cancel' and program['status'] == 'cancelled':
            return advance_program(program_id, user_id=user_id)
        raise EvolutionStateError('exploration program is terminal')
    if action == 'set_pacing':
        program['advance_mode'] = payload['advance_mode']
    elif action == 'pause':
        if program['status'] not in ('active', 'ready', 'paused'):
            raise EvolutionStateError('program needs attention before it can pause')
        program['status'] = 'paused'
    elif action in ('resume', 'start'):
        if program['status'] not in ('ready', 'paused', 'active'):
            raise EvolutionStateError('inspect the failed experiment and use explicit retry')
        if program['current'] is None:
            _remaining_budget(program)
        _enroll(program_id, user_id)
        program['status'] = 'active'
        if 'started_at' in program and program['started_at'] is None:
            program['started_at'] = int(time.time())
    elif action == 'retry':
        if program['status'] != 'needs_attention' or program['current'] is not None:
            raise EvolutionStateError('retry requires a settled failed experiment')
        _remaining_budget(program)
        _enroll(program_id, user_id)
        program.update(status='active', error='')
    elif action == 'cancel':
        program['status'] = 'cancelled'
    _save(program, version, user_id)
    if action == 'start':
        advance_program(program_id, user_id=user_id)
        return advance_program(program_id, user_id=user_id)
    return advance_program(program_id, user_id=user_id) if action == 'cancel' else program


def _seal_expansion(program, user_id):
    """Seal the last header-committed expansion before any successor overwrites it."""
    marker = program.get('last_expansion')
    if not marker:
        return
    _validate(marker, 'expansion_receipt')
    key = records.entity_key(marker['expansion_id'], user_id, kind='program expansion')
    previous = records.get_record(EXPANSIONS, key)
    if previous == marker:
        return
    if previous:
        raise EvolutionStateError('expansion receipt is immutable')
    try:
        records.put_record(EXPANSIONS, key, marker, kind='program expansion', expected_version=0)
    except StorageError as exc:
        if exc.code != 'database_conflict' or records.get_record(EXPANSIONS, key) != marker:
            raise


def append_steps(program_id, payload, *, user_id, idempotency_key, executor_run_id=None):
    """Owner-directed expansion ahead of the final audit, without deleting history."""
    _validate(payload, 'append')
    _external_steps(payload['steps'])
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 160:
        raise EvolutionContractError('Idempotency-Key is required (1–160 characters)')
    program, version = _read(program_id, user_id)
    if executor_run_id is not None and (program['status'] != 'active'
            or not program['current'] or program['current']['run_id'] != executor_run_id):
        raise EvolutionStateError('executor frontier authority changed; reload before retrying')
    _seal_expansion(program, user_id)
    expansion_id = 'expansion-' + hashlib.sha256(
        f'{program_id}:{idempotency_key}'.encode()).hexdigest()[:32]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = records.get_record(EXPANSIONS,
        records.entity_key(expansion_id, user_id, kind='program expansion'))
    if previous:
        if previous['request_digest'] != digest:
            raise EvolutionStateError('Idempotency-Key belongs to another expansion')
        return program
    if program['status'] in TERMINAL or (program['current'] and
                                        program['current']['step']['kind'] == 'final'):
        raise EvolutionStateError('final audit is already launched or program is terminal')
    if any(step['kind'] == 'final' for step in payload['steps']):
        raise EvolutionContractError('the original final audit must remain last')
    if len(program['queue']) + len(payload['steps']) > 32:
        raise EvolutionStateError('frontier is full; settle experiments before expanding')
    program['queue'][-1:-1] = deepcopy(payload['steps'])
    program['last_expansion'] = {'expansion_id': expansion_id, 'request_digest': digest,
                                 'program_id': program_id}
    _save(program, version, user_id)
    _seal_expansion(program, user_id)
    return program


def propose_replan(program_id, payload, *, user_id, request_id, executor_run_id):
    """Stage a bounded plan for independent diagnosis review; launch nothing."""
    program, version = _read(program_id, user_id)
    current = program['current']
    if (program['status'] != 'active' or not current
            or current['run_id'] != executor_run_id or current['step']['kind'] != 'replan'):
        raise EvolutionStateError('replan requires the active diagnosis executor')
    _external_steps(payload['steps'])
    if any(step['kind'] == 'final' for step in payload['steps']):
        raise EvolutionContractError('replan cannot replace the original final audit')
    sequence = current['step']['recovery_of']
    receipt = experiment_page(program_id, user_id=user_id, after=sequence - 1, limit=1)['experiments'][0]
    if len(program['queue']) < 2 or program['queue'][1] != receipt['step']:
        raise EvolutionStateError('replan target changed; original failed checks must remain')
    if any(step['instruction'] == receipt['step']['instruction']
           and step.get('hypothesis') == receipt['step'].get('hypothesis')
           for step in payload['steps']):
        raise EvolutionStateError('replan repeats the failed technique; propose a discriminating change')
    proposal = {'failed_sequence': sequence, 'source_run_id': executor_run_id,
                'request_id': request_id, 'request_digest': replanning.request_digest(payload),
                **deepcopy(payload)}
    _validate(proposal, 'replan_proposal')
    previous = replanning.proposal_for(program, executor_run_id)
    if previous:
        if previous != proposal:
            raise EvolutionStateError('diagnosis already has a frozen replan proposal')
        return previous
    if proposal.get('refresh_validation_generation'):
        _validation_generation_parent(program, sequence, user_id=user_id)
    replanning.admitted_queue(program, proposal)
    program['replan_proposal'] = proposal
    _save(program, version, user_id)
    return proposal


def _registry_change(user_id, change):
    """One owner-scoped round-robin queue, independent of archived program count."""
    key = records.entity_key('programs', user_id, kind='program registry')
    for attempt in range(3):
        registry, version = records.get_versioned_record(REGISTRY, key)
        registry = registry or {'program_ids': [], 'cursor': 0}
        result = change(registry)
        _validate(registry, 'registry')
        try:
            records.put_record(REGISTRY, key, registry, kind='program registry',
                               expected_version=version)
            return result
        except StorageError as exc:
            if exc.code != 'database_conflict' or attempt == 2:
                raise


def _enroll(program_id, user_id):
    def add(registry):
        if program_id not in registry['program_ids']:
            if len(registry['program_ids']) >= 4096:
                raise EvolutionStateError('too many unfinished exploration programs')
            registry['program_ids'].append(program_id)
    _registry_change(user_id, add)


def _unenroll(program_id, user_id):
    def remove(registry):
        if program_id in registry['program_ids']:
            registry['program_ids'].remove(program_id)
            registry['cursor'] %= max(1, len(registry['program_ids']))
    _registry_change(user_id, remove)


def sweep_programs(*, user_id, batch_size=32):
    """Reaper-owned progression, fair across active programs, with no new timer.

    The durable queue survives boot and never enumerates historical programs.
    Each visit performs one frozen launch or settlement. A missed callback or
    transient failure remains enrolled for a later cadence.
    """
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 200:
        raise EvolutionContractError('sweep batch_size must be between 1 and 200')
    key = records.entity_key('programs', user_id, kind='program registry')
    existing = records.get_record(REGISTRY, key)
    if not existing or not existing['program_ids']:
        return []
    def select(registry):
        ids = registry['program_ids']
        count = min(batch_size, len(ids))
        start = registry['cursor'] % max(1, len(ids))
        selected = [ids[(start + index) % len(ids)] for index in range(count)]
        registry['cursor'] = (start + count) % max(1, len(ids))
        return selected
    selected = _registry_change(user_id, select)
    outcomes = []
    for program_id in selected:
        try:
            program = advance_program(program_id, user_id=user_id)
            if ((program['status'] in TERMINAL and program['current'] is None)
                    or program['status'] == 'needs_attention'
                    or (program['status'] == 'paused' and program['current'] is None)):
                _unenroll(program_id, user_id)
            outcomes.append({'program_id': program_id, 'status': program['status']})
        except Exception as exc:
            logger.warning('[Evolution:Programs] sweep deferred for %s: %s',
                           program_id, type(exc).__name__)
            outcomes.append({'program_id': program_id, 'status': 'deferred',
                             'error': type(exc).__name__})
    return outcomes


def _validation_generation_parent(program, sequence, *, user_id):
    """Verify the immutable numerical failure authorizing a candidate successor."""
    rows = experiment_page(program['program_id'], user_id=user_id, after=sequence-1, limit=1)['experiments']
    if not rows or rows[0]['sequence'] != sequence or sequence > program['settled_through']:
        raise EvolutionStateError('validation generation parent is unavailable')
    parent = rows[0];step = parent['step']
    run = runs.get_run(parent['run_id'], user_id=user_id)
    if (parent['outcome'] != 'failed' or parent['error'] != 'scientific_outcome_requirement_unmet'
            or not step.get('requires_frozen_measurement_scope') or run is None
            or run.get('scientific_protocol') != step.get('scientific_protocol')
            or run.get('scientific_required_outcome') != step.get('scientific_required_outcome')):
        raise EvolutionStateError('validation generation parent is not a confirmed scoped numerical failure')
    _require_frozen_measurement_scope(step, run['scientific_protocol'])
    from .stage_outcomes import read_metadata
    executed = next((row for row in reversed(run['receipts']) if row['stage'] == 'executing'), None)
    outcome = read_metadata(run['run_id'], executed['task_id'], user_id=user_id) if executed else None
    audit = None
    if outcome and outcome.get('scientific_protocol') == run['scientific_protocol']:
        if outcome.get('background_evidence') and outcome.get('background_command_execution_observed'):
            audit = outcome.get('background_scientific_audit')
        elif not outcome.get('pending_background_commands') and outcome.get('command_execution_observed'):
            audit = outcome.get('scientific_audit')
    if not replanning.definitive_failure(run, scientific_audit=audit):
        raise EvolutionStateError('validation generation parent lacks confirmed complete numerical evidence')
    return parent


def validation_generation_review_input(run, *, user_id):
    """Expose owned generation proposals and direct-parent reuse facts to review."""
    try:
        context = json.loads(run.get('context_seed') or '{}')
    except (ValueError, TypeError):
        return {}
    program_id = context.get('program_id') if isinstance(context, dict) else None
    if not program_id:
        return {}
    program = get_program(program_id, user_id=user_id)
    current = program.get('current')
    if not current or current['run_id'] != run['run_id']:
        return {}
    step = current['step']
    if step.get('validation_parent_sequence') is not None:
        if (run.get('scientific_protocol') != step.get('scientific_protocol')
                or run.get('scientific_required_outcome') != step.get('scientific_required_outcome')):
            raise EvolutionStateError('validation successor frozen protocol changed before review')
        parent = _validation_generation_parent(program, step['validation_parent_sequence'], user_id=user_id)
        from .tool_evidence import normalize_scientific_protocol
        prior_scope = normalize_scientific_protocol(parent['step']['scientific_protocol'])['measurement_scope']
        current_scope = normalize_scientific_protocol(run['scientific_protocol'])['measurement_scope']
        return {'validation_feedback_reuse': {
            'program_id': program_id, 'parent_sequence': parent['sequence'], 'parent_run_id': parent['run_id'],
            'parent_candidate_digest': prior_scope['candidate_digest'],
            'current_candidate_digest': current_scope['candidate_digest'],
            'same_dataset_digest': prior_scope['dataset_digest'] == current_scope['dataset_digest'],
            'same_split_digest': prior_scope['split_digest'] == current_scope['split_digest'],
            'parent_sources': {key:deepcopy(parent[key]) for key in
                ('result_source','tool_evidence_source','artifact_evidence_source','background_evidence_source')
                if parent.get(key)},
            'evidence_status': 'server-linked prior numerical failure and declared input identities; '
                'same digests do not establish actual data use, untouched holdout or statistical validity'}}
    if step['kind'] != 'replan':
        return {}
    proposal = replanning.proposal_for(program, run['run_id'])
    if not proposal or not proposal.get('refresh_validation_generation'):
        return {}
    parent = _validation_generation_parent(program, proposal['failed_sequence'], user_id=user_id)
    return {'candidate_generation_refresh': {'proposal': deepcopy(proposal),
            'preserved_validation': deepcopy(parent['step']),
            'evidence_status': 'untrusted proposed changes; inspect complete plan, preserve all checks and controls'}}


def _check_validation_generation(program, step, protocol, required_outcome, *, user_id):
    sequence = step.get('validation_parent_sequence')
    if sequence is None:
        return
    parent = _validation_generation_parent(program, sequence, user_id=user_id)
    from .validation_candidate_index import template
    if template(step) != template(parent['step']):
        raise EvolutionStateError('validation successor changed its original checks or policy')
    from .tool_evidence import normalize_scientific_protocol
    original = normalize_scientific_protocol(parent['step']['scientific_protocol'])
    candidate = protocol['measurement_scope']['candidate_digest']
    if candidate == original['measurement_scope']['candidate_digest']:
        raise EvolutionStateError('validation successor requires a changed candidate generation')
    comparable = deepcopy(protocol)
    comparable['measurement_scope']['candidate_digest'] = original['measurement_scope']['candidate_digest']
    if (required_outcome != parent['step']['scientific_required_outcome']
            or comparable != original):
        raise EvolutionStateError('validation successor may change only candidate_digest; original protocol and outcome remain fixed')
    reference = step.get('failed_candidate_index')
    if reference is not None:
        from .validation_candidate_index import FailedCandidates
        index = FailedCandidates(records._storage(), user_id=user_id,
            program_id=program['program_id'], step=parent['step'])
        if not index.contains(reference, original['measurement_scope']['candidate_digest']):
            raise StorageError('database_integrity', 'Failed candidate index omits its confirmed parent')
        if index.contains(reference, candidate):
            raise EvolutionStateError('validation candidate previously failed under these frozen controls')
        return
    for previous_candidate in _validation_generation_ancestors(program, parent, original,
            required_outcome, user_id=user_id):
        if candidate == previous_candidate:
            raise EvolutionStateError('validation candidate previously failed under these frozen controls')


def _validation_generation_ancestors(program, parent, original, required_outcome, *, user_id):
    from .validation_candidate_index import template
    from .tool_evidence import normalize_scientific_protocol
    ancestor = parent
    while ancestor['step'].get('validation_parent_sequence') is not None:
        previous_sequence = ancestor['step']['validation_parent_sequence']
        if type(previous_sequence) is not int or not 0 < previous_sequence < ancestor['sequence']:
            raise EvolutionStateError('validation candidate lineage is cyclic or out of order')
        rows = experiment_page(program['program_id'], user_id=user_id,
                               after=previous_sequence-1, limit=1)['experiments']
        if (not rows or rows[0]['sequence'] != previous_sequence or rows[0]['outcome'] != 'failed'
                or rows[0]['error'] != 'scientific_outcome_requirement_unmet'):
            raise EvolutionStateError('validation candidate lineage is unavailable or unconfirmed')
        ancestor = rows[0]
        _require_frozen_measurement_scope(ancestor['step'], ancestor['step'].get('scientific_protocol') or {})
        previous_protocol = normalize_scientific_protocol(ancestor['step']['scientific_protocol'])
        previous_candidate = previous_protocol['measurement_scope']['candidate_digest']
        previous_protocol['measurement_scope']['candidate_digest'] = original['measurement_scope']['candidate_digest']
        if (template(ancestor['step']) != template(parent['step']) or previous_protocol != original
                or ancestor['step']['scientific_required_outcome'] != required_outcome):
            raise EvolutionStateError('validation candidate lineage changed its fixed controls')
        yield previous_candidate


def _prepare_failed_candidate_index(program, parent, *, user_id):
    from .validation_candidate_index import FailedCandidates, FAILED
    from lib.research.source_index import empty_reference
    from .tool_evidence import normalize_scientific_protocol
    index = FailedCandidates(records._storage(write=True), user_id=user_id,
        program_id=program['program_id'], step=parent['step'])
    protocol = normalize_scientific_protocol(parent['step']['scientific_protocol'])
    reference = parent['step'].get('failed_candidate_index')
    if reference is None:
        reference = empty_reference()
        for candidate in _validation_generation_ancestors(program, parent, protocol,
                parent['step']['scientific_required_outcome'], user_id=user_id):
            reference = index.insert(reference, candidate, FAILED)
    else:
        # Validate an existing root even if insertion could take another trie path.
        index.contains(reference, protocol['measurement_scope']['candidate_digest'])
    return index.insert(reference, protocol['measurement_scope']['candidate_digest'], FAILED)


def validation_step_digest(step):
    """Identity excludes the immutable registration, never the target checks."""
    base={key:value for key,value in step.items() if key not in ('scientific_protocol','scientific_required_outcome')}
    return hashlib.sha256(json.dumps(base,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def pending_validations(program):
    return [{'title':step['title'],'target_step_sha256':validation_step_digest(step),
        'registered':bool(step.get('scientific_protocol')),
        **({'required_observation_format':step['required_observation_format']} if step.get('required_observation_format') else {}),
        **({'validation_parent_sequence':step['validation_parent_sequence']} if step.get('validation_parent_sequence') else {}),
        **({'requires_frozen_measurement_scope': True,
            'required_measurement_scope_fields': _required_measurement_scope_fields(step)}
           if step.get('requires_frozen_measurement_scope') else {})} for step in program['queue'][1:]
        if step.get('requires_scientific_protocol')]


def preregister_validation(program_id, target_digest, protocol, required_outcome, *, user_id, executor_run_id):
    """Freeze one future validation under program CAS; dispatch nothing."""
    from .tool_evidence import normalize_scientific_protocol
    protocol=normalize_scientific_protocol(protocol)
    program,version=_read(program_id,user_id)
    current=program.get('current')
    if program['status']!='active' or not current or current['run_id']!=executor_run_id:
        raise EvolutionStateError('preregistration requires the active program executor')
    matches=[step for step in program['queue'][1:] if step.get('requires_scientific_protocol') and validation_step_digest(step)==target_digest]
    if len(matches)!=1:raise EvolutionStateError('future validation target is missing or ambiguous')
    step=matches[0]
    if step.get('required_observation_format'):
        protocol = {**protocol, 'observation_format': step['required_observation_format']}
    _require_frozen_measurement_scope(step, protocol)
    _check_validation_generation(program, step, protocol, required_outcome, user_id=user_id)
    if step.get('scientific_protocol'):
        if step['scientific_protocol']!=protocol or step.get('scientific_required_outcome')!=required_outcome:
            raise EvolutionStateError('validation protocol is already frozen; propose a new generation instead')
    else:
        step['scientific_protocol']=deepcopy(protocol);step['scientific_required_outcome']=required_outcome
        _save(program,version,user_id)
    return {'target_step_sha256':target_digest,'scientific_protocol':deepcopy(step['scientific_protocol']),
        'scientific_required_outcome':step['scientific_required_outcome'],
        'evidence_status':'precommitted plan, not measurement or verification'}


def validation_preregistered(target, run_id, *, user_id):
    program=get_program(target['program_id'],user_id=user_id)
    if not program.get('current') or program['current']['run_id']!=run_id:return False
    matches=[step for step in program['queue'][1:] if step.get('requires_scientific_protocol') and validation_step_digest(step)==target['target_step_sha256']]
    if len(matches) != 1 or not matches[0].get('scientific_protocol') or not matches[0].get('scientific_required_outcome'):
        return False
    try:
        _require_frozen_measurement_scope(matches[0], matches[0]['scientific_protocol'])
        _check_validation_generation(program, matches[0], matches[0]['scientific_protocol'], matches[0]['scientific_required_outcome'], user_id=user_id)
    except EvolutionStateError:
        return False
    return True
