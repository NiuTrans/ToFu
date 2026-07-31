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


def _tool_id_token(tool_id) -> str:
    """Return a short but DISCRIMINATING token for a tool-call id.

    This token is not merely cosmetic: ``marker_signature`` uses the ``_brief``
    string as a per-message SLOT KEY, and ``markers_ttl_flipped`` compares each
    slot's ttl value set across rounds. Two distinct tool_results sharing a slot
    key therefore merge into one slot whose value set holds BOTH markers' ttls.

    A fixed ``id[:10]`` prefix did exactly that on AWS Bedrock, whose ids are
    shaped ``toolu_bdrk_01ABC…`` — the first 10 chars are the vendor prefix, so
    EVERY tool_result collapsed onto ``tool_result(toolu_bdrk)``. The stable mid
    marker (ttl='1h') and the rolling tail marker (ttl='') then appeared as one
    slot valued ``{'1h',''}``, and the tail's normal advance read as a ttl VALUE
    flip — 358 false client-caused verdicts in one 7-day window, which also
    suppressed the honest "server-side" verdict those rounds deserved.

    Keying on the id's TAIL keeps the token short while carrying the random,
    discriminating part for both vendor shapes (Anthropic ``toolu_01ABC…`` and
    Bedrock ``toolu_bdrk_01ABC…``).
    """
    s = str(tool_id or '')
    return s[-12:] if len(s) > 12 else s


def _brief(msg: dict) -> str:
    """Short human token for a message (for readable diff output)."""
    role = msg.get('role', '?')
    if role == 'tool':
        return f'tool_result({_tool_id_token(msg.get("tool_call_id"))})'
    content = msg.get('content')
    if isinstance(content, list) and content and isinstance(content[0], dict):
        t0 = content[0].get('type')
        if t0 == 'tool_result':
            # Same brief shape as the OpenAI ``tool`` role above, keyed on the
            # FIRST tool_use_id, so both envelopes align on the same key.
            return f'tool_result({_tool_id_token(content[0].get("tool_use_id"))})'
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


def first_changed_index(old: list, new: list) -> int:
    """Return the FIRST position where two canonical fingerprint lists differ.

    Position-aware companion to ``diff_canonical`` (which reports stable keys,
    not indices). Used by ``detect_cache_break`` to log WHERE in the wire
    message list a prefix mutation lands, so a break can be classified by
    whether the earliest changed index falls inside the PRIOR round's cached
    prefix (an already-cached message rewritten in place) vs only in the fresh
    tail. Compares the overlapping prefix by position; ``-1`` if no shared
    position differs. A pure length change (no shared-position diff) also
    returns ``-1`` — the caller inspects the length separately.
    """
    n = min(len(old), len(new))
    for i in range(n):
        o = old[i] or {}
        nw = new[i] or {}
        if (o.get('fields') or {}) != (nw.get('fields') or {}):
            return i
    return -1


def first_changed_byte_index(old: list, new: list) -> int:
    """FIRST position where two ``wire_byte_prefix`` lists differ by RAW bytes.

    The TRUE-byte counterpart of ``first_changed_index`` (which walks the LOSSY
    ``canonical_messages`` fingerprints). ``detect_cache_break`` needs the
    changed POSITION to log whether a mutation landed inside the prior round's
    cached-prefix boundary (an already-cached message rewritten in place → real
    miss) or only in the freshly-appended tail (benign). But when the only
    culprit is a ``<bytes>`` divergence (``canonical`` says "identical", raw
    bytes differ — a ``reasoning_details`` rebuild, same-role merge, or
    protocol switch), ``first_changed_index`` returns ``-1`` and the position
    evidence collapses to a meaningless ``inside_prior_cached_prefix=False`` —
    exactly the class where the position matters most. This walks the
    ``[{'key','h'}]`` byte-hash lists so the byte-only case gets an honest
    index. Compares the overlapping prefix by position; ``-1`` if no shared
    position differs (a pure length change is inspected separately).
    """
    n = min(len(old), len(new))
    for i in range(n):
        o = old[i] or {}
        nw = new[i] or {}
        if o.get('h') != nw.get('h'):
            return i
    return -1


