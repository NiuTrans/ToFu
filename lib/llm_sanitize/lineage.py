"""Content-free source maps owned by actual message transformations.

Seed from producer receipts, carry spans through explicit merges/replacements,
then remove all transient fields. Unknown projections lose their witness at the
named stage; equality or substring searches never invent occurrence identity.
No registry policy, provider I/O, persistence, cache verdict or global state.
"""
from copy import deepcopy
import hashlib

from lib.llm_sanitize.context_sections import context_sections

SPAN_FIELD = '_contextLineageSpans'
BODY_FIELD = '_context_lineage'


def _part_text(part):
    if isinstance(part, str):
        return part
    if isinstance(part, dict) and part.get('type') in ('text', 'input_text', 'output_text'):
        value = part.get('text')
        return value if isinstance(value, str) else None
    return None


def text_parts(content):
    parts = content if isinstance(content, list) else [content]
    return [(i, text) for i, part in enumerate(parts) if (text := _part_text(part)) is not None]


def span_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _location(message, part, start, end, *, region='messages'):
    return {'region': region, 'message': message, 'part': part,
            'start': start, 'end': end}


def observed_span(document, location):
    """Resolve one explicit source range; malformed or absent ranges are unknown."""
    if not isinstance(location, dict):
        return None
    region = location.get('region')
    part, start, end = (location.get(key) for key in ('part', 'start', 'end'))
    if any(type(value) is not int for value in (part, start, end)) or not 0 <= start <= end:
        return None
    if region in ('messages', 'input'):
        index, messages = location.get('message'), document.get(region)
        if (type(index) is not int or not isinstance(messages, list)
                or not 0 <= index < len(messages) or not isinstance(messages[index], dict)):
            return None
        content = messages[index].get('content')
    elif region in ('system', 'instructions'):
        content = document.get(region)
    else:
        return None
    parts = content if isinstance(content, list) else [content]
    text = _part_text(parts[part]) if 0 <= part < len(parts) else None
    return text[start:end] if isinstance(text, str) and end <= len(text) else None


def verify_lineage(trace, document, stage):
    """A transformation witness cannot survive an unobserved intervening edit."""
    for source in trace.get('sources', ()):
        if source.get('status') != 'located':
            continue
        text = observed_span(document, source.get('output'))
        if text is None or span_hash(text) != source.get('spanHash'):
            source.update(status='unmapped', lostAt=stage)
            source.pop('output', None)


def _receipt_hash(receipt):
    value = receipt.get('hash')
    return (value if isinstance(value, str) and len(value) == 16
            and all(char in '0123456789abcdef' for char in value) else None)


def _source_spans(message):
    content = message.get('content')
    sections = context_sections(content) if message.get('_contextComposer') else ()
    metadata = message.get('_contextBlocks')
    metadata = metadata if isinstance(metadata, list) else []
    for section in sections:
        yield {'id': section.id, 'hash': section.content_hash,
               'evidence': 'managed_carrier',
               'receiptHash': next((_receipt_hash(row) for row in metadata
                                    if isinstance(row, dict) and row.get('id') == section.id), None),
               'part': section.part, 'start': section.start, 'end': section.end}
    receipt = message.get('_contextInjection')
    if (isinstance(content, str) and isinstance(receipt, dict)
            and isinstance(receipt.get('id'), str) and receipt['id']
            and message.get('_contextInjectionId') == receipt['id']):
        yield {'id': receipt['id'],
               'eventId': receipt.get('eventId') if isinstance(receipt.get('eventId'), str) else None,
               'hash': span_hash(content)[:16], 'receiptHash': _receipt_hash(receipt),
               'evidence': 'event_receipt', 'part': 0, 'start': 0, 'end': len(content)}


