"""Task-authorized experimental frontier writes and bounded archive retrieval.

Programs own durability and runs own execution. This adapter authorizes the
current executor against both records; model-supplied IDs convey no authority.
Problem, technique and scenario hypotheses are proposed inputs, never verdicts.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from . import programs
from .contracts import EvolutionContractError, EvolutionStateError, load_schema, validate_program
from .coordination import authorize_task

SCAN_WINDOW = 50
HIT_LIMIT = 8
CHUNK_CHARS = 6000
HYPOTHESIS_CHECKS = [
    'Problem and audience claims trace to inspected incidents or sources: who is affected, '
    'in which situation, what current workaround fails and its observed cost. '
    'Problem selection examines alternative explanations and low-effort nontechnical '
    'workarounds, distinguishes causes from symptoms and states an observation that '
    'would refute the need. Inferred demand and missing support remain unverified.',
    'Mechanism or scenario comparisons use a reproducible protocol, user-relevant metric '
    'and stated falsifier. Compare enabling change necessity against competent present '
    'alternatives at equal information, actions and cost; assuming a prerequisite cannot '
    'prove its availability or necessity. Conditional future protocols name enabling '
    'conditions, missing prerequisites and a discriminating test; acceptance of a protocol '
    'does not verify efficacy. Separate measured results from untested claims.',
    'The conclusion separates technical feasibility, novelty claims and measured consumer '
    'value, accounting for adoption effort and operating costs where relevant. '
    'A prototype or benchmark alone cannot verify demand or net user benefit; '
    'retain falsified claims and unresolved assumptions.',
]


def tool_definition():
    """Expose one source-owned schema without wire-local reference resolution."""
    defs = load_schema(programs.SCHEMA)['$defs']
    parameters = deepcopy(defs['explore_request'])
    # Shared wire admission requires locally declared required properties;
    # execute() owns validation against the full conditional source schema.
    parameters.pop('allOf', None)
    step = deepcopy(defs['proposal_step'])
    step['properties']['hypothesis'] = deepcopy(
        load_schema('evolution_run_v1.schema.json')['$defs']['exploration_hypothesis'])
    parameters['properties']['step'] = step
    parameters['properties']['steps']['items'] = deepcopy(step)
    research=load_schema('research_program_v1.schema.json')['$defs']
    def inline(value):
        if isinstance(value,dict):
            if '$ref' in value and value['$ref'].startswith('#/$defs/'):
                return inline(research[value['$ref'].rsplit('/',1)[1]])
            return {key:inline(item) for key,item in value.items()}
        if isinstance(value,list):return [inline(item) for item in value]
        return value
    protocol=inline(research['protocol'])
    protocol['required']=list(protocol['required'])+['decision_rule']
    protocol['properties']['random_seeds'].update(minItems=2,uniqueItems=True)
    parameters['properties']['scientific_protocol']=protocol
    parameters['properties']['scientific_required_outcome']=deepcopy(load_schema('evolution_run_v1.schema.json')['$defs']['scientific_required_outcome'])
    return {'type': 'function', 'function': {
        'name': 'evolution_explore',
        'description': 'For the current exploration program executor: recall archived '
            'experiments (optional literal query, bounded scan with next_cursor); read a '
            'full receipt by sequence and offset; read_context pages the complete current '
            'original objective, acceptance and hypothesis with source_digest and offset. '
            'Follow next_offset with that digest; a current_context_source must be read fully. '
            'read_research_source reads the programme-bound original discovery proposal, survey and gap map; follow next_offset with its source_digest. '
            'Reading consumes no model request and does not verify any claim. '
            'Use validation_context to list future validation identities and their required_measurement_scope_fields; inspect real inputs/configurations to compute missing identities, never invent digests; preregister freezes a complete scientific_protocol and required outcome for one target_step_sha256 before its executor launches. The protocol cannot be changed afterward; registration grants no verification. '
            'Propose a new non-final experiment '
            'with structured hypothesis (real problem, audience, evidence_refs, baseline, '
            'new scenario, technical mechanism, expected_value, assumptions, falsifier). '
            'Proposals extend the authorized frontier before its original final audit. '
            'They are unverified hypotheses and never change the original objective. '
            'During a failure diagnosis use action=replan with reason and steps; for a confirmed scoped numerical failure, refresh_validation_generation=true requests a reviewed candidate-only successor, with a final synthesis follow-up to freeze its new candidate digest. Other protocol conditions and required outcome remain unchanged; '
            'follow-ups wait for independent review and preserve the failed checks. '
            'semantic_recall judges complete archive chunks across paraphrases/languages under '
            'your current model and shared budget; exact quoted sources remain unverified. '
            'action=read can select source_kind for exact original pages; semantic_recall with source_kind=receipt (default) scans receipt JSON; deliverable, tool_evidence, artifact_evidence or background_evidence '
            'scans complete frozen originals beyond inline previews. Keep source_kind when '
            'following cursors. Missing references are reported, never replaced with previews. '
            'Follow its next_cursor {after,offset} to cover older/long receipts; repeated '
            'queries reuse cached judgments. Use read for full receipts and reviews; '
            'result_source names a larger executor original. Read that through '
            'evolution_source with program_id, sequence, run_id and its exact digest.',
        'parameters': parameters,
    }}


def _bound_program(task, program_id, *, allow_reviewer=False):
    run, role, _state = authorize_task(task)
    if role != 'executing' and not (allow_reviewer and role == 'reviewing'):
        raise EvolutionStateError('exploration requires the current executor')
    program = programs.get_program(program_id, user_id=task['_userId'])
    current = program['current']
    if (program['status'] not in ('active', 'paused') or not current
            or current['run_id'] != run['run_id']):
        raise EvolutionStateError('task is not the current experiment of this program')
    return program


def _recall(program, args, user_id):
    after = args.get('after', 0)
    query = args.get('query', '').strip().casefold()
    page = programs.experiment_page(program['program_id'], user_id=user_id,
                                    after=after, limit=SCAN_WINDOW)
    hits = []
    through = min(after, program['settled_through'])
    for receipt in page['experiments']:
        through = receipt['sequence']
        searchable = json.dumps({'step': receipt['step'], 'result': receipt['result'],
                                'review': receipt['review'], 'error': receipt['error'],
                                'replan': receipt.get('replan')},
                               ensure_ascii=False)
        index = searchable.casefold().find(query) if query else 0
        if index < 0:
            continue
        start = max(0, index - 80)
        hits.append({'sequence': through, 'title': receipt['step']['title'],
                     'outcome': receipt['outcome'], 'run_id': receipt['run_id'],
                     'excerpt': searchable[start:start + 500], 'detail_required': True})
        if len(hits) == HIT_LIMIT:
            break
    return {'experiments': hits, 'scanned_through': through,
            'settled_through': program['settled_through'],
            'next_cursor': through if through < program['settled_through'] else None,
            'query': query, 'evidence_status': 'archived source reports; inspect reviews and tool evidence'}


def _read(program, args, user_id):
    sequence = args.get('sequence')
    if sequence is None or sequence > program['settled_through']:
        raise EvolutionContractError('read requires a settled experiment sequence')
    rows = programs.experiment_page(program['program_id'], user_id=user_id,
                                      after=sequence - 1, limit=1)['experiments']
    if not rows or rows[0]['sequence']!=sequence:
        raise EvolutionStateError('requested experiment receipt is unavailable')
    receipt=rows[0]
    kind = args.get('source_kind', 'receipt')
    if kind != 'receipt':
        from .receipt_sources import read_original_page
        result = read_original_page(receipt, kind, user_id=user_id,
            offset=args.get('offset', 0), limit=CHUNK_CHARS, source_digest=args.get('source_digest'))
        return {**result, 'sequence': sequence, 'source_digest': result['source_sha256'],
                'evidence_status': 'exact archived original; source access grants no verification or replay'}
    from .semantic_memory import receipt_text, _digest
    digest=_digest(receipt)
    if args.get('source_digest') is not None and args['source_digest']!=digest:
        raise EvolutionStateError('experiment receipt source identity changed')
    encoded = receipt_text(receipt)
    offset = args.get('offset', 0)
    if offset > len(encoded):
        raise EvolutionContractError('offset is beyond the receipt')
    end = min(len(encoded), offset + CHUNK_CHARS)
    return {'sequence': sequence, 'offset': offset, 'total_chars': len(encoded),
            'source_digest': digest,
            'content': encoded[offset:end], 'next_offset': end if end < len(encoded) else None,
            'evidence_status': 'archived source reports; claims are not new verification'}


def _read_context(program, args):
    from .semantic_memory import receipt_text, _digest
    source = programs.continuation_source(program)
    encoded, digest = receipt_text(source), _digest(source)
    if args.get('source_digest') is not None and args['source_digest'] != digest:
        raise EvolutionStateError('current context source identity changed')
    offset = args.get('offset', 0)
    if offset > len(encoded):
        raise EvolutionContractError('offset is beyond the current context source')
    end = min(len(encoded), offset + CHUNK_CHARS)
    return {'offset': offset, 'total_chars': len(encoded), 'source_digest': digest,
            'content': encoded[offset:end], 'next_offset': end if end < len(encoded) else None,
            'evidence_status': 'Exact original mission and current hypothesis; hypotheses '
                               'and supporting references remain unverified claims.'}


def _deliver_read(task, program, result):
    """Recheck current executor/program after I/O, before exposing evidence."""
    _bound_program(task, program['program_id'])
    return result


def execute(task, args, *, call_id):
    """Execute a validated request under owner, current-run and stage fences."""
    validate_program(args, 'explore_request')
    program = _bound_program(task, args['program_id'])
    owner = task['_userId']
    if args['action'] == 'read_research_source':
        return _deliver_read(task,program,programs.read_research_source(program,user_id=owner,
            source_digest=args.get('source_digest'),offset=args.get('offset',0)))
    if args['action']=='validation_context':
        return _deliver_read(task, program, {'program_id':program['program_id'],
                                            'targets':programs.pending_validations(program)})
    if args['action'] == 'read_context':
        return _deliver_read(task, program, _read_context(program, args))
    if args['action'] == 'recall':
        return _deliver_read(task, program, _recall(program, args, owner))
    if args['action'] == 'semantic_recall':
        from .semantic_memory import recall
        return _deliver_read(task, program, recall(task, program, args))
    if args['action'] == 'read':
        return _deliver_read(task, program, _read(program, args, owner))
    if program['status'] != 'active':
        raise EvolutionStateError('paused exploration cannot admit executor proposals')
    if not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 160:
        raise EvolutionContractError('a stable tool-call identity is required')
    key = 'task-proposal-' + hashlib.sha256(
        f'{task["id"] if task.get("id") else task["task_id"]}:{call_id}'.encode()).hexdigest()
    if args['action']=='preregister':
        return programs.preregister_validation(program['program_id'],args['target_step_sha256'],
            args['scientific_protocol'],args['scientific_required_outcome'],user_id=owner,executor_run_id=program['current']['run_id'])
    if args['action'] == 'replan':
        if not args.get('reason') or not args.get('steps'):
            raise EvolutionContractError('replan requires a diagnosis reason and falsifiable steps')
        steps = deepcopy(args['steps'])
        for proposed in steps:
            proposed['checks'].extend(HYPOTHESIS_CHECKS)
        proposal = programs.propose_replan(program['program_id'],
            {'reason': args['reason'], 'steps': steps,
             **({'refresh_validation_generation':True} if args.get('refresh_validation_generation') else {})}, user_id=owner,
            request_id=key, executor_run_id=program['current']['run_id'])
        return {'failed_sequence': proposal['failed_sequence'], 'reason': proposal['reason'],
                **({'refresh_validation_generation':True} if proposal.get('refresh_validation_generation') else {}),
                'steps': proposal['steps'], 'evidence_status':
                'unverified plan; follows independent diagnosis review; original failed checks retained'}
    if program['current']['step']['kind'] == 'replan':
        raise EvolutionStateError('diagnosis follow-ups require action=replan and independent review')
    step = args.get('step')
    if not step or step['kind'] == 'final' or not step.get('hypothesis'):
        raise EvolutionContractError('propose requires a non-final step with a structured hypothesis')
    if len(step['checks']) > 13:
        raise EvolutionContractError('proposals allow at most 13 checks plus three required hypothesis checks')
    step = deepcopy(step)
    step['checks'].extend(HYPOTHESIS_CHECKS)
    # No immediate launch: the ordinary reaper spends only the existing program
    # authorization and remaining budget. Header CAS races fail visibly.
    updated = programs.append_steps(program['program_id'], {'steps': [step]},
                                    user_id=owner, idempotency_key=key,
                                    executor_run_id=program['current']['run_id'])
    return {'program_id': updated['program_id'], 'pending_steps': len(updated['queue']),
            'title': step['title'], 'evidence_status': 'unverified hypothesis queued for independent execution and review'}


def handle_explore(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                   cfg, project_path, project_enabled, all_tools=None):
    """Use the shared evolution display envelope and make refusals observable."""
    from .tool_handler import _entry
    try:
        result = execute(task, fn_args or {}, call_id=tc_id)
    except (EvolutionContractError, EvolutionStateError, ValueError, TypeError) as exc:
        return [_entry(fn_name, 'exploration refused', str(exc)[:400], fetched=False)]
    return [_entry(fn_name, 'exploration ' + fn_args['action'],
                   json.dumps(result, ensure_ascii=False))]
