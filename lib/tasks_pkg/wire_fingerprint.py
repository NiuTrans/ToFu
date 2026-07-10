# HOT_PATH — called once per round at send time (prepare_request tail).
"""Envelope-agnostic wire fingerprint for prompt-cache miss traceability.

Why this module exists
======================
``lib/tasks_pkg/cache_tracking.py::detect_cache_break`` used to attribute a
cache miss by ELIMINATION: if it saw no system/tools/model change and no
prefix-byte mutation *in the stored ``messages`` dict*, it blamed the server
("stochastic server-side cache miss"). But the bytes that actually reach the
server are produced much later — by ``build_body`` (sanitize chain, Claude
``reasoning_details`` replay, ``_downscale_oversized_images``), then
``add_cache_breakpoints`` (strips + re-adds ``cache_control``, moves the tail
marker), and finally — on the Anthropic-protocol path — ``openai_body_to_anthropic``
(rewrites ``tool_calls`` → ``tool_use`` and RE-SERIALIZES ``arguments`` via
``json.dumps(..., ensure_ascii=False)``). The detector never saw ANY of that,
so a genuinely CLIENT-caused miss (e.g. a JPEG re-encoded by the downscaler,
or an image retroactively shrunk when a 5th image arrived) got laundered into
"stochastic server".

This module canonicalises the FINAL, post-translation ``body['messages']`` —
in EITHER envelope (OpenAI Chat Completions shape OR the translated Anthropic
Messages shape) — into ONE envelope-agnostic list of per-message semantic
fingerprints. Anthropic prompt-caching matches the longest **tokenized-content**
prefix, NOT the raw request bytes: marker position, ``str`` vs
``[{"type":"text"}]`` wrapping, and ``arguments`` key ordering are all
invisible to the server's match. So the canonical form deliberately erases
exactly those (and ONLY those) so that a diff cries wolf only on a change the
server would actually see.

Canonicalisation rules (the load-bearing part)
==============================================
  * ``cache_control`` key            → dropped everywhere (cache metadata).
  * ``str`` content ↔ single ``{"type":"text","text":…}`` block
                                     → collapse to the same text token.
  * OpenAI ``tool_calls`` ↔ Anthropic ``tool_use``
                                     → same ``(id, name, canon_args)`` triple,
                                       where ``canon_args`` = parse-then-dump
                                       with ``sort_keys=True`` (kills the
                                       ensure_ascii re-serialisation false
                                       positive; a real arg change still shows).
  * OpenAI ``tool`` role ↔ Anthropic ``tool_result`` user block
                                     → same ``(tool_use_id, content)``.
  * thinking: OpenAI ``reasoning_content`` + ``thinking_signature`` ↔
    Anthropic ``{"type":"thinking",...}`` → same ``(thinking, signature)``.

Alignment key
=============
Downstream diffing MUST align on a STABLE per-message key, never the list
index — ``_merge_consecutive_same_role`` / ``_fix_orphaned_tool_calls``
reindex messages between rounds. ``canonical_key(entry)`` derives one from the
message's own identity (first tool id / tool_use_id / role+content hash).

Public API
==========
  - ``canonical_messages(messages) -> list[dict]`` — envelope-agnostic per-msg
    fingerprints (``{role, key, fields:{field→md5}, brief}``).
  - ``diff_canonical(old, new, max_report=8) -> list[str]`` — the exact
    ``key.field`` entries that changed (stable-key aligned).
  - ``static_prefix_hash(messages) -> str`` — hash of the leading
    system+first-user "static floor" (system+tools prefix proxy).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode('utf-8', errors='replace')).hexdigest()[:16]


def _canon_args(raw: Any) -> str:
    """Canonicalise a tool-call ``arguments`` payload.

    OpenAI keeps ``arguments`` as a JSON *string*; the Anthropic translation
    parses it and re-dumps with ``ensure_ascii=False`` (which can reorder keys
    and change escaping). Both must map to ONE canonical string so a mere
    re-serialisation is not flagged, while a genuine value change is. Parse to
    an object when possible, then dump with ``sort_keys=True``; fall back to
    the stripped raw string when it isn't valid JSON.
    """
    if isinstance(raw, (dict, list)):
        try:
            return json.dumps(raw, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.debug('[WireFP] _canon_args dict/list dump failed (%s) — '
                         'using str() form', e)
            return str(raw)
    s = '' if raw is None else str(raw)
    try:
        obj = json.loads(s or '{}')
        return json.dumps(obj, sort_keys=True, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.debug('[WireFP] _canon_args JSON re-canon failed (%s) — '
                     'using stripped raw', e)
        return s.strip()


def _text_of(content: Any) -> str:
    """Collapse ``str`` | block-list content to a single canonical text stream.

    Erases the ``str`` ↔ ``[{"type":"text","text":…}]`` wrapping distinction
    (a moving cache marker flips a message between the two shapes round-over-
    round, and the server does not care). Non-text blocks contribute a stable
    type/identity token (images by their data hash, so a re-encode DOES show).
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return '' if content is None else str(content)
    # ── str ↔ single-text-block equivalence ──
    # A moving cache marker wraps a message's bare ``str`` content into
    # ``[{"type":"text","text":…}]`` on the round it lands, and unwraps it the
    # round it moves off. The server prefix-matches the tokenized TEXT, which
    # is identical either way — so a lone text block must canonicalise to the
    # SAME token as the bare string (no ``\x02text\x03`` marker prefix), else
    # the flip cries wolf every round. Multi-block content keeps the per-block
    # markers so a genuine reorder / added block still shows.
    if (len(content) == 1 and isinstance(content[0], dict)
            and (content[0].get('type') == 'text'
                 or ('text' in content[0] and not content[0].get('type')))):
        return content[0].get('text') or ''
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get('type')
        if btype == 'text' or ('text' in block and not btype):
            parts.append('\x02text\x03' + (block.get('text') or ''))
        elif btype == 'image_url':
            url = (block.get('image_url') or {}).get('url', '')
            parts.append('\x02img\x03' + _md5(url))
        elif btype == 'image':
            src = block.get('source') or {}
            # base64 payload or url — hash whichever identifies the pixels, so
            # a downscale re-encode (new base64) is a REAL change here.
            ident = src.get('data') or src.get('url') or ''
            parts.append('\x02img\x03' + _md5(str(ident)))
        elif btype == 'tool_result':
            # Anthropic tool_result nested under a user turn — represent by its
            # id + inner content so it aligns with the OpenAI ``tool`` role.
            inner = block.get('content')
            parts.append('\x02toolresult\x03' + (block.get('tool_use_id') or '')
                         + '\x03' + _text_of(inner))
        elif btype == 'tool_use':
            parts.append('\x02tooluse\x03' + (block.get('id') or '') + '\x03'
                         + (block.get('name') or '') + '\x03'
                         + _canon_args(block.get('input')))
        elif btype == 'thinking':
            parts.append('\x02think\x03' + (block.get('thinking') or '')
                         + '\x03' + (block.get('signature') or ''))
        else:
            # Unknown block type: fold its stable JSON so a change still shows.
            try:
                parts.append('\x02' + str(btype) + '\x03'
                             + json.dumps({k: v for k, v in block.items()
                                           if k != 'cache_control'},
                                          sort_keys=True, ensure_ascii=False))
            except (TypeError, ValueError):
                parts.append('\x02' + str(btype) + '\x03' + str(block))
    return '\x01'.join(parts)


