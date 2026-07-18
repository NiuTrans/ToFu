"""lib/memory/user_profile/_render.py — injection-block rendering + tiering.

Turns the stored profile body into the cache-stable ``<system-reminder>``
blocks the injection site places on the BP4 tail. Owns the tier split
(always-on core vs relevance-gated detail), the BM25 detail selection, the
"preferences applied" UI-chip view, and the single-block / two-block renderers.
Read-only over the profile — persistence lives in ``._io``.
"""

from __future__ import annotations

from lib.log import get_logger

from lib.memory.user_profile._io import (
    _CORE_HEADERS,
    _PROFILE_DETAIL_MARKER,
    _PROFILE_MARKER,
    load_profile,
)

logger = get_logger(__name__)


def render_profile_block(body: str | None = None, scope: str = '') -> str | None:
    """Render the cache-stable injection block, or None when empty.

    The returned string is wrapped in ``<system-reminder>`` (matching every
    other out-of-band injection) and carries the ``_PROFILE_MARKER`` so the
    injection-side idempotency probe can detect it. The body itself is the
    profile markdown verbatim — frozen at task start by the caller.

    NOTE: this is placed on the prepended ``_isMeta`` user message (BP4 tail),
    NEVER messages[0]. See module docstring + the injection site in
    ``lib/tasks_pkg/system_context.py``.
    """
    if body is None:
        body = load_profile(scope)
    body = (body or '').strip()
    if not body:
        return None
    return (
        '<system-reminder>\n'
        f'{_PROFILE_MARKER} — durable facts the user has told you about '
        'themselves and how they like you to work. Apply these by default '
        'across the whole conversation, even when the current message does '
        "not restate them. They are NOT a task instruction to act on now; "
        'if one conflicts with an explicit request in this turn, the explicit '
        'request wins.\n\n'
        f'{body}\n'
        '</system-reminder>'
    )


def split_profile_tiers(body: str | None = None,
                        scope: str = '') -> tuple[str, list[str]]:
    """Split the profile into the always-on CORE tier and the DETAIL items.

    The tier is derived purely from which ``## Header`` a bullet lives under —
    no second file, no new persistence shape. Bullets under a header in
    :data:`_CORE_HEADERS` (plus any header-less leading bullets) become the
    core; bullets under every other header become individually relevance-gated
    detail items.

    Args:
        body: Profile markdown (loads from disk for *scope* when None).

    Returns:
        ``(core_text, detail_items)`` where ``core_text`` is the verbatim
        markdown of the core sections (headers + their bullets, ready to inject
        as the byte-stable always-on block, '' when empty) and ``detail_items``
        is a flat list of ``"<header>: <bullet>"`` strings — one per detail
        bullet — carrying the header so the relevance scorer and the rendered
        block both keep the identity/section context.
    """
    if body is None:
        body = load_profile(scope)
    body = (body or '').strip()
    if not body:
        return '', []

    core_lines: list[str] = []
    detail_items: list[str] = []
    current_header = ''          # display header for the current section
    current_is_core = True       # header-less leading bullets default to core
    core_header_open = False     # have we already emitted current_header into core?

    for raw in body.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith('#'):
            current_header = s.lstrip('#').strip()
            current_is_core = current_header.lower() in _CORE_HEADERS
            core_header_open = False
            continue
        bullet = s[2:].strip() if s.startswith(('- ', '* ')) else s
        if current_is_core:
            if current_header and not core_header_open:
                core_lines.append(f'## {current_header}')
                core_header_open = True
            core_lines.append(f'- {bullet}')
        else:
            prefix = f'{current_header}: ' if current_header else ''
            detail_items.append(f'{prefix}{bullet}')

    return '\n'.join(core_lines).strip(), detail_items


def _select_detail_items(detail_items: list[str], query: str,
                         detail_top_k: int = 5) -> list[str]:
    """Return the relevance-selected detail bullets for *query* (BM25, score>0).

    Shared by :func:`render_profile_tiers` (what to inject) and
    :func:`applied_profile_items` (what the UI chip reports), so the chip can
    never disagree with the prompt about which detail bullets were in context.
    Empty query or no positive matches → ``[]``.
    """
    if not detail_items or not query:
        return []
    from lib.memory.relevance import score_items
    ranked = score_items(query, detail_items)
    if not ranked:
        return []
    return [detail_items[i] for i, _ in ranked[:max(1, detail_top_k)]]


