"""Stream complete reported scientific receipts without retaining payload history.

ScientificReceiptReducer binds outcomes and recomputable audit checks to a frozen
protocol and explicit evidence references. reduce_scientific_receipts is the pure
entry point shared by workspace inspection and decision/manuscript projections.
No proof here establishes authentic data, independent reproduction or execution
completion. Bound summaries use program normalization and the research schema;
no storage, model or tool execution.
"""
from collections.abc import Mapping

from lib.research.observation_audit import decode_observation_json
from copy import deepcopy
import json
import hashlib
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .observation_audit import CHECKS, verify_observations


class ScientificReceiptReducer:
    def __init__(self, protocol=None, evidence_refs=(), *, receipt_refs=False, original_loader=None):
        self.protocol = deepcopy(protocol)
        self.refs = set(evidence_refs)
        self.receipt_refs = receipt_refs
        self.original_loader = original_loader
        self.outcomes = set()
        self.numeric_rule = isinstance(protocol, Mapping) and 'decision_rule' in protocol
        self.valid_outcomes = True
        self.valid_numeric = True
        self.measured = False
        self.verified_audit = False
        self.observed_receipts = 0

    def observe(self, receipt):
        if receipt.get('status') != 'done' or not receipt.get('result_digest'):
            return
        self.observed_receipts += 1
        if receipt.get('observation_incomplete'):
            payload = self.original_loader(receipt) if self.original_loader is not None else None
            if not isinstance(payload, Mapping):
                # Later valid receipts cannot substitute for an unverified original.
                self.valid_outcomes = False
                self.valid_numeric = False
                return
        else:
            try:
                payload = decode_observation_json(receipt.get('observation_excerpt') or receipt.get('result_excerpt') or '')
            except (ValueError, TypeError):
                raw = str(receipt.get('observation_excerpt') or receipt.get('result_excerpt') or '')
                if any(marker in raw for marker in ('"scientific_observations"', '"scientific_outcome"')):
                    self.valid_outcomes = self.valid_numeric = False
                return
        raw = str(receipt.get('observation_excerpt') or receipt.get('result_excerpt') or '')
        marked = any(marker in raw for marker in ('"scientific_observations"', '"scientific_outcome"'))
        if (not isinstance(payload, Mapping) or (marked and not any(
                key in payload for key in ('scientific_observations', 'scientific_outcome')))):
            if marked:
                self.valid_outcomes = self.valid_numeric = False
            return
        explicit = 'scientific_outcome' in payload
        outcome = payload.get('scientific_outcome')
        if explicit:
            if not isinstance(outcome, str) or outcome not in {'supported', 'refuted', 'inconclusive'}:
                self.valid_outcomes = False
            else:
                self.outcomes.add(outcome)
        audit = payload.get('scientific_audit')
        needs_numeric = explicit and self.numeric_rule
        refs = self.refs
        if self.receipt_refs and receipt.get('artifact_ref'):
            refs = refs | {receipt['artifact_ref']}
        needs_audit = isinstance(audit, Mapping) and bool(refs)
        if not (needs_numeric or needs_audit):
            return
        computed = verify_observations(payload, self.protocol)
        if needs_numeric:
            if all(computed.values()):
                self.measured = True
            else:
                self.valid_numeric = False
        if needs_audit:
            # One receipt must prove every check with links to this run. Separate
            # incomplete receipts cannot be stitched into a fictitious full proof.
            linked_checks = []
            for key in CHECKS:
                check = audit.get(key)
                linked = check.get('evidence_refs') if isinstance(check, Mapping) else None
                linked_checks.append(computed[key] and isinstance(linked, list) and bool(linked)
                    and all(isinstance(ref, str) and ref in refs for ref in linked))
            self.verified_audit |= all(linked_checks)

    @property
    def outcome_valid(self):
        return (self.valid_outcomes and self.valid_numeric
                and (not self.numeric_rule or self.measured))

    @property
    def reported_outcome(self):
        return next(iter(self.outcomes)) if self.outcome_valid and len(self.outcomes) == 1 else 'inconclusive'

    def consistent_with(self, expected):
        if not self.outcome_valid:
            return False
        return not self.outcomes or expected == self.reported_outcome

    def summary(self, expected):
        consistent = self.consistent_with(expected)
        return {'scientific_outcome': self.reported_outcome,
                'outcome_consistent': consistent, 'observed_receipts': self.observed_receipts,
                'checks': {key: bool(consistent and self.verified_audit) for key in CHECKS},
                'verification_basis': 'reported_observation_consistency/v1'}


def reduce_scientific_receipts(receipts, *, expected_outcome, protocol=None, evidence_refs=(), original_loader=None):
    reducer = ScientificReceiptReducer(protocol, evidence_refs, original_loader=original_loader)
    for receipt in receipts:
        reducer.observe(receipt)
    return reducer.summary(expected_outcome)


@lru_cache(maxsize=1)
def evidence_validator():
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                         'research_program_v1.schema.json').read_text())
    return Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/receiptEvidence'})


def normalize_receipt_evidence(value):
    if (not evidence_validator().is_valid(value) or type(value['version']) is not int
            or type(value['observed_receipts']) is not int):
        raise ValueError('Invalid Research complete receipt evidence')
    return deepcopy(value)


def receipt_evidence_digest(run):
    from .program import normalize_run_evidence
    material = dict(run); material.pop('receipt_evidence', None)
    canonical = normalize_run_evidence([material])[0]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def bound_receipt_evidence(run, protocol=None):
    """Verify server-owned full-stream projection against immutable run and protocol."""
    evidence = run.get('receipt_evidence')
    if evidence is None:
        return None
    try:
        evidence = normalize_receipt_evidence(evidence)
        if evidence['run_digest'] != receipt_evidence_digest(run):
            return None
        if protocol is not None:
            from .program import dependency_digests
            if run.get('protocol_digest') != dependency_digests({'protocol': protocol})['protocol_digest']:
                return None
        return evidence
    except (ValueError, TypeError):
        return None