def _fields_of(msg: dict) -> dict[str, str]:
    """Per-field canonical hashes for ONE message, envelope-agnostic.

    Handles both the OpenAI shape (role tool / assistant.tool_calls /
    reasoning_content+thinking_signature) and the translated Anthropic shape
    (user turn holding tool_result blocks / assistant.content holding
    tool_use + thinking blocks). The output field set is the SAME regardless
    of envelope so a protocol switch alone never registers as a change.
    """
    role = msg.get('role', '')
    content = msg.get('content')

    # ── Tool RESULT — normalise BOTH envelopes to one representation ──
    # OpenAI carries it as a ``tool`` role message; the Anthropic translation
    # nests one-or-more ``tool_result`` blocks inside a ``user`` turn. Both
    # must produce the SAME (role token + tool_result hash) so a protocol
    # switch alone is not a change. Canonical form: role='toolresult', and the
    # tool_result hash = the ordered list of ``(tool_use_id, inner_text)``.
    _tr_specs: list[str] | None = None
    if role == 'tool':
        _tr_specs = [(msg.get('tool_call_id') or '') + '\x03'
                     + _text_of(content)]
    elif (role == 'user' and isinstance(content, list) and content
          and isinstance(content[0], dict)
          and content[0].get('type') == 'tool_result'):
        _tr_specs = []
        for blk in content:
            if isinstance(blk, dict) and blk.get('type') == 'tool_result':
                _tr_specs.append((blk.get('tool_use_id') or '') + '\x03'
                                 + _text_of(blk.get('content')))
    if _tr_specs is not None:
        return {'role': _md5('toolresult'),
                'tool_result': _md5('\x01'.join(_tr_specs))}

    fields: dict[str, str] = {'role': _md5(role)}

    # ── Assistant: DECOMPOSE either envelope into the SAME field set ──
    # OpenAI shape keeps text in ``content``, thinking in
    # ``reasoning_content``+``thinking_signature``, calls in ``tool_calls``.
    # The Anthropic translation packs ALL of them into ``content`` blocks
    # (thinking / text / tool_use). To make a protocol switch alone NOT count
    # as a change, pull the Anthropic blocks back out into the same three
    # fields the OpenAI shape uses.
    _text_parts: list[str] = []
    _think_text = msg.get('reasoning_content') or ''
    _think_sig = msg.get('thinking_signature') or ''
    _tool_specs: list[str] = []  # (id, name, canon_args) per call, any envelope

    for tc in msg.get('tool_calls') or ():
        if isinstance(tc, dict):
            fn = tc.get('function') or {}
            _tool_specs.append((tc.get('id') or '') + '\x03'
                               + (fn.get('name') or '') + '\x03'
                               + _canon_args(fn.get('arguments')))

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                _text_parts.append(str(block))
                continue
            btype = block.get('type')
            if btype == 'thinking':
                _think_text = _think_text or (block.get('thinking') or '')
                _think_sig = _think_sig or (block.get('signature') or '')
            elif btype == 'tool_use':
                _tool_specs.append((block.get('id') or '') + '\x03'
                                   + (block.get('name') or '') + '\x03'
                                   + _canon_args(block.get('input')))
            else:
                # text / image / other → the canonical text stream
                _text_parts.append(_text_of([block]))
    else:
        _t = _text_of(content)
        if _t:
            _text_parts.append(_t)

    _txt = '\x01'.join(p for p in _text_parts if p)
    if _txt:
        fields['content'] = _md5(_txt)
    if _tool_specs:
        fields['tool_calls'] = _md5('\x01'.join(_tool_specs))
    if _think_text or _think_sig:
        # NOTE: ``reasoning_details`` is deliberately NOT a separate field.
        # build_body synthesises it FROM ``reasoning_content`` +
        # ``thinking_signature`` (which the ``thinking`` field already covers),
        # and the Anthropic envelope carries a ``thinking`` block instead — so
        # hashing ``reasoning_details`` on its own would make the two envelopes
        # diverge for the identical turn. The thinking text+signature is the
        # canonical, envelope-independent representation.
        fields['thinking'] = _md5(_think_text + '\x03' + _think_sig)

    return fields


