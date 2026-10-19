"""Quote identity is deterministic; semantic verdicts are model assessments, not proof."""
from __future__ import annotations

import hashlib

STATUSES = ('supported', 'contradicted', 'insufficient', 'model_assessed')


def verify_quote(card: dict, sources: dict) -> bool:
    """Offsets are Unicode character offsets in the exact stored parsed snapshot."""
    source = sources.get(card.get('arxiv_id'), {})
    text = source.get('content', '')
    start, end = card.get('start'), card.get('end')
    return bool(
        source.get('source') == 'parsed_text'
        and card.get('source') == 'parsed_text'
        and isinstance(start, int) and not isinstance(start, bool)
        and isinstance(end, int) and not isinstance(end, bool)
        and 0 <= start < end <= len(text)
        and card.get('quote') == text[start:end]
        and card.get('snapshot_sha256') == hashlib.sha256(text.encode()).hexdigest()
        and card.get('source_version', '') == source.get('source_version', '')
        and card.get('paper_hash', '') == source.get('paper_hash', ''))


def assess_gaps(gaps, cards, sources, call, *, batch_size=24):
    """Independently compare every retained claim with every valid evidence batch.

    A supported competing solution contradicts (and closes) the scoped gap.
    Failed/unvisited batches cannot establish absence of counterevidence.
    """
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 24:
        raise ValueError('entailment batch_size must be between 1 and 24')
    valid = [c for c in cards if verify_quote(c, sources)]
    for gap in gaps:
        assessments, failed = [], 0
        for start in range(0, len(valid), batch_size):
            batch = valid[start:start + batch_size]
            try:
                result = call('entailment', {
                    'claim': gap.get('scoped_claim') or gap.get('gap', ''),
                    'cards': batch,
                    'instruction': 'Independently assess the scoped open-gap claim against original quotes. '
                    'Return assessments: [{card_id, status, rationale}]. Status supported means the '
                    'quote supports the gap; contradicted means supported competing evidence solves '
                    'the scoped gap; insufficient means neither. Do not infer absence from silence.'})
                rows = result.get('assessments', [])
                allowed = {c['card_id'] for c in batch}
                retained = [dict(r, assessment='model_assessed') for r in rows
                            if isinstance(r, dict) and r.get('card_id') in allowed
                            and r.get('status') in STATUSES]
                assessments.extend(retained)
                if not allowed <= {r['card_id'] for r in retained}:
                    failed += 1
            except Exception as error:
                from lib.llm.llm_errors import AbortedError
                if isinstance(error, AbortedError):
                    raise
                failed += 1
        incomplete = bool(failed or len(valid) != len(cards))
        statuses = {r['status'] for r in assessments}
        verdict = ('contradicted' if 'contradicted' in statuses else
                   'supported' if 'supported' in statuses else 'insufficient')
        gap.update(entailment=verdict, assessment='model_assessed',
                   status='closed' if verdict == 'contradicted' else
                   'open' if verdict == 'supported' and not incomplete else 'uncertain',
                   evidence_assessments=assessments,
                   assessment_incomplete=incomplete,
                   confidence='model_assessed', low_confidence=verdict == 'insufficient' or incomplete)
        gap['reference_verification'] = {
            'support': verdict, 'counterevidence': 'model_assessed',
            'closest_work': 'unverified', 'note': 'Independent model assessment, not proof.'}
    return gaps
