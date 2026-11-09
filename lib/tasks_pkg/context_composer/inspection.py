"""Read-only source attribution for exported context and provider requests.

Entry points: inspect_context(document), compare_context(before, after).
Dependencies: registry policy, the renderer's section identity and the existing
wire fingerprint owner. No acquisition, task mutation, storage, tokenization or
provider calls. Receipts explain decisions; actual sections witness content.
These reports locate request drift, never infer a provider cache-hit verdict.
"""
from __future__ import annotations

import hashlib

from ._sections import context_sections
from .registry import block_spec
from ._lineage_inspection import inspect_lineage, request_document
from lib.tasks_pkg.wire_fingerprint import (
    cache_relevant_controls, capture_wire_message_evidence, marker_signature, wire_byte_region,
)


def _request_parts(document: dict, wire: bool) -> tuple[list[dict], object, list[dict]]:
    """Accept complete Inspector snapshots or Chat/Anthropic/Responses bodies."""
    if not isinstance(document, dict):
        raise ValueError('Expected a JSON object containing messages or input')
    keys = [key for key in ('messages', 'input') if key in document]
    if len(keys) != 1:
        raise ValueError('Provide exactly one complete messages or input field')
    messages = document[keys[0]]
    if keys[0] == 'input' and isinstance(messages, str):
        messages = [{'role': 'user', 'content': messages}]
    if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
        raise ValueError('messages/input must be an array of message objects')
    manifest = document.get('contextManifest', [])
    if not isinstance(manifest, list) or any(not isinstance(row, dict) for row in manifest):
        raise ValueError('contextManifest must be an array of receipt objects')
    # Only top-level local metadata is removed. Underscored keys inside a tool
    # argument or user JSON are real content, not composer implementation data.
    observed = messages if wire else [
        {key: value for key, value in message.items()
         if not key.startswith('_') and key != 'carrier'} for message in messages]
    system = document.get('instructions') if keys[0] == 'input' else document.get('system')
    return observed, system, manifest


def _source(block_id: str) -> dict:
    spec = block_spec(block_id)
    return {'id': block_id, 'registryId': spec.id if spec else None,
            'source': spec.source if spec else None,
            'lane': spec.lane if spec else None,
            'authority': spec.authority if spec else None,
            'placement': spec.placement if spec else None,
            'registry': 'lib/tasks_pkg/context_composer/registry.py' if spec else None,
            'writer': ({'block': 'lib.tasks_pkg.context_composer.render_context',
                        'telemetry': 'lib.tasks_pkg.context_composer.record_injection_diagnostic'}
                       .get(spec.lane, 'lib.tasks_pkg.context_composer.append_injection')) if spec else None}


def _decisions(manifest: list[dict], block_id: str, content_hash: str) -> list[dict]:
    # Never copy noteText, arbitrary provenance or the source body into reports.
    keys = ('reason', 'injected', 'appended', 'reused', 'hash', 'round',
            'appendedTokens', 'delivery')
    return [{**{key: row[key] for key in keys if key in row},
             'matchesObservedHash': row.get('hash') == content_hash}
            for row in manifest if row.get('id') == block_id]


def _origins(message: dict, manifest: list[dict], *, wire: bool) -> list[dict]:
    origins = []
    metadata = message.get('_contextBlocks')
    metadata = metadata if isinstance(metadata, list) else []
    for section in context_sections(message.get('content')):
        source = _source(section.id)
        owned = (not wire and bool(message.get('_contextComposer'))
                 and (source['placement'] != 'system' or message.get('role') == 'system'))
        receipt = next((row for row in metadata if isinstance(row, dict)
                        and row.get('id') == section.id), {}) if owned else {}
        origins.append({**source, 'hash': section.content_hash,
                        'part': section.part, 'start': section.start, 'end': section.end,
                        'evidence': 'managed_carrier' if owned else 'marker_only',
                        'matchesReceipt': receipt.get('hash') == section.content_hash if receipt else None,
                        'decisions': _decisions(manifest, section.id, section.content_hash)})
    receipt = message.get('_contextInjection')
    if not wire and isinstance(receipt, dict) and isinstance(message.get('content'), str):
        block_id = message.get('_contextInjectionId')
        if isinstance(block_id, str) and block_id == receipt.get('id'):
            digest = hashlib.sha256(message['content'].encode('utf-8')).hexdigest()[:16]
            origins.append({**_source(block_id), 'hash': digest,
                            'evidence': 'event_receipt', 'eventId': receipt.get('eventId'),
                            'matchesReceipt': digest == receipt.get('hash'),
                            'decisions': _decisions(manifest, block_id, digest)})
            return origins
    origins.extend(_event_receipt_matches(message.get('content'), manifest))
    return origins