def applied_profile_items(body: str | None = None, scope: str = '',
                          query: str = '', detail_top_k: int = 5) -> dict:
    """Report the profile bullets ACTUALLY placed in context for this turn.

    Mirrors exactly what :func:`render_profile_tiers` injects: the full core
    tier (always-on) plus only the relevance-selected detail bullets. Used to
    build the "preferences applied" UI chip so the frontend shows the REAL
    injected set — never an arbitrary first-N slice (the chip and the prompt
    must agree).

    Returns ``{'core': [str, ...], 'detail': [str, ...]}`` — ``detail`` is the
    same selection (and order) as the injected detail block, ``[]`` on an
    irrelevant / empty-query turn.
    """
    core_text, detail_items = split_profile_tiers(body, scope)
    core = [ln[2:].strip() for ln in core_text.splitlines()
            if ln.startswith('- ')]
    detail = _select_detail_items(detail_items, query, detail_top_k)
    return {'core': core, 'detail': detail}


def render_profile_tiers(body: str | None = None, scope: str = '',
                         query: str = '', detail_top_k: int = 5
                         ) -> tuple[str | None, str | None]:
    """Render the profile as TWO blocks: always-on core + relevance-gated detail.

    This is the tiered counterpart to :func:`render_profile_block`. The core
    block is byte-stable across turns (no query dependence) so it stays in the
    prompt cache; the detail block varies per turn — only the detail bullets
    whose BM25 score against *query* is positive are included (top ``detail_top_k``),
    so an irrelevant turn ships NO detail at all.

    Both are wrapped in ``<system-reminder>`` and carry distinct markers
    (:data:`_PROFILE_MARKER` / :data:`_PROFILE_DETAIL_MARKER`) so the injection
    site's idempotency probes can detect each independently.

    Args:
        query: Last-user-turn text used to score the detail tier. Empty query
            → no detail block (nothing to relevance-match against).
        detail_top_k: Max detail bullets to surface on a relevant turn.

    Returns:
        ``(core_block, detail_block)`` — either may be None when its tier is
        empty (no core sections / no relevant detail).
    """
    if body is None:
        body = load_profile(scope)
    core_text, detail_items = split_profile_tiers(body, scope)

    core_block = None
    if core_text:
        core_block = (
            '<system-reminder>\n'
            f'{_PROFILE_MARKER} — durable facts the user has told you about '
            'themselves and how they like you to work. Apply these by default '
            'across the whole conversation, even when the current message does '
            "not restate them. They are NOT a task instruction to act on now; "
            'if one conflicts with an explicit request in this turn, the '
            'explicit request wins.\n\n'
            f'{core_text}\n'
            '</system-reminder>'
        )

    detail_block = None
    picked = _select_detail_items(detail_items, query, detail_top_k)
    if picked:
        lines = '\n'.join(f'- {it}' for it in picked)
        detail_block = (
            '<system-reminder>\n'
            f'{_PROFILE_DETAIL_MARKER} — additional facts about the user '
            'that look relevant to THIS turn. Same standing as the core '
            'profile; apply by default, but the explicit request wins.\n\n'
            f'{lines}\n'
            '</system-reminder>'
        )

    return core_block, detail_block


def profile_summary_for_event(body: str | None = None,
                              max_items: int = 8, scope: str = '') -> list[str]:
    """Extract a short list of preference bullet lines for the UI chip.

    Pulls markdown bullet lines (``- ``/``* ``) from the profile so the
    "preferences applied" chip can show WHICH preferences were in play this
    turn without dumping the whole file. Header lines and blanks are skipped.
    """
    if body is None:
        body = load_profile(scope)
    items: list[str] = []
    for raw in (body or '').splitlines():
        line = raw.strip()
        if line.startswith(('- ', '* ')):
            items.append(line[2:].strip())
        if len(items) >= max_items:
            break
    return items
