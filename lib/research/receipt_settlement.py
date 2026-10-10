"""Verified terminal settlement for immutable paged Research observations.

Failure validation and success projection bind one prior pending run and its
candidate terminal result to owner/scope/revision. The workspace CAS alone
publishes the conclusion after full-chain revalidation on its selected authority.
Success recomputes reported scientific consistency; neither path grants job
replay, data authenticity or external-process quiescence.
Dependencies: receipt_pages, execution_observation, program; no model/tool dispatch.
"""
from dataclasses import dataclass

from .receipt_pages import ReceiptPages, digest
from .execution_observation import iter_execution_states, reduce_execution_state
from .program import normalize_run_evidence, dependency_digests


def run_from(workspace, run_id):
    return next((run for run in normalize_run_evidence(workspace.get('runs')) if run['id'] == run_id), None)


@dataclass(frozen=True)
class ReceiptFailureSettlement:
    user_id: int
    paper_hash: str
    lang: str
    run_id: str
    revision: int
    prior_digest: str
    source_digest: str

    def validate(self, client, candidate, prior, *, user_id, paper_hash, lang, revision):
        if (type(self.revision) is not int or self.revision < 0
                or (self.user_id, self.paper_hash, self.lang, self.revision) != (user_id, paper_hash, lang, revision)):
            raise ValueError('Research failure settlement binding or revision changed')
        old = run_from(prior, self.run_id)
        source = run_from(candidate, self.run_id)
        if old is None or source is None or digest(old) != self.prior_digest or digest(source) != self.source_digest:
            raise ValueError('Research failure settlement observations changed')
        reference = old.get('receipt_history')
        if (old['execution_status'] not in {'planned', 'queued', 'running'}
                or not reference or not reference['head'] or source.get('receipt_history') != reference
                or source['execution_status'] != 'failed' or source['status'] != 'failed'
                or source['scientific_outcome'] != 'inconclusive'
                or source['artifact_ref'] or source['artifact_refs']):
            raise ValueError('Research failure settlement requires a pending paged basis and failed result')
        dependencies = dependency_digests(candidate)
        if (source['tool_receipts'][:len(old['tool_receipts'])] != old['tool_receipts']
                or any(source[key] != old[key] or source[key] != value for key, value in dependencies.items())):
            raise ValueError('Research failure settlement cannot rewrite its scientific basis or receipts')
        repository = ReceiptPages(client, user_id=user_id, paper_hash=paper_hash, lang=lang,
            run_id=self.run_id, hypothesis_digest=old['hypothesis_digest'], protocol_digest=old['protocol_digest'])
        with repository.open_receipts(reference, source['tool_receipts']) as receipts:
            state = reduce_execution_state(iter_execution_states(receipts, candidate.get('capability_bindings', [])))
        if state != 'failed':
            raise ValueError('Research failure settlement requires complete failed job observations')
        return self.run_id


def prepare_failure_settlement(client, candidate, prior, *, user_id, paper_hash, lang, run_id, revision):
    if type(revision) is not int or revision < 0:
        raise ValueError('Invalid Research failure settlement revision')
    old = run_from(prior, run_id); source = run_from(candidate, run_id)
    if old is None or source is None:
        raise ValueError('Research failure settlement run was not found')
    result = ReceiptFailureSettlement(user_id, paper_hash, lang, run_id, revision, digest(old), digest(source))
    result.validate(client, candidate, prior, user_id=user_id, paper_hash=paper_hash, lang=lang, revision=revision)
    return result


@dataclass(frozen=True)
class ReceiptSuccessSettlement:
    user_id: int
    paper_hash: str
    lang: str
    run_id: str
    revision: int
    prior_digest: str
    source_digest: str

    def project(self, client, candidate, prior, *, user_id, paper_hash, lang, revision):
        from copy import deepcopy
        from .execution_observation import ExecutionStateReducer
        from .science_reduction import ScientificReceiptReducer, receipt_evidence_digest
        if (type(self.revision) is not int or self.revision < 0
                or (self.user_id, self.paper_hash, self.lang, self.revision) != (user_id, paper_hash, lang, revision)):
            raise ValueError('Research success settlement binding or revision changed')
        old = run_from(prior, self.run_id); source = run_from(candidate, self.run_id)
        if old is None or source is None or digest(old) != self.prior_digest or digest(source) != self.source_digest:
            raise ValueError('Research success settlement observations changed')
        reference = old.get('receipt_history')
        if (old['execution_status'] not in {'planned', 'queued', 'running'}
                or not reference or not reference['head'] or source.get('receipt_history') != reference
                or source['execution_status'] != 'completed' or source.get('receipt_evidence') is not None):
            raise ValueError('Research success settlement requires a pending paged basis and completed result')
        dependencies = dependency_digests(candidate)
        if (source['tool_receipts'][:len(old['tool_receipts'])] != old['tool_receipts']
                or any(source[key] != old[key] or source[key] != value for key, value in dependencies.items())):
            raise ValueError('Research success settlement cannot rewrite its scientific basis or receipts')
        repository = ReceiptPages(client, user_id=user_id, paper_hash=paper_hash, lang=lang,
            run_id=self.run_id, hypothesis_digest=old['hypothesis_digest'], protocol_digest=old['protocol_digest'])
        execution = ExecutionStateReducer()
        refs = list(dict.fromkeys([source['artifact_ref'], *source['artifact_refs']]))
        refs = [ref for ref in refs if ref]
        with repository.open_receipts(reference, source['tool_receipts']) as receipts:
            for receipt, state in iter_execution_states(receipts, candidate.get('capability_bindings', [])):
                execution.observe(receipt, state)
                ref = receipt.get('artifact_ref')
                if state == 'completed' and receipt.get('status') == 'done' and ref and ref not in refs and len(refs) < 32:
                    refs.append(ref)
        if execution.status != 'completed':
            raise ValueError('Research success settlement requires complete completed job observations')
        from .original_science import original_science_loader
        original_loader = original_science_loader(client, user_id=user_id, paper_hash=paper_hash, lang=lang, run=source)
        science = ScientificReceiptReducer(candidate.get('protocol'), [source['id'], *refs],
                                            receipt_refs=True, original_loader=original_loader)
        with repository.open_receipts(reference, source['tool_receipts']) as receipts:
            for receipt in receipts:
                science.observe(receipt)
        projected = deepcopy(source)
        projected['scientific_outcome'] = science.reported_outcome
        projected['artifact_refs'] = refs
        projected['artifact_ref'] = refs[0] if refs else ''
        projected['status'] = 'passed' if science.outcome_valid and science.reported_outcome == 'supported' and refs else 'inconclusive'
        summary = science.summary(projected['scientific_outcome'])
        projected['receipt_evidence'] = dict(summary, version=1, run_digest=receipt_evidence_digest(projected))
        return projected


def prepare_success_settlement(client, candidate, prior, *, user_id, paper_hash, lang, run_id, revision):
    if type(revision) is not int or revision < 0:
        raise ValueError('Invalid Research success settlement revision')
    old = run_from(prior, run_id); source = run_from(candidate, run_id)
    if old is None or source is None:
        raise ValueError('Research success settlement run was not found')
    result = ReceiptSuccessSettlement(user_id, paper_hash, lang, run_id, revision, digest(old), digest(source))
    result.project(client, candidate, prior, user_id=user_id, paper_hash=paper_hash, lang=lang, revision=revision)
    return result
