"""Validate exported transformation witnesses before joining source/cache evidence.

Only explicit positions and request/transport identity can join an occurrence.
Old exports remain readable. Missing, malformed, ambiguous or stale witnesses
stay diagnostic losses, never reconstructed source authority or cache verdicts.
"""
import hashlib
import json
from functools import cache
from pathlib import Path

from lib.llm_sanitize.context_sections import context_sections
from lib.llm_sanitize.lineage import observed_span, span_hash


@cache
def _lineage_validator():
    # Offline inspection uses the authority schema, not a parallel shape table.
    from jsonschema import Draft202012Validator
    path = Path(__file__).resolve().parents[3] / 'contracts/context_lineage_v1.schema.json'
    return Draft202012Validator(json.loads(path.read_text(encoding='utf-8')))


def request_document(document):
    if not isinstance(document, dict):
        raise ValueError('Expected a JSON request or investigation bundle')
    if 'request' not in document:
        return document
    if 'messages' in document or 'input' in document or not isinstance(document['request'], dict):
        raise ValueError('An investigation bundle must contain exactly one request object')
    return document['request']


def _candidates(document):
    evidence = document.get('evidence', document)
    if not isinstance(evidence, dict):
        return []
    rows = [evidence.get('contextLineage'), evidence.get('_wire_context_lineage')]
    supplied = evidence.get('wireProjections')
    projections = list(supplied) if isinstance(supplied, list) else []
    if isinstance(evidence.get('wireProjection'), dict):
        projections.append(evidence['wireProjection'])
    rows.extend(row.get('contextLineage') for row in projections if isinstance(row, dict))
    unique = []
    for row in rows:
        if isinstance(row, dict) and row not in unique:
            unique.append(row)
    return unique


def _select(document, request, wire):
    candidates = _candidates(document)
    if not candidates:
        return None, 'missing'
    if wire:
        raw = json.dumps(request, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        candidates = [row for row in candidates if row.get('requestContentHash') == digest
                      and (not document.get('traceId') or row.get('traceId') == document['traceId'])]
    else:
        candidates = [row for row in candidates if row.get('stage') == 'canonical'
                      and not row.get('requestContentHash')]
    if len(candidates) != 1:
        return None, 'ambiguous' if candidates else 'request_mismatch'
    trace = candidates[0]
    if trace.get('degraded'):
        return None, 'degraded'
    if not _lineage_validator().is_valid(trace):
        return None, 'invalid'
    return trace, 'verified_content' if wire else 'verified_positions'


def _cache_observation(document, trace):
    records = document.get('cacheRecords', [])
    if isinstance(document.get('cacheRecord'), dict):
        records = [*records, document['cacheRecord']] if isinstance(records, list) else []
    if not isinstance(records, list) or not records:
        return {'status': 'missing'}
    identity = trace.get('requestIdentity') or {}
    keys = ('semantic_request_id', 'provider_prefix_id')
    matches = [row for row in records if isinstance(row, dict)
               and trace.get('traceId') and row.get('trace_id') == trace['traceId']
               and isinstance(row.get('request_identity'), dict)
               and all(identity.get(key) and identity[key] == row['request_identity'].get(key) for key in keys)]
    if len(matches) != 1:
        return {'status': 'ambiguous' if matches else 'request_mismatch'}
    keys = ('bucket', 'cache_read', 'cache_write', 'body_identical', 'wire_available',
            'wire_changed', 'namespace_verified', 'ttl_flip', 'breakpoint_lost',
            'routing_diff', 'culprits')
    return {'status': 'matched', 'owner': 'cache_tracking',
            **{key: matches[0][key] for key in keys if key in matches[0]}}


def inspect_lineage(document, request, *, wire):
    trace, status = _select(document, request, wire)
    report = {'status': status, 'losses': []}
    if trace is None:
        return [], report, {'status': 'unbound'}
    report['stage'] = trace.get('stage')
    origins = []
    for row in trace['sources']:
        if not isinstance(row.get('id'), str):
            report['losses'].append({'reason': 'invalid_source'})
            continue
        identity = {key: row[key] for key in ('id', 'eventId') if key in row}
        if row.get('status') != 'located':
            report['losses'].append({**identity, 'lostAt': row.get('lostAt'), 'reason': 'unmapped'})
            continue
        location = row.get('output')
        text = observed_span(request, location)
        if text is None or span_hash(text) != row.get('spanHash'):
            report['losses'].append({**identity, 'lostAt': 'artifact_validation', 'reason': 'span_mismatch'})
            continue
        event = row.get('evidence') == 'event_receipt'
        section = next(context_sections(text), None) if not event else None
        if not event and (section is None or section.id != row['id']):
            report['losses'].append({**identity, 'reason': 'section_mismatch'})
            continue
        digest = span_hash(text)[:16] if event else section.content_hash
        origins.append({**identity, 'hash': digest, **location,
                        'evidence': 'transformation_lineage',
                        'sourceEvidence': row.get('evidence'),
                        'sourceHash': row.get('hash'),
                        'changedAt': row.get('changedAt'),
                        'matchesReceipt': (digest == row['receiptHash']
                                           if row.get('receiptHash') else None)})
    return origins, report, _cache_observation(document, trace) if wire else {'status': 'unbound'}