def _brief(msg: dict) -> str:
    """Short human token for a message (for readable diff output)."""
    role = msg.get('role', '?')
    if role == 'tool':
        return f'tool_result({(msg.get("tool_call_id") or "")[:10]})'
    content = msg.get('content')
    if isinstance(content, list) and content and isinstance(content[0], dict):
        t0 = content[0].get('type')
        if t0 == 'tool_result':
            # Same brief shape as the OpenAI ``tool`` role above, keyed on the
            # FIRST tool_use_id, so both envelopes align on the same key.
            return f'tool_result({(content[0].get("tool_use_id") or "")[:10]})'
    tcs = msg.get('tool_calls') or ()
    if tcs and isinstance(tcs[0], dict):
        return f'{role}/tool_call({((tcs[0].get("function") or {}).get("name") or "?")})'
    txt = _text_of(content)
    return f'{role}({txt[:24]!r})'


def canonical_key(entry: dict) -> str:
    """Stable per-message alignment key (NOT the list index).

    ``_merge_consecutive_same_role`` / ``_fix_orphaned_tool_calls`` reindex
    messages, so index-based diffing explodes. Key off the message's own
    identity: a tool id when present (most stable), else role + a hash of the
    canonical field set.
    """
    role = entry.get('role', '')
    fields = entry.get('fields') or {}
    # Prefer a tool identity if the brief captured one.
    brief = entry.get('brief', '')
    if 'tool_result(' in brief or 'tool_call(' in brief or 'tool_use(' in brief:
        return brief
    return f'{role}:' + _md5(json.dumps(fields, sort_keys=True))


