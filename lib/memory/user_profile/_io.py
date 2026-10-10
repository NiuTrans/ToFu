"""lib/memory/user_profile/_io.py — profile body persistence + markers.

Storage primitives for the profile body: read (``load_profile``), size checks
(``profile_char_count`` / ``profile_over_cap``), atomic write (``save_profile``),
and the structured per-item view (``parse_items`` / ``serialize_items`` /
``save_items``) used by the settings UI. Also owns the module-level constants
(the cap, the injection markers, and the core-tier header set) that the render
layer consumes.
"""

from __future__ import annotations

from lib.log import audit_log, get_logger

from lib.memory.user_profile._paths import profile_path

logger = get_logger(__name__)

# Hard byte/char cap on My Context. ~800 tokens of dense English prose ≈ 2.5
# KB; we cap on CHARS (cheap, exact, language-agnostic). New writes are rejected
# as a unit when they exceed it, so the model never silently rewrites or drops
# a user's durable facts.
USER_PROFILE_CHAR_CAP = 2500

# Marker so the injection-side idempotency probe can detect an already-present
# block, and so we never confuse the profile reminder with CLAUDE.md.
_PROFILE_MARKER = '[USER CONTEXT]'

# Distinct marker for the relevance-gated DETAIL tier (rendered as its own
# cache-safe block, separate from the always-on core). Kept separate so the
# injection-side idempotency probes for core vs detail never collide.
_PROFILE_DETAIL_MARKER = '[USER CONTEXT — relevant detail]'

# Section headers (case-insensitive, normalised) whose bullets form the
# ALWAYS-ON core tier — work-style / standing instructions that should be in
# the prompt every turn regardless of the turn's topic. Everything under any
# OTHER header (e.g. "About the user" identity facts, project-specific notes)
# is the DETAIL tier, surfaced only when relevant to the current turn. A
# header-less bullet (no preceding ``##``) defaults to core, since an unsorted
# standing instruction is safer always-on than silently dropped.
_CORE_HEADERS = frozenset({'preferences', 'response preferences',
                           'about the user', 'work rules'})


def load_profile(scope: str = '') -> str:
    """Return the *scope*'s profile body (markdown), or '' when none exists.

    Never raises — a read failure degrades to an empty profile (the feature
    is advisory; a missing/broken profile must never block a turn).
    """
    try:
        from lib.memory.user_profile._context import legacy_profile_body
        return legacy_profile_body(scope)
    except Exception as e:
        logger.warning('[UserProfile] structured read failed (%s): %s',
                       profile_path(scope), e)
        return ''


def profile_char_count(body: str | None = None, scope: str = '') -> int:
    """Char count of the profile body (loads from disk when *body* is None)."""
    if body is None:
        body = load_profile(scope)
    return len(body or '')


def profile_over_cap(body: str | None = None, scope: str = '') -> bool:
    """True when the (given or on-disk) profile exceeds the hard cap."""
    return profile_char_count(body, scope) > USER_PROFILE_CHAR_CAP


def save_profile(body: str, scope: str = '') -> dict:
    """Persist the *scope*'s profile body atomically. Returns a status dict.

    The legacy Markdown body is translated into structured My Context. Writes
    over the hard cap are rejected rather than truncated or model-distilled.
    Empty/whitespace input clears the document.

    Returns ``{'path', 'chars', 'over_cap', 'saved': bool}``.
    """
    path = profile_path(scope)
    body = (body or '').strip()
    try:
        from lib.memory.user_profile._context import (
            legacy_items_to_context,
            save_context_items,
        )
        structured = legacy_items_to_context(parse_items(body)) if body else []
        res = save_context_items(structured, scope, enforce_cap=True)
    except Exception as e:
        logger.error('[UserProfile] structured save failed (%s): %s', path, e,
                     exc_info=True)
        return {'path': path, 'chars': len(body),
                'over_cap': len(body) > USER_PROFILE_CHAR_CAP,
                'saved': False}
    res['path'] = path
    audit_log('user_profile_saved', chars=res['chars'],
              over_cap=res['over_cap'])
    return res


# ── Structured per-item view (for the settings UI) ──
#
# The on-disk format is markdown bullets under ``## Header`` lines. The
# settings UI edits ONE preference at a time, so it needs a structured view
# (a flat list of ``{header, text}``) and a way to write a whole edited list
# back. These are pure transforms over the same markdown body — the injected
# prompt block is still the verbatim markdown, so nothing about cache-stability
# or the cap changes.

def parse_items(body: str | None = None, scope: str = '') -> list[dict]:
    """Parse the profile markdown into an ordered list of ``{header, text}``.

    A line beginning with ``#`` starts a new section (its text, minus the
    leading ``#``/whitespace, becomes the ``header`` of every following item).
    Bullet lines (``- ``/``* ``) and any other non-blank line become items.
    """
    if body is None:
        body = load_profile(scope)
    items: list[dict] = []
    current_header = ''
    for raw in (body or '').splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith('#'):
            current_header = s.lstrip('#').strip()
            continue
        if s.startswith(('- ', '* ')):
            s = s[2:].strip()
        items.append({'header': current_header, 'text': s})
    return items


def serialize_items(items: list[dict]) -> str:
    """Render a list of ``{header, text}`` back into markdown bullets.

    Items are grouped under their header in first-seen order; empty-text items
    are dropped. The result round-trips with :func:`parse_items`.
    """
    order: list[str] = []
    groups: dict[str, list[str]] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        header = (it.get('header') or '').strip()
        text = (it.get('text') or '').strip().lstrip('-*').strip()
        if not text:
            continue
        if header not in groups:
            groups[header] = []
            order.append(header)
        groups[header].append(text)
    out: list[str] = []
    for header in order:
        if header:
            out.append(f'## {header}')
        out.extend(f'- {t}' for t in groups[header])
        out.append('')
    return '\n'.join(out).strip()


def save_items(items: list[dict], scope: str = '') -> dict:
    """Serialize a structured item list and persist it (see :func:`save_profile`)."""
    return save_profile(serialize_items(items), scope)