def system_fingerprint(system: Any, tools: Any) -> dict[str, str]:
    """Fingerprint the top-level ``system`` field + tool schemas.

    THE INSTRUMENT-BLINDSPOT FIX. ``canonical_messages`` only sees
    ``body['messages']`` — but on the Anthropic path ``openai_body_to_anthropic``
    HOISTS the system prompt out of ``messages`` into the top-level ``system``
    field (a ``str`` or a list of ``{type:text,text}`` blocks). So a per-turn
    change to anything assembled INTO the system prompt (the cross-conversation
    digest, the charter, the board — the 29298-pin bug) was invisible to the
    detector, which then mislabeled the miss ``server-side — PROVEN``. This
    hashes exactly that hoisted region (and the tool schemas, the other cached
    prefix segment) so ``detect_cache_break`` can see a system-block mutation
    and STOP laundering it into "server-side".

    ``cache_control`` markers are stripped (cache metadata the server ignores);
    the str↔block wrapping is collapsed via ``_text_of`` so a marker flip alone
    is not a false positive — mirroring the message canonicalisation rules.

    Returns ``{'system': md5, 'tools': md5}`` (fields absent → hash of '').
    """
    # System: collapse str | block-list to the canonical text stream.
    if system is None:
        _sys_text = ''
    elif isinstance(system, str):
        _sys_text = system
    elif isinstance(system, list):
        _sys_text = _text_of(system)
    else:
        _sys_text = str(system)

    # Tools: hash the ordered (name, description, canonical-params) triples,
    # ignoring cache_control. A tool add/remove/reorder or a schema edit shows;
    # a marker move does not.
    _tool_specs: list[str] = []
    for t in tools or ():
        if not isinstance(t, dict):
            continue
        fn = t.get('function') if isinstance(t.get('function'), dict) else t
        name = fn.get('name') or ''
        desc = fn.get('description') or ''
        params = fn.get('parameters')
        try:
            _pj = json.dumps(params, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            _pj = str(params)
        _tool_specs.append(name + '\x03' + desc + '\x03' + _pj)

    return {'system': _md5(_sys_text),
            'tools': _md5('\x01'.join(_tool_specs))}


def marker_signature(body: dict) -> dict[str, Any]:
    """Fingerprint WHERE the ``cache_control`` breakpoints sit in the final
    wire body — the ONE thing ``canonical_messages`` deliberately erases.

    ``canonical_messages`` strips ``cache_control`` (it proves whether the
    tokenized CONTENT bytes changed). But a cache miss can be caused purely by
    the breakpoints moving / disappearing while the content is byte-identical —
    exactly the "tail breakpoint lost in the anthropic translation on a
    tool-ending round" bug. When that happens the content fingerprint says
    "identical" and the detector would wrongly assert "server-side PROVEN".

    This captures the marker LAYOUT so ``detect_cache_break`` can tell a
    genuine byte-identical-AND-same-markers round (→ provably server-side) from
    a same-content-but-markers-moved round (→ client-caused). Returns::

        {'count': N,                       # total cache_control markers
         'msg': [(align_key, block_ord), …],  # per-message-block marker slots
         'sys': K, 'tools': K,             # markers on hoisted system / tools
         'ttls': [(slot_key, ttl), …]}     # per-marker ttl VALUE (sorted)

    The per-message slot uses the stable ``canonical_key`` (not the list index)
    so a benign reindex does not register as a move.

    ``ttls`` fingerprints each marker's ``cache_control.ttl`` VALUE (``''`` for
    a bare ``{'type':'ephemeral'}`` = 5-minute default, ``'1h'`` for extended).
    The ttl is part of the gateway's cache key, so a stable-block ttl flip
    (5m↔1h) re-keys the whole prefix even when the body bytes AND the marker
    count/position are unchanged — the ``_task_id``-drop latch bypass. It is
    keyed by the marker's STABLE slot (``sys``/``tools``/the per-message
    ``canonical_key``, NOT the list index) and SORTED, so the rolling tail
    marker's normal forward advance does not perturb it — only an actual ttl
    VALUE change on an otherwise-stable slot does. ``markers_ttl_flipped``
    consumes this; ``markers_regressed`` deliberately ignores it (a ttl flip is
    not a breakpoint LOSS).
    """
    sig: dict[str, Any] = {'count': 0, 'msg': [], 'sys': 0, 'tools': 0,
                           'ttls': [], 'msg_blocks': [], 'body_msg_blocks': []}
    if not isinstance(body, dict):
        return sig

    _ttls: list = []

    def _count_cc(blocks, slot: str) -> int:
        n = 0
        if isinstance(blocks, list):
            for b in blocks:
                if isinstance(b, dict) and isinstance(b.get('cache_control'), dict):
                    _ttls.append((slot, b['cache_control'].get('ttl', '')))
                    n += 1
        return n

    # Messages (both envelopes: OpenAI shape + translated Anthropic shape).
    # ``_cum_blocks`` tracks each message-marker's CUMULATIVE content-block
    # position in Anthropic's block space — the unit its ~20-block cache
    # lookback is measured in. ``mid_anchor_out_of_window`` uses these positions
    # to tell when the rolling tail can no longer reach the mid stepping-stone
    # (the sawtooth whole-prefix rewrite). A block ≈ one content entry; an
    # assistant ``tool_calls`` turn also emits one tool_use block per call.
    _cum_blocks = 0
    for msg in body.get('messages') or ():
        if not isinstance(msg, dict):
            _cum_blocks += 1
            continue
        content = msg.get('content')
        _blocks_here = (len(content) if isinstance(content, list)
                        else (1 if content else 0))
        if isinstance(msg.get('tool_calls'), list):
            _blocks_here += len(msg['tool_calls'])
        _blocks_here = max(1, _blocks_here)
        if not isinstance(content, list):
            _cum_blocks += _blocks_here
            continue
        marked = [bi for bi, b in enumerate(content)
                  if isinstance(b, dict) and b.get('cache_control')]
        if marked:
            entry = {'role': msg.get('role', ''),
                     'fields': _fields_of(msg), 'brief': _brief(msg)}
            key = canonical_key(entry)
            # The system prompt sits at ``messages[0]`` on the OpenAI-protocol
            # wire path (system is NOT hoisted out to a top-level field there),
            # so its cache_control marker lands at cumulative block 0. That
            # HEAD marker is the static-prefix anchor, NOT part of the mid→tail
            # stepping-stone span. Including it in the span made
            # ``mid_anchor_out_of_window`` compute max(msg_blocks)-min()=tail-0
            # — always > lookback on any long conversation — a false positive
            # unrelated to the real mid→tail geometry. ``body_msg_blocks`` keeps
            # only the BODY (non-system) message markers so the predicate
            # measures the true mid→tail span.
            _is_head = msg.get('role') == 'system'
            for bi in marked:
                sig['msg'].append((key, bi))
                sig['msg_blocks'].append(_cum_blocks + bi)
                if not _is_head:
                    sig['body_msg_blocks'].append(_cum_blocks + bi)
                sig['count'] += 1
                _cc = content[bi].get('cache_control')
                if isinstance(_cc, dict):
                    _ttls.append((f'msg:{key}', _cc.get('ttl', '')))
        _cum_blocks += _blocks_here

    # Hoisted system (Anthropic path: list of blocks) + tools.
    system = body.get('system')
    if isinstance(system, list):
        _s = _count_cc(system, 'sys')
        sig['sys'] = _s
        sig['count'] += _s
    for t in body.get('tools') or ():
        if isinstance(t, dict):
            fn = t.get('function') if isinstance(t.get('function'), dict) else t
            if isinstance(fn, dict) and isinstance(fn.get('cache_control'), dict):
                sig['tools'] += 1
                sig['count'] += 1
                _ttls.append(('tools', fn['cache_control'].get('ttl', '')))
    # Sort by slot key so the list is order-stable across a benign reindex /
    # tail advance; a genuine ttl VALUE change on a slot still shows.
    sig['ttls'] = sorted(_ttls, key=lambda p: str(p[0]))
    return sig


def markers_regressed(prev: dict | None, cur: dict | None) -> bool:
    """True if breakpoints were LOST between rounds — the precise, false-
    positive-free signal that a cache miss is client-caused, not server-side.

    Why NOT a full position set-diff: the rolling TAIL breakpoint (and the
    quantized MID stepping-stone on its jump rounds) legitimately MOVE forward
    every round. A naive "any marker moved" test would fire on every single
    round and permanently disable the honest "server-side PROVEN" verdict. That
    move does not cause a miss — the new tail reads back through the prior
    entry — so it must NOT count.

    What DOES count is a breakpoint DISAPPEARING: the total marker count
    dropping, or the system/tools marker count changing, between rounds. That
    is exactly the "tail breakpoint silently lost in the anthropic translation
    on a tool-ending round" bug — the code intended to place N markers but the
    final wire body carries fewer. In steady state the count is constant
    (system + tool + mid + tail = 4), so a decrease is unambiguous.
    """
    if prev is None or cur is None:
        return False
    if cur.get('count', 0) < prev.get('count', 0):
        return True
    if prev.get('sys', 0) != cur.get('sys', 0):
        return True
    if prev.get('tools', 0) != cur.get('tools', 0):
        return True
    return False


def markers_ttl_flipped(prev: dict | None, cur: dict | None) -> bool:
    """True if a cache_control marker's ttl VALUE changed between rounds while
    the marker layout was otherwise stable — a CLIENT-caused cache reset.

    The companion to ``markers_regressed``: that catches a breakpoint being
    LOST (count/sys/tools drop); THIS catches a breakpoint's ttl VALUE flipping
    (5m↔1h) with the count unchanged. The ttl is part of the gateway's cache
    key, so a stable-block ttl flip re-keys the whole prefix on a byte-identical
    body — the ``_task_id``-drop latch bypass, which would otherwise launder
    into a false "byte-identical → server-side" verdict. Compares the
    slot-keyed, sorted ``ttls`` lists ``marker_signature`` emits.

    Why this is false-positive-free on the rolling tail: ``ttls`` is keyed by
    the marker's STABLE slot (``sys`` / ``tools`` / per-message
    ``canonical_key``), so the tail marker's normal forward advance changes
    WHICH message carries it (a new slot key) but not any existing slot's ttl.
    A pure add/remove of a slot is already a COUNT change caught by
    ``markers_regressed``; here we only fire when the MULTISET of (slot, ttl)
    pairs differs in a way that is not merely a count change — i.e. an existing
    slot's ttl value flipped. A missing side (mid-deploy / non-Claude / a
    pre-fix signature with no ``ttls`` key) is inert.
    """
    if prev is None or cur is None:
        return False
    _p = prev.get('ttls')
    _c = cur.get('ttls')
    # Pre-fix signature (no ttls captured) → inert, never cry wolf.
    if _p is None or _c is None:
        return False
    # Compare the DISTINCT ttl VALUE SET per slot, on slots PRESENT IN BOTH
    # rounds. A value flip on a surviving slot is the signal; an added/removed
    # slot is a count change (markers_regressed's job).
    #
    # ★ SET, not multiset. Every marker within a slot carries the SAME ttl in
    #   normal operation (all `sys` markers use `_cc_stable`; `tools` is one
    #   marker; each `msg:<canonical_key>` slot is one unique message with one
    #   marker), so a slot's value set is homogeneous. The ONLY way a multiset
    #   compare differed from a set compare was a pure COUNT change with the
    #   value unchanged — e.g. the `sys` slot going ['1h','1h']→['1h'] when the
    #   mid-history anchor arms as a conversation grows and `_system_bp_budget`
    #   drops 2→1. That is NOT a ttl flip (no value changed) yet the old
    #   multiset compare cried wolf → a spurious <ttl-flip> "cache re-keyed"
    #   verdict on a byte-changed round, mislabelling a real miss as an in-task
    #   latch bypass. Comparing distinct-value sets makes that count change
    #   inert while a genuine 1h↔5m VALUE flip on a slot ({'1h'} vs {''}) still
    #   fires — matching this function's own documented intent ("an existing
    #   slot's ttl value flipped").
    _pm: dict[str, set] = {}
    for slot, ttl in _p:
        _pm.setdefault(slot, set()).add(str(ttl))
    _cm: dict[str, set] = {}
    for slot, ttl in _c:
        _cm.setdefault(slot, set()).add(str(ttl))
    for slot in set(_pm) & set(_cm):
        if _pm[slot] != _cm[slot]:
            return True
    return False


def mid_anchor_out_of_window(cur: dict | None,
                             lookback: int = 20) -> bool:
    """True when the mid-history stepping-stone breakpoint sits FARTHER than
    Anthropic's ~``lookback``-block cache window behind the rolling tail — so
    the tail can no longer extend the mid entry and the whole prefix past the
    mid is re-written on a byte-identical body.

    Reads the cumulative message-marker BLOCK positions ``marker_signature``
    records in ``msg_blocks``. The two message-level markers are the mid anchor
    (earliest) and the rolling tail (latest); their block span is what must stay
    within the lookback. This is a CLIENT-side breakpoint-LAYOUT miss (the
    add_cache_breakpoints trail/step params let the stone slip past the window),
    NOT a server/gateway fault — detect_cache_break names it so a periodic
    read-collapse on a byte-identical, same-routing round is never laundered
    into "upstream cache miss". Inert when the fingerprint is missing / pre-fix
    (no ``msg_blocks``) or fewer than 2 message markers exist (no mid armed).
    """
    if not isinstance(cur, dict):
        return False
    # Measure the BODY (non-system) marker span. The system-prompt marker sits
    # at cumulative block 0 on the OpenAI-protocol wire path (system stays at
    # ``messages[0]``, not hoisted), so including it makes the span
    # tail-minus-0 \u2014 always past the lookback on any long conversation, a false
    # positive that has nothing to do with the mid\u2192tail stepping-stone geometry.
    # ``body_msg_blocks`` (added alongside ``msg_blocks``) excludes that HEAD
    # marker so the predicate reflects the true mid\u2192tail reach. Fall back to
    # the legacy ``msg_blocks`` only when the newer field is absent (a signature
    # captured before this fix, e.g. mid-deploy).
    blocks = cur.get('body_msg_blocks')
    if not isinstance(blocks, list):
        blocks = cur.get('msg_blocks')
    if not isinstance(blocks, list) or len(blocks) < 2:
        return False
    return (max(blocks) - min(blocks)) > lookback


def _strip_cache_control(obj: Any) -> Any:
    """Recursively drop ``cache_control`` keys for raw-byte hashing.

    ``cache_control`` is the ONE legitimately-mobile element in the wire body
    (the rolling tail marker moves every round, tracked separately by
    ``marker_signature``). Stripping it keeps ``wire_byte_prefix`` from crying
    wolf on the normal marker advance while still hashing EVERYTHING else byte
    for byte.
    """
    if isinstance(obj, dict):
        return {k: _strip_cache_control(v) for k, v in obj.items()
                if k != 'cache_control'}
    if isinstance(obj, list):
        return [_strip_cache_control(v) for v in obj]
    return obj


def wire_byte_prefix(messages: list) -> list[dict]:
    """Per-message hash of the ACTUAL serialized wire bytes — the TRUE-byte
    instrument that closes the "canonical says identical but the bytes weren't"
    laundering path.

    ``canonical_messages`` is deliberately LOSSY: it strips ``cache_control``,
    collapses ``str`` ↔ ``[{type:text}]``, canonicalises tool-arg key order,
    and DOES NOT hash ``reasoning_details`` (``build_body`` synthesises that
    field from ``reasoning_content`` + ``thinking_signature``, so canonical
    uses those instead). Therefore "canonical identical" does NOT imply "the
    bytes on the wire were identical". A round can rebuild ``reasoning_details``,
    merge consecutive same-role turns, or reorder fields while canonical reports
    "identical" — and a verdict that then asserts *"bytes were byte-identical"*
    would be literally false, laundering a real content/serialization change
    into an "upstream eviction".

    This hashes ``json.dumps(msg)`` of each message EXACTLY as the transport
    serialises it (insertion order preserved via ``sort_keys=False``), stripping
    ONLY ``cache_control``. Every other byte — ``reasoning_details``,
    ``extra_content``, field order, the full base64 of an image — is included,
    so any canonical-invisible mutation shows up. Aligned by the SAME
    ``canonical_key`` as ``diff_canonical`` so a benign reindex does not
    explode.

    IMPORTANT — this fingerprint is ENVELOPE-SENSITIVE (unlike
    ``canonical_messages``). A cross-round protocol/endpoint switch (OpenAI ↔
    Anthropic body shape) legitimately changes these bytes without changing the
    conversation. So a byte divergence with an IDENTICAL canonical fingerprint
    means "the real bytes differ" — which could be a content mutation OR a
    routing/protocol switch. The detector must therefore only use this to
    REFUSE the false "byte-identical eviction" claim and name the honest set of
    causes, never to fabricate a specific client-side history edit.

    Returns ``[{'key', 'h'}]`` per message.
    """
    out: list[dict] = []
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        entry = {'role': msg.get('role', ''),
                 'fields': _fields_of(msg), 'brief': _brief(msg)}
        key = canonical_key(entry)
        try:
            raw = json.dumps(_strip_cache_control(msg),
                             ensure_ascii=False, sort_keys=False)
        except (TypeError, ValueError) as e:
            logger.debug('[WireFP] wire_byte_prefix dump failed (%s) — '
                         'using str() form', e)
            raw = str(msg)
        out.append({'key': key, 'h': _md5(raw)})
    return out


def diff_byte_prefix(old: list, new: list, max_report: int = 8) -> list[str]:
    """Name the messages whose RAW serialized bytes differ, stable-key aligned.

    The true-byte counterpart of ``diff_canonical``. Compares the overlapping
    prefix by position (this round appends new tail messages we do not diff),
    reporting the stable ``key`` of each byte-divergent message prefixed with
    ``<bytes>`` so a downstream verdict can tell it apart from a canonical
    (semantic) culprit. A message present in one list but not the other is a
    length change. Capped at ``max_report`` (``…`` marks truncation).
    """
    changes: list[str] = []
    n = min(len(old), len(new))
    for i in range(n):
        o = old[i] or {}
        nw = new[i] or {}
        if o.get('h') != nw.get('h'):
            changes.append('<bytes>' + (nw.get('key') or o.get('key')
                                        or f'[{i}]'))
            if len(changes) >= max_report:
                changes.append('…')
                return changes
    if len(old) != len(new):
        changes.append(f'byte-len {len(old)}\u2192{len(new)}')
    return changes


def wire_byte_field_prefix(messages: list) -> list[dict]:
    """Per-message, PER-TOP-LEVEL-FIELD hash of the actual serialized wire
    bytes — the field-granular companion to ``wire_byte_prefix``.

    ``wire_byte_prefix`` proves THAT an already-cached message's raw bytes
    changed round-over-round, but its diff (``diff_byte_prefix``) names only
    the MESSAGE (``<bytes>assistant/tool_call(read_files)``). For the dominant
    remaining prefix-cache miss — a canonical-INVISIBLE ``<bytes>`` flip on a
    replayed ``assistant/tool_call`` turn — that leaves the culprit a CATEGORY
    ("reasoning_details rebuild / same-role merge / field reorder / protocol
    switch"), never a proven field.

    This hashes EACH top-level key of the message separately (``json.dumps``,
    insertion order preserved, ONLY ``cache_control`` stripped) so
    ``diff_byte_field_prefix`` can name the EXACT ``key.field`` that flipped:
    ``{reasoning_details}`` (the build_body rebuild), ``{tool_calls}`` (arg
    re-serialization), ``{content}``, etc. A separate ``__order__`` pseudo-
    field captures the field INSERTION ORDER (a pure reorder changes the wire
    bytes but no single field's value), so an order-only flip is named rather
    than laundered into "eviction".

    Aligned by the SAME ``canonical_key`` as ``diff_canonical`` /
    ``wire_byte_prefix`` so a benign reindex does not explode. Returns
    ``[{'key', 'fields': {field: md5}}]`` per message.
    """
    out: list[dict] = []
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        entry = {'role': msg.get('role', ''),
                 'fields': _fields_of(msg), 'brief': _brief(msg)}
        key = canonical_key(entry)
        clean = _strip_cache_control(msg)
        field_hashes: dict[str, str] = {}
        for fld, val in clean.items():
            try:
                raw = json.dumps(val, ensure_ascii=False, sort_keys=False)
            except (TypeError, ValueError) as e:
                logger.debug('[WireFP] wire_byte_field_prefix dump failed for '
                             'field=%s (%s) — using str() form', fld, e)
                raw = str(val)
            field_hashes[fld] = _md5(raw)
        # __order__ pseudo-field: the field INSERTION ORDER. A pure reorder
        # (same keys+values, different order) changes the serialized bytes
        # without changing any single field's value — it must still be named.
        field_hashes['__order__'] = _md5('\x01'.join(clean.keys()))
        out.append({'key': key, 'fields': field_hashes})
    return out


def diff_byte_field_prefix(old: list, new: list, max_report: int = 8) -> list[str]:
    """Name the exact ``<bytes>key{field}`` entries that byte-diverged.

    The field-granular counterpart of ``diff_byte_prefix``. Compares the
    overlapping prefix by position; for each message whose per-field hash map
    differs, reports one ``<bytes>{stable-key}{field}`` token per changed
    field (so a downstream verdict can name the ACTIONABLE field —
    ``reasoning_details`` / ``tool_calls`` / ``content`` / ``__order__``).
    Capped at ``max_report`` culprits (``…`` marks truncation). A length change
    of the compared prefix is reported as ``byte-field-len A→B``.
    """
    changes: list[str] = []
    n = min(len(old), len(new))
    for i in range(n):
        o = old[i] or {}
        nw = new[i] or {}
        of = o.get('fields') or {}
        nf = nw.get('fields') or {}
        if of == nf:
            continue
        key = nw.get('key') or o.get('key') or f'[{i}]'
        for field in sorted(set(of) | set(nf)):
            if of.get(field) != nf.get(field):
                changes.append(f'<bytes>{key}{{{field}}}')
                if len(changes) >= max_report:
                    changes.append('…')
                    return changes
    if len(old) != len(new):
        changes.append(f'byte-field-len {len(old)}\u2192{len(new)}')
    return changes


def wire_byte_region(system: Any, tools: Any) -> dict[str, str]:
    """TRUE-byte hash of the HOISTED ``system`` + ``tools`` cached-prefix region.

    The true-byte counterpart of ``system_fingerprint`` — and the reason it is
    needed: ``system_fingerprint`` is ITSELF LOSSY. It runs ``_text_of`` over
    the system block list (collapsing ``str`` ↔ ``[{type:text}]`` and folding
    block ordering into a text stream) and canonicalises each tool's
    ``parameters`` with ``sort_keys=True``. So a canonical-invisible byte change
    in the hoisted region — a system BLOCK REORDERING, a whitespace/wrapping
    flip, a per-turn re-serialization, or a tool-param KEY REORDER — leaves
    ``system_fingerprint`` reporting "unchanged" even though the exact bytes the
    gateway caches on DID change.

    On the Anthropic path this is the HIGHEST-probability suspect region: the
    hoisted system prompt is where the per-turn context is injected fresh every
    round (charter, board, peer-status, activity-feed, ``relevant_memories``).
    A byte change there that ``system_fingerprint`` cannot see would let a real
    context-mechanism corruption be laundered into "upstream eviction".

    So this hashes the ACTUAL serialized bytes: ``json.dumps`` with insertion
    order preserved (``sort_keys=False``), stripping ONLY ``cache_control``. NO
    ``_text_of`` collapsing, NO param key sorting — a block reorder or a key
    reorder DOES change the hash here, which is the whole point.

    Returns ``{'system': md5, 'tools': md5}``.
    """
    def _dump(obj: Any) -> str:
        try:
            return json.dumps(_strip_cache_control(obj),
                              ensure_ascii=False, sort_keys=False)
        except (TypeError, ValueError) as e:
            logger.debug('[WireFP] wire_byte_region dump failed (%s) — '
                         'using str() form', e)
            return str(obj)

    if system is None:
        _sys_raw = ''
    elif isinstance(system, str):
        _sys_raw = system            # a bare string IS its own wire bytes
    else:
        _sys_raw = _dump(system)     # block list — order-sensitive by design
    _tools_raw = _dump(tools or [])
    return {'system': _md5(_sys_raw), 'tools': _md5(_tools_raw)}


def diff_byte_region(old: dict | None, new: dict | None) -> list[str]:
    """Name which hoisted region(s) diverged at the RAW-byte level.

    Compares ``wire_byte_region`` outputs. Returns ``<bytes>system`` /
    ``<bytes>tools`` for each field whose true bytes changed, so a downstream
    verdict tells a hoisted-region byte divergence apart from a lossy
    ``system_fingerprint`` ``<hoisted>`` culprit and from a per-message
    ``<bytes>`` culprit. ``.get(...)`` defaults keep pre-change (missing) state
    inert — a mid-deploy round with no stored region never cries wolf.
    """
    if not old or not new:
        return []
    changes: list[str] = []
    for fld in ('system', 'tools'):
        if old.get(fld) != new.get(fld):
            changes.append('<bytes>' + fld)
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


def _canon_beta(anthropic_beta: Any) -> str:
    """Normalize an ``anthropic-beta`` header into an ORDER-INDEPENDENT token set.

    The header is a comma-joined list of beta flags (e.g.
    ``prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11``). Whether
    ``extended-cache-ttl`` is present is part of the gateway's cache key, so a
    presence flip must show — but a mere token REORDER (or added whitespace)
    does not change what the gateway keys on, so it must NOT. Split on ``,``,
    strip, drop empties, sort, re-join.
    """
    if not anthropic_beta:
        return ''
    parts = [p.strip() for p in str(anthropic_beta).split(',')]
    return ','.join(sorted(p for p in parts if p))


def routing_fingerprint(*, key_hash: Any = '', anthropic_beta: Any = '',
                        endpoint: Any = '') -> dict[str, str]:
    """Fingerprint the request's CACHE-NAMESPACE-determining routing attributes.

    THE LAST BLIND SPOT. ``canonical_messages`` / ``system_fingerprint`` /
    ``wire_byte_*`` all prove whether the request BODY changed. But Anthropic
    prompt caching is namespaced by the request's *routing* too: the upstream
    API key (a distinct key = a distinct cache namespace), the
    ``anthropic-beta`` header (``extended-cache-ttl`` presence is part of the
    key), and the endpoint. The dispatch layer CAN flip the key mid-conversation
    (cooldown / 429 / 401 / timeout → sticky key scored ``inf`` → picker
    rebinds), which drags the endpoint along, and the beta header is latched
    per-TASK so a new turn can re-latch a changed global. When any of these
    flips, a BYTE-IDENTICAL prefix lands on a COLD namespace → a guaranteed
    client-caused miss that the body fingerprints are blind to.

    This captures the three attributes so ``detect_cache_break`` can diff them
    (``diff_routing``) BEFORE it reaches the "byte-identical → upstream" verdict
    and instead NAME a client cache-namespace switch. ``key_hash`` is the
    already-salted+truncated non-secret discriminator ``_sse_core`` computes;
    the beta header is normalized order-independently so a token reorder is not
    a false flip. Any field absent → ''.

    Returns ``{'key': str, 'beta': str, 'endpoint': str}``.
    """
    return {
        'key': '' if key_hash is None else str(key_hash),
        'beta': _canon_beta(anthropic_beta),
        'endpoint': '' if endpoint is None else str(endpoint),
    }


def diff_routing(old: dict | None, new: dict | None) -> list[str]:
    """Name which cache-namespace attribute(s) flipped between rounds.

    Compares two ``routing_fingerprint`` outputs. Returns a stable-ordered list
    of ``<ns>key`` / ``<ns>beta`` / ``<ns>endpoint`` for each attribute whose
    value changed — the precise, false-positive-free signal that a byte-
    identical prefix was routed to a DIFFERENT gateway cache namespace (→ a
    client-caused cold miss). A missing side (mid-deploy: no prior routing
    captured, or non-Claude / capture failure) is inert — returns ``[]`` so it
    never cries wolf before the fingerprint exists.
    """
    if not old or not new:
        return []
    changes: list[str] = []
    for fld in ('key', 'beta', 'endpoint'):
        if old.get(fld) != new.get(fld):
            changes.append('<ns>' + fld)
    return changes