def canonical_messages(messages: list) -> list[dict]:
    """Canonicalise a post-translation wire message list to fingerprints.

    Each entry: ``{'role', 'key', 'fields': {field: md5}, 'brief'}``. Envelope
    (OpenAI vs Anthropic) is erased — the same conversation produces the same
    fingerprints on either protocol.
    """
    out: list[dict] = []
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        entry = {
            'role': msg.get('role', ''),
            'fields': _fields_of(msg),
            'brief': _brief(msg),
        }
        entry['key'] = canonical_key(entry)
        out.append(entry)
    return out


def diff_canonical(old: list, new: list, max_report: int = 8) -> list[str]:
    """Name the exact ``key.field`` entries that differ, stable-key aligned.

    Compares the OVERLAPPING prefix by position but reports the STABLE key +
    field, so a re-index shifts nothing as long as the same messages are
    present in the same order. A message present in one list but absent in the
    other (an actual prefix edit) is reported as ``+key`` / ``-key``. Capped at
    ``max_report`` culprits (an ``…`` marks truncation).
    """
    changes: list[str] = []
    n = min(len(old), len(new))
    for i in range(n):
        o = old[i] or {}
        nw = new[i] or {}
        of = o.get('fields') or {}
        nf = nw.get('fields') or {}
        key = nw.get('key') or o.get('key') or f'[{i}]'
        for field in sorted(set(of) | set(nf)):
            if of.get(field) != nf.get(field):
                changes.append(f'{key}.{field}')
                if len(changes) >= max_report:
                    changes.append('…')
                    return changes
    if len(old) != len(new):
        changes.append(f'len {len(old)}\u2192{len(new)}')
    return changes


def static_prefix_hash(messages: list) -> str:
    """Hash the leading static floor (system message(s) + first user turn).

    A proxy for "the system+tools prefix that reads back even on a body miss".
    Used to distinguish "whole prefix evicted" from "only the body past the
    static floor was not reused".
    """
    parts: list[str] = []
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        role = msg.get('role', '')
        if role == 'system':
            parts.append('sys\x03' + _text_of(msg.get('content')))
        elif role == 'user':
            parts.append('user\x03' + _text_of(msg.get('content')))
            break  # stop at the first user turn — that's the static floor
        else:
            break
    return _md5('\x01'.join(parts))
