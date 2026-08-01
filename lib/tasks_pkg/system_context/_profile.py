"""User-context + personal-preference profile placement helpers.

Extracted from ``lib.tasks_pkg.system_context`` (facade-preserving split).

Holds the Claude-Code-style ``prependUserContext`` inserter and the two
preference-profile placement primitives (byte-stable CORE tier on the
``_isMeta`` carrier, relevance-gated DETAIL tier on the true tail).
"""

from lib.log import get_logger

logger = get_logger(__name__)

# Idempotency markers for the personal-preference profile blocks. Mirror the
# constants in lib/memory/user_profile.py — kept in sync there. The core tier
# (always-on) carries _PROFILE_MARKER; the relevance-gated detail tier carries
# the distinct _PROFILE_DETAIL_MARKER so the two never collide.
_PROFILE_MARKER = '[USER PREFERENCE PROFILE]'
_PROFILE_DETAIL_MARKER = '[USER PREFERENCE PROFILE — relevant detail]'


def _insert_user_context_message(messages, body: str) -> None:
    """Insert a Claude-Code-style ``<system-reminder>`` user message.

    Inserted RIGHT AFTER the last system message (or at index 0 if no
    system message), BEFORE the first real user message.  Matches Claude
    Code's ``prependUserContext`` behavior — see ``utils/api.ts:449``.

    Marked with ``_isMeta: True`` so the debug panel / token
    counter / persistence layers can recognize it as synthetic.

    Idempotency: skip if any existing user message already contains the
    ``<system-reminder>`` claudeMd marker — Critic mode reuses worker
    messages and re-injecting would duplicate.
    """
    # Find the first non-system slot
    insert_idx = 0
    for i, m in enumerate(messages):
        if m.get('role') != 'system':
            insert_idx = i
            break
    else:
        # All system → append
        insert_idx = len(messages)

    # Idempotency: if a previous _isMeta user message with the same
    # marker already exists, don't double-inject.
    for m in messages:
        if m.get('role') != 'user' or not m.get('_isMeta'):
            continue
        c = m.get('content', '')
        if isinstance(c, str) and '[PROJECT CO-PILOT MODE]' in c:
            logger.debug('[Inject] CC user-context already present, skipping')
            return
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get('type') == 'text' \
                        and '[PROJECT CO-PILOT MODE]' in blk.get('text', ''):
                    logger.debug('[Inject] CC user-context already present, skipping')
                    return

    messages.insert(insert_idx, {
        'role': 'user',
        'content': body,
        '_isMeta': True,  # synthetic marker — see Claude Code's isMeta flag
    })


def _append_user_profile_block(messages, block: str,
                               marker: str = _PROFILE_MARKER) -> bool:
    """Append the preference-profile block to the cache-safe tail.

    Placement priority (cache-stability matters):
      1. If a prepended ``_isMeta`` user message exists (CLAUDE.md carrier),
         append the block there as a separate text block. This co-locates the
         profile with the project-context reminder in the BP4 tail segment so
         a profile edit re-writes only that already-dynamic segment.
      2. Otherwise (project mode off → no _isMeta msg) append to the FIRST
         real user message — still the tail, still NOT messages[0].

    ``marker`` is the idempotency substring this block carries (the core tier
    uses :data:`_PROFILE_MARKER`; the relevance-gated detail tier passes its
    own distinct marker so the two never block each other).

    Returns True if the block was injected, False if no suitable user message
    was found (in which case the caller skips the notify_compaction).
    """
    # Idempotency: the block rides a USER message (not messages[0]), so the
    # system-text probe in the caller can't see it. Re-scan here and bail if
    # the marker is already present anywhere (endpoint re-entry / post-
    # compaction re-injection share the same messages list).
    for m in messages:
        c = m.get('content', '')
        if isinstance(c, str):
            if marker in c:
                return False
        elif isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get('type') == 'text' \
                        and marker in blk.get('text', ''):
                    return False

    target_idx = None
    for i, m in enumerate(messages):
        if m.get('role') == 'user' and m.get('_isMeta'):
            target_idx = i
            break
    if target_idx is None:
        # No _isMeta carrier — fall back to the first real (non-meta) user msg.
        for i, m in enumerate(messages):
            if m.get('role') == 'user':
                target_idx = i
                break
    if target_idx is None:
        return False

    content = messages[target_idx].get('content', '')
    if isinstance(content, str):
        # Never fabricate a phantom empty text block (Kimi 400 "text content is empty").
        _blocks = ([{'type': 'text', 'text': content}] if content.strip() else [])
        _blocks.append({'type': 'text', 'text': block})
        messages[target_idx]['content'] = _blocks
    elif isinstance(content, list):
        messages[target_idx]['content'] = list(content) + [
            {'type': 'text', 'text': block},
        ]
    else:
        messages[target_idx]['content'] = [{'type': 'text', 'text': block}]
    return True


