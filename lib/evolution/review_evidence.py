"""Retain material review objections and refuse contradictory positive verdicts.

Runs freeze this policy for new generic tasks. Independent reviewers identify
objections; this module binds them to acceptance and projects negative/unknown
judgments. It cannot discover truth, resolve objections, or grant learning.
"""
from .contracts import EvolutionContractError

VERSION = 3


def instruction(run):
    if not run.get('evidence_review_version'):
        return ''
    return (
        ('\nFor evidence policy v2 validation successors, deliver every declared direct-parent '
         'original through evolution_source into successful final provider inputs. '
         'review_next_action considers current and historical delivery together; '
         'historical_read_progress is separate from current submission coverage; '
         'follow next_unread_offset until both are complete before positive acceptance. '
         'Reading or reporting a preview is insufficient. If required parent references '
         'are unavailable, finish inspecting current sources and submit an unknown '
         'objective with the concrete missing evidence rather than repeating impossible reads.\n'
         if run.get('evidence_review_version', 0) >= 2 else '')
        + ('\nEvidence policy v3 records successful delivery of authorized frozen program-history originals separately from current submission coverage. Historical access receipts do not establish claim validity, current-generation applicability or complete final acceptance.\n' if run.get('evidence_review_version', 0) >= 3 else '')
        + '\nReturn evidence_audit with complete and objections. Independently check '
        'material claims against inspected sources before deciding acceptance. '
        'Record contradicted facts as contradicted, and missing or unsupported '
        'evidence as unverified; bind each objection to its criterion_id and, '
        'where applicable, the exact requirement_id. Include the claim and '
        'concrete inspected evidence_ref. A correct hash does not prove the '
        'claim made about its content. Check counts, causal/cost assertions and '
        'whether baselines are competent under the same available information; '
        'repeating the motivating defect is not baseline qualification. Future '
        'mechanisms and scenarios may be conditional hypotheses: distinguish '
        'them from assertions that an enabling capability is unavailable or '
        'necessary, or that novelty, demand or benefit has been verified. '
        'Do not invent objections to work outside the original acceptance. '
        'Objections are unresolved material acceptance blockers, not style '
        'suggestions; they cannot coexist with a passing affected criterion. '
        'Use at most eight objections. If review coverage or that bound leaves '
        'material objections unaudited, set complete=false. An empty list '
        'means only that this reviewer recorded no material objection; it '
        'does not establish truth or consumer value.\n')


def enforce(run, review):
    audit = review.get('evidence_audit')
    if audit is None:
        if run.get('evidence_review_version'):
            raise EvolutionContractError('review must include the frozen evidence audit')
        return
    criteria = {row['criterion_id']: row for row in review['criterion_results']}
    requirements = {row['requirement_id']: row for row in review.get('requirement_results', [])}

    def refuse(row, outcome, evidence):
        # An unknown objection cannot weaken an already failed check.
        if row['outcome'] == 'fail' and outcome == 'unknown':
            return
        row.update(outcome=outcome, evidence_ref=evidence[:500])

    for index, objection in enumerate(audit['objections']):
        requirement = objection.get('requirement_id')
        if requirement and requirement not in requirements:
            raise EvolutionContractError('evidence objection references an unknown requirement')
        outcome = 'fail' if objection['classification'] == 'contradicted' else 'unknown'
        evidence = f'Evidence objection {index + 1}: {objection["claim"]}; {objection["evidence_ref"]}'
        refuse(criteria[objection['criterion_id']], outcome, evidence)
        if requirement:
            refuse(requirements[requirement], outcome, evidence)
            refuse(criteria['objective'], outcome, evidence)
    if not audit['complete']:
        refuse(criteria['objective'], 'unknown', 'Independent evidence audit is incomplete; material objections may remain.')