def _event_receipt_matches(content, manifest: list[dict]) -> list[dict]:
    """Sanitized snapshots lost private IDs; equal text identifies candidates only.

    Repeated identical events remain distinct receipts. Never pick an arbitrary
    occurrence or infer an ACK from a text match.
    """
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        content = content[0].get('text')
    if not isinstance(content, str):
        return []
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
    matches = []
    for row in manifest:
        block_id = row.get('id')
        spec = block_spec(block_id) if isinstance(block_id, str) else None
        if (spec and spec.lane in ('event', 'inbox', 'bootstrap')
                and row.get('eventId') and row.get('appended') and row.get('hash') == digest):
            matches.append({**_source(block_id), 'hash': digest,
                            'evidence': 'receipt_hash_match', 'eventId': row['eventId'],
                            'matchesReceipt': True, 'decisions': _decisions([row], block_id, digest)})
    return matches


def inspect_context(document: dict, *, wire: bool = False) -> dict:
    """Describe all observed carriers without copying prompt bodies or mutating input.

    wire=True treats even matching markers as observations, never authority.
    Working and Inspector snapshots are not final provider transport evidence.
    """
    request = request_document(document)
    observed, system, manifest = _request_parts(request, wire)
    original = request.get('messages', request.get('input'))
    if isinstance(original, str):
        original = observed
    messages = [{'index': index, 'role': message.get('role', message.get('type', '')),
                 'origins': _origins(message, manifest, wire=wire)}
                for index, message in enumerate(original)]
    system_origins = _origins({'role': 'system', 'content': system}, manifest, wire=True)
    mapped, lineage, cache_observation = inspect_lineage(document, request, wire=wire)
    _merge_lineage_origins(mapped, messages, system_origins, manifest)
    located = {origin.get('eventId') for origins in [system_origins, *(m['origins'] for m in messages)]
               for origin in origins}
    unlocated = [{**_source(row['id']), 'eventId': row['eventId'], 'hash': row.get('hash'),
                  'reason': 'no_exact_carrier_match'}
                 for row in manifest if row.get('eventId') and row.get('appended')
                 and isinstance(row.get('id'), str) and row['eventId'] not in located]
    return {
        'view': 'parsed_wire_request' if wire else 'message_snapshot',
        'cacheVerdict': 'not_evaluated',
        'messages': messages,
        'systemOrigins': system_origins,
        'unlocatedEventReceipts': unlocated,
        'lineage': lineage, 'cacheObservation': cache_observation,
    }


def _merge_lineage_origins(mapped, messages, system_origins, manifest):
    for origin in mapped:
        target = (messages[origin['message']]['origins']
                  if origin['region'] in ('messages', 'input') else system_origins)
        source = _source(origin['id'])
        if (source['placement'] == 'system' and origin['region'] in ('messages', 'input')
                and messages[origin['message']]['role'] not in ('system', 'developer')):
            origin['evidence'] = 'marker_only'
        target[:] = [row for row in target if not (row['id'] == origin['id'] and (
            row['evidence'] == 'receipt_hash_match'
            or all(row.get(key) == origin.get(key) for key in ('part', 'start', 'end'))))]
        target.append({**source, **origin,
                       'decisions': _decisions(manifest, origin['id'], origin['hash'])})


def _changed_origins(before: list[dict], after: list[dict]) -> list[dict]:
    """Compare every version of each source; do not blame unchanged siblings."""
    def hashes(origins):
        result = {}
        for origin in origins:
            result.setdefault(origin['id'], []).append(origin['hash'])
        return result

    old, new = hashes(before), hashes(after)
    sources = {origin['id']: origin for origin in [*before, *after]}
    return [{**_source(block_id), 'beforeHashes': old.get(block_id, []),
             'afterHashes': new.get(block_id, []), 'evidence': sources[block_id]['evidence']}
            for block_id in sorted(old.keys() | new.keys())
            if old.get(block_id) != new.get(block_id)]


