"""In-memory paper edit proposals: agent-proposed hunks the user reviews.

A proposal parks up to 20 exact-anchor search/replace hunks against one owned
local paper document. Hunks are pre-validated at creation by running the same
matching logic the journaled write tools use (``_compute_diff``) against the
live files — sequentially per file, so hunk N sees hunk N-1's result exactly
like the batch edit tool — and hunks that no longer match stay in the
proposal, marked failed. Accepting re-runs each selected pending hunk through
``tool_apply_diff`` (which re-validates against drifted files and journals one
undo transition per file); rejecting only marks. The store is bounded: 3600s
TTL, at most 64 proposals per owner and 4096 globally, oldest evicted first.

TODO(enterprise): this store is deliberately process-local for the
single-user product (restart loses pending proposals; the user simply
re-asks). A durable multi-user deployment must replace it with a storage-port
implementation keeping the same bounds, owner scoping and validation flow.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict

from .contract import validate
from .errors import BuildError
from .source_edit import resolve_edit_target, resolve_local_document

_TTL_SECONDS = 3600
_PER_OWNER_CAP = 64
_GLOBAL_CAP = 4096
_MAX_EDITS = 20

_LOCK = threading.Lock()
# proposal_id -> {'proposal': dict, 'owner': int, 'expires_at': float};
# insertion-ordered so eviction drops the oldest first.
_PROPOSALS: OrderedDict[str, dict] = OrderedDict()


def _read_text(target):
    from lib.project_mod.write_tools._ops import _read_original_text
    try:
        return _read_original_text(str(target))
    except (OSError, ValueError) as exc:
        raise BuildError('source_path', 'Paper source is unavailable') from exc


def _prevalidate(base, edits):
    """Mark each hunk pending/failed by replaying matches without writing."""
    from lib.project_mod.write_tools._ops import _compute_diff
    groups = OrderedDict()
    for row in edits:
        groups.setdefault(row['path'], []).append(row)
    for path, rows in groups.items():
        try:
            target = resolve_edit_target(base, path)
            current = _read_text(target)
        except BuildError as exc:
            from lib.error_envelope import from_exception
            for row in rows:
                row['status'] = 'failed'
                row['error'] = from_exception(exc)['message']
            continue
        for row in rows:
            comp = _compute_diff(current, path, row['search'], row['replace'])
            if comp['ok']:
                current = comp['new_content']
                row['status'] = 'pending'
            else:
                row['status'] = 'failed'
                row['error'] = (comp.get('error') or 'Edit does not match the current file')[:2000]


def _normalize_edits(base, edits):
    """Shape-check + path-gate hunks; traversal/binary targets are refused."""
    normalized = []
    for index, edit in enumerate(edits[:_MAX_EDITS]):
        path = str(edit.get('path') or '').strip()
        search = str(edit.get('search') or '')
        replace = str(edit.get('replace') or '')
        if not path or not search:
            raise BuildError('build_contract', 'Every proposal edit needs a path and a search anchor')
        resolve_edit_target(base, path)  # refuses traversal, non-paper and binary suffixes
        normalized.append({'index': index, 'path': path, 'search': search,
                           'replace': replace,
                           'description': str(edit.get('description') or '')[:200],
                           'status': 'pending'})
    if not normalized:
        raise BuildError('build_contract', 'A proposal needs at least one edit')
    return normalized


def _sweep(now):
    expired = [key for key, record in _PROPOSALS.items() if record['expires_at'] <= now]
    for key in expired:
        del _PROPOSALS[key]


def _evict(owner):
    while len(_PROPOSALS) > _GLOBAL_CAP:
        _PROPOSALS.popitem(last=False)
    owned = [key for key, record in _PROPOSALS.items() if record['owner'] == owner]
    while len(owned) > _PER_OWNER_CAP:
        del _PROPOSALS[owned.pop(0)]


def create_proposal(*, owner_user_id, document_id, conv_id, edits) -> dict:
    _document, base, _main = resolve_local_document(document_id, owner_user_id=owner_user_id)
    normalized = _normalize_edits(base, list(edits or []))
    _prevalidate(base, normalized)
    proposal = validate({
        'schema_version': 1,
        'proposal_id': secrets.token_hex(16),
        'document_id': document_id,
        'conv_id': str(conv_id or '')[:160],
        'created_at': int(time.time() * 1000),
        'status': 'pending',
        'edits': normalized,
    }, 'PaperProposalV1')
    with _LOCK:
        now = time.time()
        _sweep(now)
        _PROPOSALS[proposal['proposal_id']] = {
            'proposal': proposal, 'owner': owner_user_id,
            'expires_at': now + _TTL_SECONDS}
        _evict(owner_user_id)
    return proposal


def _owned_record(proposal_id, owner_user_id):
    record = _PROPOSALS.get(proposal_id)
    if not record or record['owner'] != owner_user_id:
        return None
    if record['expires_at'] <= time.time():
        del _PROPOSALS[proposal_id]
        return None
    return record


def get_proposal(proposal_id, *, owner_user_id) -> dict | None:
    with _LOCK:
        record = _owned_record(proposal_id, owner_user_id)
        return dict(record['proposal']) if record else None


def _proposal_status(edits):
    statuses = {row['status'] for row in edits}
    if statuses == {'applied'}:
        return 'applied'
    if 'applied' in statuses:
        return 'partial'
    if 'pending' in statuses:
        return 'pending'
    if 'rejected' in statuses:
        return 'rejected'
    return 'partial'  # every hunk failed pre-validation; nothing left to decide


def decide_proposal(proposal_id, indexes, action, *, owner_user_id) -> dict:
    """Apply ('accept') or mark ('reject') the selected pending hunks.

    Returns {'proposal': updated, 'results': per-hunk outcomes}. Accepting
    re-resolves the document and funnels every hunk through the journaled
    write tools, so a hunk only lands if it still matches the live file.
    """
    if action not in {'accept', 'reject'}:
        raise BuildError('build_contract', 'Proposal action must be accept or reject')
    with _LOCK:
        record = _owned_record(proposal_id, owner_user_id)
        if record is None:
            raise BuildError('proposal_not_found', 'Paper edit proposal is unavailable')
        proposal = record['proposal']
        known = {row['index'] for row in proposal['edits']}
        selected = known if indexes is None else set(indexes)
        if not selected <= known:
            raise BuildError('build_contract', 'Unknown proposal edit index')
        results = []
        pending = [row for row in proposal['edits']
                   if row['index'] in selected and row['status'] == 'pending']
        if action == 'reject':
            for row in pending:
                row['status'] = 'rejected'
                results.append({'index': row['index'], 'ok': True})
        elif pending:
            _document, base, _main = resolve_local_document(
                proposal['document_id'], owner_user_id=owner_user_id)
            from lib.project_mod.write_tools import tool_apply_diff
            conv_id = proposal.get('conv_id') or None
            for row in pending:
                outcome = tool_apply_diff(str(base), row['path'], row['search'],
                                          row['replace'],
                                          description=row.get('description') or '',
                                          conv_id=conv_id)
                if outcome.get('ok'):
                    row['status'] = 'applied'
                    row.pop('error', None)
                    results.append({'index': row['index'], 'ok': True})
                else:
                    row['status'] = 'failed'
                    row['error'] = (outcome.get('error') or 'Edit no longer matches')[:2000]
                    results.append({'index': row['index'], 'ok': False,
                                    'error': row['error']})
        proposal['status'] = _proposal_status(proposal['edits'])
        record['expires_at'] = time.time() + _TTL_SECONDS
        return {'proposal': validate(dict(proposal), 'PaperProposalV1'),
                'results': results}
