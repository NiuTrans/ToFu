"""Render the structured, always-on My Context prompt block and event items."""

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

    NOTE: this is placed on the appended ``_isMeta`` user message (BP4 tail),
    NEVER messages[0]. See module docstring + the injection site in
    ``lib.tasks_pkg.context_composer``.
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


def context_items_for_event(body: str | None = None,
                            scope: str = '') -> list[str]:
    """Return every durable item represented by the injected context block."""
    if body is None:
        from lib.memory.user_profile._context import load_context
        items = []
        for item in load_context(scope)['items']:
            if item.get('type') == 'work_rule':
                items.append(f'When {item.get("condition", "")} → '
                             f'{item.get("action", "")}')
            elif item.get('text'):
                items.append(item['text'])
        return items
    return [line[2:].strip() for line in (body or '').splitlines()
            if line.startswith('- ')]


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
