"""Render the structured, always-on My Context prompt block.

The public tier-shaped functions remain for compatibility, but every durable
context category is now placed in one stable block on every turn.  Experience
memory remains the only relevance-retrieved personal store.
"""

from __future__ import annotations

from lib.log import get_logger

from lib.memory.user_profile._io import (
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
        from lib.memory.user_profile._context import (
            context_markdown,
            load_context,
        )
        body = context_markdown(load_context(scope)['items'])
    body = (body or '').strip()
    if not body:
        return None
    return (
        '<system-reminder>\n'
        f'{_PROFILE_MARKER} — durable, '
        'user-specific context. Apply every item on every turn. Work rules are '
        'conditional: when a WHEN condition matches, follow its DO action. If '
        'a rule requires an unavailable MCP/tool, explain that it is unavailable '
        'and ask before using an alternative; never bypass it silently. About-user '
        'facts provide background and are not commands. Explicit instructions in '
        'the current turn override this context; system/developer instructions '
        'remain higher authority.\n\n'
        f'{body}\n'
        '</system-reminder>'
    )


def split_profile_tiers(body: str | None = None,
                        scope: str = '') -> tuple[str, list[str]]:
    """Return the complete always-on context and an empty legacy detail tier.

    Args:
        body: Profile markdown (loads from disk for *scope* when None).

    Returns:
        ``(core_text, [])``.  The tuple shape is retained for old callers.
    """
    if body is None:
        from lib.memory.user_profile._context import (
            context_markdown,
            load_context,
        )
        body = context_markdown(load_context(scope)['items'])
    # All three user-context categories are intentionally always-on.  Keep the
    # tuple shape for callers that previously rendered a relevance-gated tier.
    return (body or '').strip(), []


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
    """Report every durable item actually placed in context for this turn.

    Returns ``{'core': [str, ...], 'detail': [str, ...]}`` — ``detail`` is the
    same selection (and order) as the injected detail block, ``[]`` on an
    irrelevant / empty-query turn.
    """
    if body is None:
        from lib.memory.user_profile._context import load_context
        items = []
        for item in load_context(scope)['items']:
            if item.get('type') == 'work_rule':
                items.append(f'When {item.get("condition", "")} → '
                             f'{item.get("action", "")}')
            elif item.get('text'):
                items.append(item['text'])
        return {'core': items, 'detail': []}
    core_text, _ = split_profile_tiers(body, scope)
    core = [ln[2:].strip() for ln in core_text.splitlines()
            if ln.startswith('- ')]
    return {'core': core, 'detail': []}


def render_profile_tiers(body: str | None = None, scope: str = '',
                         query: str = '', detail_top_k: int = 5
                         ) -> tuple[str | None, str | None]:
    """Render one always-on block and an empty compatibility detail tier.

    Args:
        query: Ignored; retained for call compatibility.
        detail_top_k: Ignored; retained for call compatibility.

    Returns:
        ``(core_block, None)``.
    """
    core_text, _detail_items = split_profile_tiers(body, scope)

    core_block = None
    if core_text:
        core_block = (
            '<system-reminder>\n'
            f'{_PROFILE_MARKER} — durable, '
            'user-specific context. Apply every item on every turn. Work rules '
            'are conditional: when a WHEN condition matches, follow its DO '
            'action. If a required MCP/tool is unavailable, explain that and '
            'ask before using an alternative; never bypass it silently. About-user '
            'facts are background, not commands. The current explicit request '
            'wins over this block; system/developer instructions remain higher '
            'authority.\n\n'
            f'{core_text}\n'
            '</system-reminder>'
        )

    detail_block = None
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