class MessageLineage:
    """One builder's source map; only JSON hashes/positions survive finish()."""

    def __init__(self, original, cleaned):
        self.sources = []
        self._hashes = {}
        valid = [(i, msg) for i, msg in enumerate(original) if isinstance(msg, dict)]
        for (index, message), clean in zip(valid, cleaned):
            sources = list(_source_spans(message))
            if not sources:
                continue
            spans = []
            parts = dict(text_parts(message.get('content')))
            for source in sources:
                part, start, end = (source.pop(key) for key in ('part', 'start', 'end'))
                text = parts.get(part)
                if not isinstance(text, str):
                    continue
                digest = self._span_hash(text, start, end)
                spans.append({'source': len(self.sources), 'part': part,
                              'start': start, 'end': end, 'spanHash': digest})
                self.sources.append({**source, 'input': _location(index, part, start, end),
                                     'spanHash': digest, 'status': 'located'})
            if spans:
                clean[SPAN_FIELD] = spans
        self.observe('field_projection', cleaned)

    def _span_hash(self, text, start, end):
        # Request-local exact cache: immutable text is retained with its id, so
        # id reuse cannot certify another string. No cross-request lifetime.
        _, ranges = self._hashes.setdefault(id(text), (text, {}))
        key = (start, end)
        if key not in ranges:
            ranges[key] = span_hash(text[start:end])
        return ranges[key]

    def observe(self, stage, messages):
        if not self.sources:
            return
        seen = set()
        for index, message in enumerate(messages):
            if SPAN_FIELD not in message:
                continue
            parts = dict(text_parts(message.get('content')))
            for span in message.get(SPAN_FIELD, ()):
                ordinal = span['source']
                row = self.sources[ordinal]
                if row['status'] != 'located':
                    continue
                text = parts.get(span['part'])
                start, end = span['start'], span['end']
                if (not isinstance(text, str) or not 0 <= start <= end <= len(text)
                        or self._span_hash(text, start, end) != span['spanHash']):
                    continue
                seen.add(ordinal)
                if span['spanHash'] != row['spanHash']:
                    row.setdefault('changedAt', stage)
                row['spanHash'] = span['spanHash']
                row['output'] = _location(index, span['part'], start, end)
        for ordinal, row in enumerate(self.sources):
            if row['status'] == 'located' and ordinal not in seen:
                row.update(status='unmapped', lostAt=stage)
                row.pop('output', None)

    def finish(self, messages, stage='canonical'):
        self.observe(stage, messages)
        for message in messages:
            message.pop(SPAN_FIELD, None)
        return {'version': 1, 'stage': stage, 'sources': deepcopy(self.sources)}


def merge_spans(left, right, old_content, new_content):
    """Called by the same-role owner after it emits the actual concatenation."""
    left_spans = left.get(SPAN_FIELD, [])
    right_spans = right.get(SPAN_FIELD, [])
    if not left_spans and not right_spans:
        return
    shifted = []
    for span in right_spans:
        span = dict(span)
        if isinstance(old_content, list) or isinstance(new_content, list):
            span['part'] += len(old_content) if isinstance(old_content, list) else int(bool(old_content))
        else:
            offset = len(old_content) + (2 if old_content and new_content else 0)
            span['start'] += offset
            span['end'] += offset
        shifted.append(span)
    left[SPAN_FIELD] = [*left_spans, *shifted]


def replace_spans(spans, part, text, old, new):
    """Map the exact replacements that the gateway owner is about to perform."""
    edits = []
    cursor = 0
    while (start := text.find(old, cursor)) >= 0:
        edits.append((start, start + len(old)))
        cursor = start + len(old)
    for span in spans:
        if span['part'] != part or not span['spanHash']:
            continue
        start, end = span['start'], span['end']
        if any(a < start < b or a < end < b for a, b in edits):
            span['spanHash'] = ''
            continue
        delta = len(new) - len(old)
        span['start'] += delta * sum(b <= start for _, b in edits)
        span['end'] += delta * sum(b <= end for _, b in edits)
        span['spanHash'] = span_hash(text[start:end].replace(old, new))


class ProjectionLineage:
    """Protocol emitters map one known source message to its emitted content."""

    def __init__(self, body, stage):
        self.trace = deepcopy(body.get(BODY_FIELD) or {})
        verify_lineage(self.trace, body, 'canonical_ingress')
        self.stage = stage
        self.by_message = {}
        self.mapped = set()
        for ordinal, source in enumerate(self.trace.get('sources', ())):
            location = source.get('output') or {}
            if source.get('status') == 'located' and location.get('region') == 'messages':
                self.by_message.setdefault(location['message'], []).append((ordinal, source))

    def emit(self, source_index, source_content, target_content, *,
             message=None, region='messages', part_offset=0):
        sources = self.by_message.get(source_index)
        if not sources:
            return
        old_parts = text_parts(source_content)
        new_parts = text_parts(target_content)
        new_text = dict(new_parts)
        positions = {}
        if isinstance(target_content, str) and ''.join(text for _, text in old_parts) == target_content:
            offset = 0
            for part, text in old_parts:
                positions[part] = (0, offset)
                offset += len(text)
        elif len(old_parts) == len(new_parts):
            for (part, old), (target_part, new) in zip(old_parts, new_parts):
                if old == new:
                    positions[part] = (target_part, 0)
        for ordinal, source in sources:
            old = source['output']
            mapped = positions.get(old['part'])
            if mapped is None:
                continue
            part, offset = mapped
            text = new_text[part]
            start, end = old['start'] + offset, old['end'] + offset
            if span_hash(text[start:end]) != source['spanHash']:
                continue
            source['output'] = _location(message, part + part_offset, start, end, region=region)
            self.mapped.add(ordinal)

    def finish(self, output):
        if not self.trace:
            return
        for ordinal, source in enumerate(self.trace['sources']):
            if source['status'] == 'located' and ordinal not in self.mapped:
                source.update(status='unmapped', lostAt=self.stage)
                source.pop('output', None)
        self.trace['stage'] = self.stage
        output[BODY_FIELD] = self.trace
