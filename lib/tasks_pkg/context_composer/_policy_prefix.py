"""Static platform-policy prefix; runtime facts stay in their chronological lane.

Only the registered platform_static producer can use this boundary. Migration
edits request-local composer envelopes, never user text or durable Turns.
"""
from __future__ import annotations

import hashlib
import re

from .registry import block_spec
from ._sections import _canonical_section, _strip_to_body

_STATIC = 'platform_static'
# Match outer producer envelopes first: a static-looking quote inside a project
# or memory envelope is data, not another producer. The stored digest below is
# additionally required before any content can move into a native policy role.
_ENVELOPE = re.compile(
    r'<!-- tofu-context:([^:\n]+):start -->.*?'
    r'<!-- tofu-context:\1:end -->', re.DOTALL)


def is_static_policy(block) -> bool:
    spec = block_spec(block.id)
    return bool(spec and spec.placement == 'system' and block.source == spec.source)


def policy_carrier(rows: list[dict]) -> dict:
    return {
        'role': 'system', '_isMeta': True, '_contextComposer': True,
        'carrier': 'system_context',
        'content': [{'type': 'text', 'text': row['_rendered']} for row in rows],
        '_contextBlocks': [
            {key: row[key] for key in ('id', 'hash', 'stability', 'lifecycle')}
            for row in rows],
    }


def insert_policy(messages: list[dict], carrier: dict) -> None:
    index = next((i for i, m in enumerate(messages)
                  if m.get('role') not in ('system', 'developer')), len(messages))
    messages.insert(index, carrier)


def _verified_spans(message: dict) -> dict:
    """Map part index to exact owned spans; no digest means no role promotion."""
    if not message.get('_contextComposer') or not message.get('_isMeta'):
        return {}
    rows = [row for row in message.get('_contextBlocks') or []
            if isinstance(row, dict) and row.get('id') == _STATIC]
    digests = {row.get('hash') for row in rows if row.get('hash')}
    content = message.get('content')
    parts = content if isinstance(content, list) else [content]
    spans = {}
    for index, part in enumerate(parts):
        text = (part.get('text') if isinstance(part, dict)
                and part.get('type') == 'text' else part)
        if not isinstance(text, str):
            continue
        # Digest evidence uses the wrapper-independent section hash: the
        # producer stores canonical-section digests in ``_contextBlocks``,
        # so the on-wire span (standalone or amalgamated envelope) must be
        # reduced to the same form before comparison.
        matches = [match for match in _ENVELOPE.finditer(text)
                   if match.group(1) == _STATIC
                   and hashlib.sha256(
                       _canonical_section(
                           _STATIC, _strip_to_body(match.group(0)))
                       .encode('utf-8')).hexdigest()[:16]
                   in digests]
        if matches:
            spans[index] = matches
    return spans


def migrate_policy_prefix(messages: list[dict]) -> None:
    """Move verified static envelopes once, without changing quoted bytes.

    The latest verified policy wins. Missing/mismatched producer evidence fails
    closed; normal collection may add current policy but cannot promote a quote.
    Already-dedicated prefix policy is a no-op for cache stability.
    """
    owned = {index: spans for index, message in enumerate(messages)
             if (spans := _verified_spans(message))}
    if not owned:
        return
    last_index = next(reversed(owned))
    rendered = next(reversed(owned[last_index].values()))[-1].group(0)
    message = messages[last_index]
    policy_end = next((i for i, m in enumerate(messages)
                       if m.get('role') not in ('system', 'developer')), len(messages))
    if (len(owned) == 1 and last_index < policy_end
            and message.get('role') == 'system'
            and message.get('content') in (rendered, [{'type': 'text', 'text': rendered}])):
        return
    normalized = []
    for index, message in enumerate(messages):
        if index not in owned:
            normalized.append(message)
            continue
        content = message.get('content')
        parts = content if isinstance(content, list) else [content]
        kept = []
        for part_index, part in enumerate(parts):
            matches = owned[index].get(part_index)
            if not matches:
                kept.append(part)
                continue
            text = part['text'] if isinstance(part, dict) else part
            for match in reversed(matches):
                text = text[:match.start()] + text[match.end():]
            if text:
                kept.append({**part, 'text': text} if isinstance(part, dict) else text)
        if kept:
            normalized.append({**message,
                'content': kept if isinstance(content, list) else ''.join(kept),
                '_contextBlocks': [r for r in message['_contextBlocks']
                                   if not isinstance(r, dict) or r.get('id') != _STATIC]})
    insert_policy(normalized, policy_carrier([{
        'id': _STATIC,
        'hash': hashlib.sha256(
            _canonical_section(_STATIC, _strip_to_body(rendered))
            .encode('utf-8')).hexdigest()[:16],
        'stability': 'static', 'lifecycle': 'conversation', '_rendered': rendered}]))
    messages[:] = normalized
