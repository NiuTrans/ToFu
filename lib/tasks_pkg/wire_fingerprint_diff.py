"""Pure comparisons for canonical and byte-level provider fingerprints.

Construction stays in wire_fingerprint; comparison owns ordered culprit labels,
report truncation and first changed positions. Inputs are read-only snapshots.
This module has no task state, hashing, JSON serialization or logging dependency.
"""
from __future__ import annotations


def _result_blocks(entry: dict, key: str) -> tuple:
    """Per-result ``(key, hash)`` pairs; a lone result is its own message."""
    blocks = entry.get('blocks')
    if blocks is not None:
        return blocks
    fields = entry.get('fields') or {}
    if 'tool_result' in fields:
        return ((entry.get('key') or key, fields['tool_result']),)
    return ()


def _changed_result_keys(old: dict, new: dict, key: str) -> list[str]:
    """Keys of the tool results that differ inside one merged message.

    Only entries carrying per-result ``blocks`` evidence are attributed per
    result; other snapshots keep the message-level report unchanged.
    """
    if 'blocks' not in old and 'blocks' not in new:
        return [key]
    old_blocks = _result_blocks(old, key)
    new_blocks = _result_blocks(new, key)
    changed: list[str] = []
    for index in range(max(len(old_blocks), len(new_blocks))):
        before = old_blocks[index] if index < len(old_blocks) else None
        after = new_blocks[index] if index < len(new_blocks) else None
        if before != after:
            result_key = (after or before)[0] or key
            if result_key not in changed:
                changed.append(result_key)
    return changed or [key]


def diff_canonical(old: list, new: list, max_report: int = 8) -> list[str]:
    """Name the exact ``key.field`` entries that differ, stable-key aligned.

    Compares the OVERLAPPING prefix by position but reports the STABLE key +
    field, so a re-index shifts nothing as long as the same messages are
    present in the same order. A changed ``tool_result`` of a turn merging
    several results is reported once per changed result, under that result's
    own key (the merged message's key names only its first result). A length
    difference is reported as ``len A→B``. Capped at ``max_report`` culprits
    (an ``…`` marks truncation).
    """
    changes: list[str] = []
    n = min(len(old), len(new))
    for i in range(n):
        o = old[i] or {}
        nw = new[i] or {}
        of = o.get('fields') or {}
        nf = nw.get('fields') or {}
        # Equal native maps need no field-set allocation or sorting.
        if type(of) is dict and type(nf) is dict and of == nf:
            continue
        key = nw.get('key') or o.get('key') or f'[{i}]'
        for field in sorted(set(of) | set(nf)):
            if of.get(field) != nf.get(field):
                culprit_keys = (_changed_result_keys(o, nw, key)
                                if field == 'tool_result' else (key,))
                for culprit_key in culprit_keys:
                    changes.append(f'{culprit_key}.{field}')
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


def diff_routing(old: dict | None, new: dict | None) -> list[str]:
    """Name which cache-namespace attribute(s) flipped between rounds.

    Compares two ``routing_fingerprint`` outputs. Returns a stable-ordered list
    of ``<ns>key`` / ``<ns>beta`` / ``<ns>endpoint`` / ``<ns>affinity`` for each
    attribute whose
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
    # Fingerprints captured before the affinity field existed carried none.
    if old.get('affinity', '') != new.get('affinity', ''):
        changes.append('<ns>affinity')
    return changes
