"""Wrapper-independent section identity for rendered context blocks.

A block's bytes can ride a standalone ``<system-reminder>`` envelope or an
amalgamated same-authority wrapper on-wire; only the body varies, so reuse
and policy-migration digests both hash the canonical section form below.
Single source of truth: ``_render`` (fresh renders + reuse scan) and
``_policy_prefix`` (migration digest evidence) must never drift apart.
"""
from __future__ import annotations

import re


def _canonical_section(block_id: str, body: str) -> str:
    """The wrapper-independent identity of one block's rendered bytes.

    Reuse compares this form on both sides: ``_managed_block_hashes`` strips
    whatever ``<system-reminder>``/``[Context authority…]``/``[source: …]``
    decoration a span carries on-wire down to this body-only section, and the
    renderer hashes the same section for a freshly rendered block.  Only the
    body varies; the reminder/authority/source lines are deterministic from
    the block, so they carry no reuse signal.  This is what lets an amalgamated
    block (no per-block reminder on-wire) still hash-match its next render.
    """
    return (f'<!-- tofu-context:{block_id}:start -->\n'
            f'{body}\n'
            f'<!-- tofu-context:{block_id}:end -->')


def _strip_to_body(span: str) -> str:
    """Reduce one on-wire ``tofu-context`` span to its body text.

    Handles both the standalone ``<system-reminder>``-wrapped envelope and the
    amalgamated section (``[source: …]`` line, no reminder).  Unknown shapes
    return the raw span so a foreign/legacy span never collides by accident.
    """
    match = re.match(
        r'<!-- tofu-context:[^:\n]+:start -->\n(.*?)\n'
        r'<!-- tofu-context:[^:\n]+:end -->',
        span.strip(), re.DOTALL)
    if not match:
        return span
    inner = match.group(1)
    reminder = re.match(
        r'<system-reminder>\n\[Context authority: [^\]]+\]\n(.*)\n'
        r'</system-reminder>', inner, re.DOTALL)
    if reminder:
        return reminder.group(1)
    source_line = re.match(r'\[source: [^\]]+\]\n(.*)', inner, re.DOTALL)
    if source_line:
        return source_line.group(1)
    return inner


def remove_managed_section(message: dict, block_id: str) -> dict | None:
    """Withdraw an owned section without mutating a shared source dictionary.

    Legacy carriers may amalgamate sections or split their text across parts.
    Retain complete sibling sections rather than looking for the removed
    section's terminator: compaction may have damaged that delimiter. If a
    sibling cannot be recovered, leave ownership intact rather than silently
    stripping metadata and orphaning its text. New checklist carriers are
    separate, so this mixed-carrier path is compatibility-only.
    """
    metadata = message.get('_contextBlocks') or []
    siblings = [row for row in metadata if row.get('id') != block_id]
    if not siblings:
        return None
    content = message.get('content')
    parts = content if isinstance(content, list) else []
    text = content if isinstance(content, str) else ''.join(
        part['text'] for part in parts
        if isinstance(part, dict) and isinstance(part.get('text'), str))
    spans = []
    for row in siblings:
        sibling_id = re.escape(row['id'])
        match = re.search(r'<!-- tofu-context:' + sibling_id + r':start -->.*?'
                          r'<!-- tofu-context:' + sibling_id + r':end -->',
                          text, re.DOTALL)
        if match is None:
            return message
        spans.append((match.start(), match.group(0)))
    kept_text = '\n'.join(span for _, span in sorted(spans))
    kept_content = kept_text if isinstance(content, str) else [
        {'type': 'text', 'text': kept_text},
        *(part for part in parts if not (isinstance(part, dict)
                                        and isinstance(part.get('text'), str))),
    ]
    return {**message, 'content': kept_content, '_contextBlocks': siblings}