def _refresh_detail_block(messages, block: str | None,
                          marker: str = _PROFILE_DETAIL_MARKER) -> str:
    """Replace (or strip) the relevance-gated DETAIL block on the TRUE tail.

    The detail tier rides the LAST user message (the genuine volatile tail) —
    the SAME cache-safe seam ``inject_relevant_memories`` uses — NOT the
    prepended ``_isMeta`` carrier (index 1, CLAUDE.md). The carrier lives
    inside the cached prompt prefix (``messages[0:N-2]`` after the first tool
    round); because the detail selection changes per turn, putting it on the
    carrier rewrites the carrier bytes every turn and re-bills the whole prefix
    from ``messages[1]`` onward within the 5-min TTL window (this was bug B4 —
    the ★2.5 comment mislabelled the carrier as "the tail"). The byte-stable
    CORE tier still rides the carrier via :func:`_append_user_profile_block`;
    only the volatile detail moves here.

    Unlike :func:`_append_user_profile_block` (append-once, for the byte-stable
    CORE tier), the detail tier is RELEVANCE-GATED PER TURN. Within ONE task
    the same last user message may be re-injected (endpoint-mode Planner /
    Worker / Critic share the message list), so this:

      1. Removes any existing text block carrying ``marker`` from the LAST user
         message (a stale detail from a re-entry on the SAME message), and
      2. appends ``block`` when non-None (this turn's fresh selection).

    A PRIOR turn's detail block, frozen on that turn's (now mid-history) user
    message, is deliberately left untouched — stripping a prefix-resident block
    would itself mutate the cached prefix. This is the same frozen-stale-block
    tradeoff ``<relevant_memories>`` already makes, and it keeps the cache
    intact: no shared message is rewritten across turns.

    Returns one of ``'replaced'`` / ``'added'`` / ``'removed'`` / ``'noop'`` so
    the caller knows whether the tail was mutated (→ ``notify_compaction``).
    """
    # Target the LAST user message (true volatile tail), walking from the end —
    # mirrors inject_relevant_memories. NEVER the _isMeta carrier (see B4).
    target_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            target_idx = i
            break
    if target_idx is None:
        return 'noop'

    content = messages[target_idx].get('content', '')
    # Normalise to a list of blocks so we can surgically drop the stale one.
    # Never fabricate a phantom empty text block (Kimi 400 "text content is empty").
    if isinstance(content, str):
        blocks = [{'type': 'text', 'text': content}] if content.strip() else []
    elif isinstance(content, list):
        blocks = list(content)
    else:
        blocks = []

    def _is_detail(blk) -> bool:
        return (isinstance(blk, dict) and blk.get('type') == 'text'
                and marker in blk.get('text', ''))

    had_old = any(_is_detail(b) for b in blocks)
    if not had_old and block is None:
        return 'noop'  # nothing to strip, nothing to add — tail unchanged

    new_blocks = [b for b in blocks if not _is_detail(b)]
    if block is not None:
        new_blocks.append({'type': 'text', 'text': block})

    messages[target_idx]['content'] = new_blocks
    if block is not None:
        return 'replaced' if had_old else 'added'
    return 'removed'
