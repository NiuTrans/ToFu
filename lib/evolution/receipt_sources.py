"""Resolve short delivery provenance for new receipts and read-only old memory.

Entries: deliverable_reference / read_original_page. A frozen auxiliary source binds an old executor;
program settlement may supply its explicit last executing receipt task instead.
Stage outcomes own metadata/pages. Inline text only checks identity, never
supplies original content. No receipt migration, task replay, or review policy.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib

from . import stage_outcomes
from .contracts import EvolutionStateError


def deliverable_reference(receipt, *, user_id, execution_task_id=None):
    """Discover a sealed, exact short original; missing provenance stays unknown."""
    if receipt.get('result_source'):
        return deepcopy(receipt['result_source'])
    auxiliaries = [(kind, receipt[field]) for kind, field in
        (('tool_evidence', 'tool_evidence_source'), ('artifact_evidence', 'artifact_evidence_source'))
        if receipt.get(field)]
    task_ids = {ref['task_id'] for _, ref in auxiliaries}
    if execution_task_id is not None:
        task_ids.add(execution_task_id)
    if not task_ids:
        return None
    if len(task_ids) != 1:
        raise EvolutionStateError('short original executor references disagree')
    task_id = next(iter(task_ids))
    outcome = stage_outcomes.read_metadata(receipt['run_id'], task_id, user_id=user_id)
    if outcome is None and not auxiliaries:
        # Old/synthetic stages without a terminal archive remain unavailable.
        return None
    if outcome is None or outcome['archive_state'] != 'sealed' or outcome['stage'] != 'executing':
        raise EvolutionStateError('short original executor archive is unavailable or unsealed')
    for kind, ref in auxiliaries:
        descriptor = stage_outcomes.source_view(outcome, kind)
        if (ref.get('source_kind', kind) != kind
                or ref['source_sha256'] != descriptor['content_sha256']
                or ref['total_chars'] != descriptor['content_chars']
                or (kind == 'artifact_evidence' and ref.get('complete') != descriptor['complete'])):
            raise EvolutionStateError('short original auxiliary reference changed')
    text = receipt['result']
    if (len(text) > 24000 or len(text) != outcome['content_chars']
            or hashlib.sha256(text.encode('utf-8')).hexdigest() != outcome['content_sha256']):
        raise EvolutionStateError('short original differs from the complete frozen delivery')
    if not text:
        # Even zero-character originals must pass the owned-run source boundary.
        stage_outcomes.page(receipt['run_id'], task_id, user_id=user_id)
    from .source_tools import reference
    return reference(outcome)


def read_original_page(receipt, source_kind, *, user_id, offset=0, limit=6000, source_digest=None):
    """Read an archived receipt's exact original; missing sources never become previews."""
    if source_kind == 'deliverable':
        ref = deliverable_reference(receipt, user_id=user_id)
    else:
        field = {'tool_evidence': 'tool_evidence_source',
                 'artifact_evidence': 'artifact_evidence_source',
                 'background_evidence': 'background_evidence_source'}.get(source_kind)
        if field is None:
            raise EvolutionStateError('unknown archived original source kind')
        ref = receipt.get(field)
    if not ref:
        raise EvolutionStateError('archived original source is unavailable')
    if source_digest is not None and source_digest != ref['source_sha256']:
        raise EvolutionStateError('archived original source identity changed')
    metadata = stage_outcomes.read_metadata(receipt['run_id'], ref['task_id'], user_id=user_id)
    if metadata is None or metadata['archive_state'] != 'sealed' or metadata['stage'] != 'executing':
        raise EvolutionStateError('archived original is unavailable or unsealed')
    descriptor = stage_outcomes.source_view(metadata, source_kind)
    if (ref.get('source_kind', source_kind) != source_kind
            or ref['source_sha256'] != descriptor['content_sha256']
            or ref['total_chars'] != descriptor['content_chars']
            or (source_kind == 'artifact_evidence' and ref.get('complete') != descriptor['complete'])):
        raise EvolutionStateError('archived original reference changed')
    page = stage_outcomes.page(receipt['run_id'], ref['task_id'], user_id=user_id,
                               source_kind=source_kind, offset=offset, limit=limit)
    if page['source_sha256'] != ref['source_sha256'] or page['total_chars'] != ref['total_chars']:
        raise EvolutionStateError('archived original changed during read')
    return page