def _first_message_difference(index: int, before, after, old_report: dict, new_report: dict) -> dict:
    old = before.field_bytes[index]['fields'] if index < len(before.field_bytes) else {}
    new = after.field_bytes[index]['fields'] if index < len(after.field_bytes) else {}
    old_origins = (old_report['messages'][index]['origins']
                   if index < len(old_report['messages']) else [])
    new_origins = (new_report['messages'][index]['origins']
                   if index < len(new_report['messages']) else [])
    local_changes = _changed_origins(old_origins, new_origins)
    affected = {origin['id'] for origin in local_changes}
    # Inserting/withdrawing one carrier shifts unchanged siblings. Compare
    # source versions across the whole artifact before naming them as changed.
    all_old = [origin for message in old_report['messages'] for origin in message['origins']]
    all_new = [origin for message in new_report['messages'] for origin in message['origins']]
    changed = [origin for origin in _changed_origins(all_old, all_new) if origin['id'] in affected]
    semantic = (index >= min(len(before.canonical), len(after.canonical))
                or before.canonical[index]['fields'] != after.canonical[index]['fields'])
    return {'index': index,
            'beforeKey': before.canonical[index]['key'] if index < len(before.canonical) else None,
            'afterKey': after.canonical[index]['key'] if index < len(after.canonical) else None,
            'fields': sorted(key for key in old.keys() | new.keys()
                                           if old.get(key) != new.get(key)),
            'semanticChange': semantic,
            'beforeOrigins': old_origins, 'afterOrigins': new_origins,
            'changedOrigins': changed,
            'attribution': ('observed_sections' if changed else
                            'carrier_layout' if local_changes else 'outside_observed_sections')}


def compare_context(before: dict, after: dict, *, wire: bool = False) -> dict:
    """Locate the first changed prior message and separately compare prefix regions.

    Uses the live diagnostic owner's canonical/field/region fingerprints. A pure
    append is not a prefix rewrite; shortening is not an unchanged prefix.
    Parsed JSON cannot prove original serialization, TTL, routing or cache usage.
    """
    old_report, new_report = inspect_context(before, wire=wire), inspect_context(after, wire=wire)
    before, after = request_document(before), request_document(after)
    old_messages, old_system, _ = _request_parts(before, wire)
    new_messages, new_system, _ = _request_parts(after, wire)
    old, new = (capture_wire_message_evidence(messages)
                for messages in (old_messages, new_messages))
    common = 0
    for left, right in zip(old.message_bytes, new.message_bytes):
        if left['h'] != right['h']:
            break
        common += 1
    if common < min(len(old_messages), len(new_messages)):
        change = 'prefix_changed'
    elif len(old_messages) > len(new_messages):
        change = 'history_shortened'
    elif len(old_messages) < len(new_messages):
        change = 'append_only'
    else:
        change = 'unchanged'
    old_regions, new_regions = (
        wire_byte_region(system, doc.get('tools'), cache_relevant_controls(doc))
        for doc, system in ((before, old_system), (after, new_system)))
    changed_regions = [key for key in old_regions if old_regions[key] != new_regions[key]]
    if before.get('model') != after.get('model'):
        changed_regions.append('model')
    if not wire and before.get('params') != after.get('params'):
        changed_regions.append('request_params')
    if ('messages' in before) != ('messages' in after) or (
            isinstance(before.get('input'), str) != isinstance(after.get('input'), str)):
        changed_regions.append('request_shape')
    return {
        'view': old_report['view'], 'cacheVerdict': 'not_evaluated',
        'messageChange': change, 'commonPrefixMessages': common,
        'beforeMessageCount': len(old_messages), 'afterMessageCount': len(new_messages),
        'firstMessageDifference': (_first_message_difference(common, old, new, old_report, new_report)
                                   if change in ('prefix_changed', 'history_shortened') else None),
        'changedRegions': changed_regions,
        'cacheMarkersChanged': marker_signature(before) != marker_signature(after),
        'systemChangedOrigins': _changed_origins(old_report['systemOrigins'], new_report['systemOrigins']),
        'lineage': {'before': old_report['lineage'], 'after': new_report['lineage']},
        'cacheObservations': {'before': old_report['cacheObservation'], 'after': new_report['cacheObservation']},
        'unlocatedEventReceipts': {'before': old_report['unlocatedEventReceipts'],
                                  'after': new_report['unlocatedEventReceipts']},
    }
