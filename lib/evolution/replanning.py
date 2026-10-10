"""Evidence-gated recovery policy for durable exploration programs.

Programs own the CAS and ordinary runs own execution/review. This module only
classifies settled evidence, describes a diagnosis and validates its proposed
frontier. No executor, storage, timer or learning authority lives here.
"""
from copy import deepcopy
import hashlib
import json

from .contracts import EvolutionStateError

REPAIR_WINDOW = 3


def definitive_failure(run, *, scientific_audit=None):
    """Unknown evidence and infrastructure errors never authorize auto-replay."""
    if run['status']=='failed' and run.get('error')=='validation_preregistration_missing' and run.get('validation_preregistration_target'):
        return True  # Confirmed missing plan, not an ambiguous external effect.
    if run['status']=='failed' and run.get('error')=='scientific_outcome_requirement_unmet':
        from .tool_evidence import scientific_audit_valid,scientific_outcome_met,scientific_protocol_digest
        protocol=run.get('scientific_protocol')
        required=run.get('scientific_required_outcome')
        return bool(protocol and required and scientific_audit_valid(scientific_audit)
            and scientific_audit['protocol_sha256']==scientific_protocol_digest(protocol)
            and not scientific_outcome_met(scientific_audit,required))
    review = run.get('review') or {}
    results = review.get('criterion_results', []) + review.get('requirement_results', [])
    return (run['status'] in ('failed', 'needs_attention') and not run['error']
            and review.get('outcome') in ('failure', 'partial') and bool(results)
            and all(item.get('outcome') in ('pass', 'fail') for item in results)
            and any(item['outcome'] == 'fail' for item in results))


def diagnosis_step(sequence):
    return {'kind': 'replan', 'recovery_of': sequence,
            'title': f'Diagnose experiment {sequence} and design alternatives',
            'instruction': f'Read the complete archived experiment {sequence} with '
                'evolution_explore action=read, following every next_offset with the exact source_digest. Inspect '
                'its frozen protocol, recorded failure and available independent review. Read complete command originals with action=read source_kind=tool_evidence (and background_evidence when present), following next_offset and source_digest; summaries cannot replace measurements. Diagnose the causal '
                'failure; distinguish evidence from assumptions. Compare competing '
                'technical mechanisms and application scenarios against the baseline. '
                'Submit action=replan with a reason and falsifiable follow-up steps. '
                'Prefer the cheapest experiment that discriminates between alternatives. '
                'The original failed checks and final objective audit remain required; retain the failed experiment as evidence. '
                'For a confirmed numerical target miss with frozen candidate/baseline generations, '
                'request refresh_validation_generation=true when the candidate must change, and end your follow-up steps with synthesis. '
                'After approval, that synthesis must use validation_context and preregister the successor with a new candidate_digest; '
                'keep the baseline, data, budget, thresholds, seeds and required outcome unchanged. '
                'Do not register the old candidate again or invent identities; inspect actual files/configuration first. '
                'The proposed plan takes effect only after this independent '
                'review passes. Do not repeat ambiguous external effects.',
            'checks': ['The diagnosis cites inspected failure evidence and separates causal hypotheses from observations.',
                       'A replan proposal was submitted; alternatives have a baseline, falsifier and explicit assumptions.',
                       'Follow-ups address the failure without weakening the original objective or checks.']}


def proposal_for(program, run_id):
    proposal = program.get('replan_proposal')
    return proposal if proposal and proposal['source_run_id'] == run_id else None


def request_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def admitted_queue(program, proposal, *, failed_index=None):
    """Insert reviewed alternatives; explicit refresh keeps checks on a new candidate basis."""
    if not proposal:
        raise EvolutionStateError('replan_proposal_missing; diagnosis must submit a plan')
    current = program['current']
    if (not current or current['step'].get('recovery_of') != proposal['failed_sequence']
            or proposal['source_run_id'] != current['run_id']):
        raise EvolutionStateError('replan_proposal_stale; inspect the diagnosis')
    queue = deepcopy(program['queue'][1:])
    if not queue:
        raise EvolutionStateError('replan_target_missing; original checks must remain')
    if proposal.get('refresh_validation_generation'):
        original = queue[0]
        if (original['kind'] != 'experiment' or not original.get('requires_frozen_measurement_scope')
                or not original.get('scientific_protocol') or not original.get('scientific_required_outcome')):
            raise EvolutionStateError('generation refresh requires an already frozen scoped validation')
        if not proposal['steps'] or proposal['steps'][-1]['kind'] != 'synthesis':
            raise EvolutionStateError('generation refresh requires a final synthesis follow-up to preregister validation')
        successor = deepcopy(original)
        successor.pop('scientific_protocol');successor.pop('scientific_required_outcome')
        successor.pop('failed_candidate_index', None)
        successor['validation_parent_sequence'] = proposal['failed_sequence']
        if failed_index is not None:
            successor['failed_candidate_index'] = deepcopy(failed_index)
        queue[0] = successor
    if len(queue) + len(proposal['steps']) > 32:
        raise EvolutionStateError('replan_frontier_full; settle or reduce follow-ups')
    return deepcopy(proposal['steps']) + queue
