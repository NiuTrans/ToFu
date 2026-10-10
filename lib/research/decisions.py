"""Derive bounded research decisions and audit gates from current run receipts.

No tool execution or numerical confidence estimation occurs here. Protocol plans
are proposals; only structured completed-result receipts can satisfy audit gates.
"""
from __future__ import annotations

from collections.abc import Mapping

from .observation_audit import CHECKS as AUDIT_CHECKS


def normalize_portfolio(value):
    raw = value if isinstance(value, Mapping) else {}
    result = {}
    for key, fields in {
        'competing_hypotheses': ('id', 'statement', 'prediction', 'scope'),
        'discriminating_experiments': ('id', 'hypothesis_ids', 'design', 'decision_rule',
                                     'expected_information', 'expected_value', 'cost'),
    }.items():
        rows = raw.get(key)
        if not isinstance(rows, list):
            continue
        normalized = []
        for row in rows[:16]:
            if not isinstance(row, Mapping):
                continue
            item = {field: str(row.get(field) or '').strip()[:2000]
                    for field in fields if field != 'hypothesis_ids'}
            if 'hypothesis_ids' in fields:
                ids = row.get('hypothesis_ids')
                item['hypothesis_ids'] = [str(x)[:96] for x in ids[:16]] if isinstance(ids, list) else []
            if item['id']:
                normalized.append(item)
        if normalized:
            result[key] = normalized
    return result


def authoritative(run):
    if run.get('validity') != 'current' or run.get('execution_status') != 'completed':
        return False
    if run.get('receipt_history', {}).get('head'):
        from .science_reduction import bound_receipt_evidence
        evidence = bound_receipt_evidence(run)
        return bool(evidence and evidence['observed_receipts'])
    return any(r.get('status') == 'done' and r.get('result_digest') for r in run.get('tool_receipts', []))


def receipt_outcome_consistent(run, protocol=None):
    """Bind explicit completed receipt outcomes to the retained run conclusion.

    Legacy receipts without an outcome add no numerical proof. Unknown explicit
    labels and decisive conclusions that disagree with any completed receipt
    fail closed; pending/error receipts cannot override completed observations.
    """
    from .science_reduction import reduce_scientific_receipts, bound_receipt_evidence
    if run.get('receipt_history', {}).get('head'):
        evidence = bound_receipt_evidence(run, protocol)
        return bool(evidence and evidence['outcome_consistent']
                    and evidence['scientific_outcome'] == run.get('scientific_outcome'))
    return reduce_scientific_receipts(run.get('tool_receipts', []),
        expected_outcome=run.get('scientific_outcome'), protocol=protocol)['outcome_consistent']


def effective_outcome(run, protocol=None):
    return run.get('scientific_outcome') if receipt_outcome_consistent(run, protocol) else 'inconclusive'


def has_conflicting_evidence(runs, history=None, protocol=None):
    """Current completed receipts cannot cherry-pick opposite decisive outcomes."""
    outcomes = {effective_outcome(run, protocol) for run in runs if authoritative(run)}
    outcomes.update(key for key, count in (history or {}).get('outcomes', {}).items() if count)
    return {'supported', 'refuted'}.issubset(outcomes)


def has_unresolved_evidence(runs, history=None, protocol=None):
    """Incomplete or contradictory conclusions cannot disappear from review."""
    return (has_conflicting_evidence(runs, history, protocol)
            or any(effective_outcome(run, protocol) == 'inconclusive' for run in runs if authoritative(run))
            or bool((history or {}).get('outcomes', {}).get('inconclusive', 0)))


def research_decision(protocol, runs, history=None):
    observed = [r for r in runs if authoritative(r)]
    outcomes = {effective_outcome(r, protocol) for r in observed}
    outcomes.update(key for key, count in (history or {}).get('outcomes', {}).items() if count)
    status = ('retain' if outcomes == {'supported'} else
              'refute' if outcomes == {'refuted'} else
              'refine' if has_conflicting_evidence(observed, history, protocol) else 'inconclusive')
    reason = {
        'retain': 'Retain only within the tested protocol scope; independent validation remains necessary.',
        'refute': 'The tested hypothesis is refuted in this protocol scope. A negative-result manuscript is valid.',
        'refine': 'Conflicting results require a narrower scope and a discriminating replication.',
        'inconclusive': 'No decisive current evidence; repair missing evidence or resolve uncertainty before claiming support.',
    }[status]
    plans = normalize_portfolio(protocol).get('discriminating_experiments', [])
    next_actions = []
    for index, plan in enumerate(plans):
        next_actions.append(dict(plan, priority=index + 1, requires_confirmation=True))
    if not next_actions:
        next_actions = [{
            'id': 'resolve-current-evidence', 'hypothesis_ids': [], 'priority': 1,
            'design': ('Replicate the frozen comparison on independent held-out data; '
                       'separate scope effects from the competing explanation.'),
            'decision_rule': str(protocol.get('falsifier') or 'Owner must freeze a falsifier before execution.'),
            'expected_information': 'Distinguish reproducible effects from uncertainty or protocol-specific failure.',
            'expected_value': 'Bound the claims that can be reported, including a negative result.',
            'cost': str(protocol.get('resources') or 'Unknown; estimate compute, data, time and tool costs before confirmation.'),
            'requires_confirmation': True,
        }]
    return {'status': status, 'reason': reason,
            'run_ids': [r['id'] for r in observed],
            'scope': str(protocol.get('dataset') or 'Unspecified tested population'),
            'next_actions': next_actions}


def manuscript_audit(runs, claims, history=None, protocol=None):
    checks = {key: False for key in AUDIT_CHECKS}
    eligible = [r for r in runs if authoritative(r)]
    archived = sum((history or {}).get('outcomes', {}).values())
    for key in checks:
        checks[key] = bool(eligible or archived) and not (history or {}).get('audit_failures', {}).get(key, 0)
    from .science_reduction import reduce_scientific_receipts
    for run in eligible:
        refs = {run.get('id'), run.get('artifact_ref'), *run.get('artifact_refs', [])} - {None, ''}
        if run.get('receipt_history', {}).get('head'):
            from .science_reduction import bound_receipt_evidence
            summary = bound_receipt_evidence(run, protocol)
            valid = receipt_outcome_consistent(run, protocol)
        else:
            summary = reduce_scientific_receipts(run.get('tool_receipts', []),
                expected_outcome=run.get('scientific_outcome'), protocol=protocol, evidence_refs=refs)
            valid = summary['outcome_consistent']
        for key in checks:
            checks[key] &= bool(valid and summary and summary['checks'][key])
    checks['claim_traceability'] = bool(claims) and all(
        c.get('status') == 'supported' and c.get('validity') == 'current' for c in claims)
    return {'passed': all(checks.values()),
            'verification_basis': 'reported_observation_consistency/v1',
            'checks': [{'id': key, 'ok': ok} for key, ok in checks.items()]}
