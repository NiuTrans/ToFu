"""lib/memory/user_profile.py — the rolling, bounded personal-preference profile.

This is a THIRD memory placement, distinct from the two that already exist:

  1. System-prefix injection (BP1–3, always-on) — cache-poison for anything
     that changes; the memory-count hint was ripped out for exactly this
     reason (see ``.tofu/skills/memory-count-hint-mutates-cached-system-prefix.md``).
  2. Per-turn BM25 prefetch (``<relevant_memories>`` in the tail) — cache-safe,
     but its cheap-LLM reranker is *designed to drop* anything without a
     concrete task step, so a standing preference ("always answer in Chinese")
     never survives.

A personal preference needs to be BOTH always-on AND cache-stable. The trick
(validated by Hermes Agent + our own CLAUDE.md placement): put it in the
prepended ``_isMeta`` user message — the BP4 5-min-TTL tail segment — NOT the
system prefix. When the profile changes, only the cheap tail re-writes once;
the expensive system+tools prefix stays cached. The injection helper lives in
``lib/tasks_pkg/system_context.py`` and calls ``notify_compaction`` so the
cache-tracker doesn't false-positive the mutation.

Design choices (locked by the user):
  * Hard-capped (~800 tokens ≈ 2.5 KB).
  * NOT part of the BM25 corpus — it is never "searched", it is always present.
  * SCOPED by identity. ``scope=''`` (open / private mode — one operator, no
    tenant binding) → the single global file ``<data>/memories/.tofu_user_profile.md``
    (BYTE-IDENTICAL to before, no migration). A multi-user tenant ``user_id``
    → a per-tenant file ``<data>/memories/profiles/<scope>/.tofu_user_profile.md``
    so one tenant's profile is never injected into another's prompt. Scope is
    resolved from the request's ``AuthContext`` via ``resolve_profile_scope``
    and captured onto the task at creation (the consolidation daemon has no
    request context). The ``.tofu`` prefix (per the artifact registry — see
    ``lib/agent_artifacts.py``) is preserved on the filename either way.
  * Bullet-list markdown under headers (Hermes/OpenClaw ``USER.md`` shape).
  * The hard cap is the forcing function for refinement: the consolidation
    pass (layer 3) must ``replace`` in place rather than append.

This module is storage + rendering ONLY. The propose-confirm capture loop
(layer 3) builds on ``load_profile`` / ``save_profile`` / ``profile_over_cap``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = [
    'USER_PROFILE_CHAR_CAP',
    'resolve_profile_scope',
    'profile_path',
    'load_profile',
    'save_profile',
    'profile_char_count',
    'profile_over_cap',
    'render_profile_block',
    'split_profile_tiers',
    'render_profile_tiers',
    'applied_profile_items',
    'profile_summary_for_event',
    'apply_reinforcement',
    'apply_new_preference',
    'parse_items',
    'serialize_items',
    'save_items',
    'load_pending',
    'stage_pending',
    'resolve_pending',
]

# Hard byte/char cap on the profile body. ~800 tokens of dense English prose
# ≈ 2.5 KB; we cap on CHARS (cheap, exact, language-agnostic). Past this the
# consolidation pass must distil rather than grow. Kept as a module constant
# (not env-tunable) — the cap is the whole point of the design.
USER_PROFILE_CHAR_CAP = 2500

# Marker so the injection-side idempotency probe can detect an already-present
# block, and so we never confuse the profile reminder with CLAUDE.md.
_PROFILE_MARKER = '[USER PREFERENCE PROFILE]'

# Distinct marker for the relevance-gated DETAIL tier (rendered as its own
# cache-safe block, separate from the always-on core). Kept separate so the
# injection-side idempotency probes for core vs detail never collide.
_PROFILE_DETAIL_MARKER = '[USER PREFERENCE PROFILE — relevant detail]'

# Section headers (case-insensitive, normalised) whose bullets form the
# ALWAYS-ON core tier — work-style / standing instructions that should be in
# the prompt every turn regardless of the turn's topic. Everything under any
# OTHER header (e.g. "About the user" identity facts, project-specific notes)
# is the DETAIL tier, surfaced only when relevant to the current turn. A
# header-less bullet (no preceding ``##``) defaults to core, since an unsorted
# standing instruction is safer always-on than silently dropped.
_CORE_HEADERS = frozenset({'preferences'})


def resolve_profile_scope(ctx) -> str:
    """Resolve the profile storage scope from a request ``AuthContext``.

    The rule is deliberately minimal: the scope is the authenticated
    ``user_id``, which is populated ONLY by multi-user login
    (``_mint_session_key`` in ``routes/api_v1/users.py``). Open mode
    (synthetic local-admin) and private mode (a Bearer key with no tenant
    binding) both leave ``user_id`` empty, so they resolve to ``''`` — the
    single shared global profile, exactly the personal-install semantic.

    Fail-safe: anything we can't read an explicit ``user_id`` off of yields
    ``''`` (the global file), never a half-built scope.
    """
    try:
        return (getattr(ctx, 'user_id', '') or '').strip()
    except Exception as e:
        logger.debug('[UserProfile] scope resolve failed: %s', e)
        return ''


def _server_memories_dir() -> str:
    """Return ``<data>/memories`` (parent of the global store).

    Resolved fresh each call (mirrors ``storage._server_data_dir``) so tests
    can redirect via ``$TOFU_DATA_DIR``.
    """
    from lib.memory.storage import _server_data_dir
    return os.path.join(_server_data_dir(), 'memories')


def _sanitize_scope(scope: str) -> str:
    """Turn an identity scope (a multi-user ``user_id``) into a safe dir name.

    Returns ``''`` for an empty/falsy scope — the signal to use the single
    global file (open / private mode: one operator, no tenant binding). For a
    real scope we combine a charset-restricted prefix (readability) with a
    SHA-256 suffix (collision-resistance + traversal-proofing), so a hostile
    ``user_id`` like ``../../etc`` can never escape ``<data>/memories/profiles``.
    """
    import hashlib
    import re
    s = (scope or '').strip()
    if not s:
        return ''
    digest = hashlib.sha256(s.encode('utf-8')).hexdigest()[:16]
    safe = re.sub(r'[^A-Za-z0-9_-]', '', s)[:32]
    return f'{safe}_{digest}' if safe else digest


def profile_path(scope: str = '') -> str:
    """Absolute path to the personal-preference profile file for *scope*.

    ``scope=''`` (the default) → the single global file
    ``<data>/memories/.tofu_user_profile.md`` — the personal-install / open /
    private-mode profile. This keeps every existing deployment BYTE-IDENTICAL:
    open mode and private mode never set a ``user_id``, so they always land
    here, and there is no migration.

    A non-empty *scope* (a multi-user tenant ``user_id``) → a per-tenant file
    ``<data>/memories/profiles/<sanitized-scope>/.tofu_user_profile.md`` so one
    tenant's profile is never injected into another's prompt. The ``.tofu``
    prefix is preserved on the filename, and rooting under ``data/`` keeps the
    profile project-independent (follows the user across projects).
    """
    from lib.agent_artifacts import USER_PROFILE_FILE
    base = _server_memories_dir()
    sid = _sanitize_scope(scope)
    if not sid:
        return os.path.join(base, USER_PROFILE_FILE)
    return os.path.join(base, 'profiles', sid, USER_PROFILE_FILE)


def load_profile(scope: str = '') -> str:
    """Return the *scope*'s profile body (markdown), or '' when none exists.

    Never raises — a read failure degrades to an empty profile (the feature
    is advisory; a missing/broken profile must never block a turn).
    """
    path = profile_path(scope)
    try:
        if not os.path.isfile(path):
            return ''
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError as e:
        logger.warning('[UserProfile] read failed (%s): %s', path, e)
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

    The body is stored verbatim (markdown). We do NOT silently truncate at
    the cap — truncation mid-sentence corrupts meaning — instead we persist
    and FLAG ``over_cap`` so the consolidation pass (layer 3) knows it must
    distil on the next pass. Empty/whitespace body deletes the file (a user
    clearing their profile should leave no stale block).

    Returns ``{'path', 'chars', 'over_cap', 'saved': bool}``.
    """
    from lib.json_store import write_text_atomic

    path = profile_path(scope)
    body = (body or '').strip()

    if not body:
        # Clearing the profile — remove the file so nothing is injected.
        try:
            if os.path.isfile(path):
                os.remove(path)
                logger.info('[UserProfile] cleared (file removed): %s', path)
        except OSError as e:
            logger.warning('[UserProfile] clear failed (%s): %s', path, e)
        return {'path': path, 'chars': 0, 'over_cap': False, 'saved': True}

    over = len(body) > USER_PROFILE_CHAR_CAP
    if over:
        logger.warning('[UserProfile] body %d chars exceeds cap %d — saved '
                       'anyway; consolidation pass must distil',
                       len(body), USER_PROFILE_CHAR_CAP)

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_text_atomic(path, body + '\n')
    except OSError as e:
        logger.error('[UserProfile] save failed (%s): %s', path, e,
                     exc_info=True)
        return {'path': path, 'chars': len(body), 'over_cap': over,
                'saved': False}

    audit_log('user_profile_saved', chars=len(body), over_cap=over)
    return {'path': path, 'chars': len(body), 'over_cap': over, 'saved': True}


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


# ═══════════════════════════════════════════════════════════════════════
#  Consolidation write primitives (layer 3) — deterministic + cap-aware.
#  These are the testable core of the consolidation pass: they apply ONE
#  edit and enforce the cap as a forcing function (replace/distil in place,
#  never append-and-grow past the cap).
# ═══════════════════════════════════════════════════════════════════════

_DEFAULT_HEADER = '## Preferences'


def apply_reinforcement(old_text: str, new_text: str, scope: str = '') -> dict:
    """Replace an existing preference line IN PLACE (Hermes-style substring).

    ``old_text`` must be a unique substring of the current profile (typically
    a full bullet line). It is swapped for ``new_text``. This is the
    auto-applied path: a reinforcement of something already known, so it
    NEVER grows the file unboundedly (length delta only).

    Returns ``{'saved', 'matched', 'chars', 'over_cap'}``. ``matched`` is
    False (no write) when ``old_text`` isn't found or is ambiguous.
    """
    body = load_profile(scope)
    if not old_text or old_text not in body:
        logger.info('[UserProfile] reinforcement skipped — old_text not found')
        return {'saved': False, 'matched': False,
                'chars': len(body), 'over_cap': profile_over_cap(body)}
    if body.count(old_text) > 1:
        logger.warning('[UserProfile] reinforcement skipped — old_text '
                       'ambiguous (%d matches)', body.count(old_text))
        return {'saved': False, 'matched': False,
                'chars': len(body), 'over_cap': profile_over_cap(body)}
    updated = body.replace(old_text, new_text, 1)
    res = save_profile(updated, scope)
    res['matched'] = True
    return res


def apply_new_preference(text: str, header: str = _DEFAULT_HEADER,
                         scope: str = '') -> dict:
    """Append a NEW preference bullet under *header* (used after confirm).

    Cap is the forcing function: if appending would exceed the cap, the
    caller (consolidation pass) must distil first. We DO append here and
    flag ``over_cap`` so the next pass knows to consolidate — but we never
    silently drop the user's confirmed preference.

    Returns ``{'saved', 'chars', 'over_cap'}``.
    """
    text = (text or '').strip().lstrip('-*').strip()
    if not text:
        return {'saved': False, 'chars': profile_char_count(scope=scope),
                'over_cap': False}
    body = load_profile(scope)
    bullet = f'- {text}'
    if not body:
        new_body = f'{header}\n{bullet}'
    elif header in body:
        # Insert the bullet right after the header's first line.
        lines = body.splitlines()
        out: list[str] = []
        inserted = False
        for ln in lines:
            out.append(ln)
            if not inserted and ln.strip() == header.strip():
                out.append(bullet)
                inserted = True
        if not inserted:  # header substring but not its own line — append
            out.append(bullet)
        new_body = '\n'.join(out)
    else:
        new_body = f'{body}\n\n{header}\n{bullet}'
    return save_profile(new_body, scope)


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


# ── Pending proposals (propose-then-confirm gate) ──

def _pending_path() -> str:
    from lib.agent_artifacts import USER_PROFILE_PENDING_FILE
    return os.path.join(_server_memories_dir(), USER_PROFILE_PENDING_FILE)


def load_pending() -> list[dict]:
    """Return the list of staged (unconfirmed) preference proposals."""
    from lib.json_store import read_json
    data = read_json(_pending_path(), default=[])
    return data if isinstance(data, list) else []


def stage_pending(proposal: dict) -> dict:
    """Stage a NEW-preference proposal awaiting user confirmation.

    *proposal* must carry at least ``{'text': ...}``. We mint an ``id`` and a
    ``created`` timestamp, dedupe by identical ``text`` (so the same
    preference proposed twice doesn't pile up), and persist. Returns the
    stored proposal dict (with id).
    """
    import uuid
    from lib.json_store import write_json_atomic

    text = (proposal.get('text') or '').strip()
    if not text:
        return {}
    pending = load_pending()
    for p in pending:
        if (p.get('text') or '').strip() == text:
            return p  # already staged — idempotent
    entry = {
        'id': uuid.uuid4().hex[:12],
        'text': text,
        'header': proposal.get('header') or _DEFAULT_HEADER,
        'evidence': (proposal.get('evidence') or '')[:300],
        'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    pending.append(entry)
    write_json_atomic(_pending_path(), pending)
    audit_log('user_profile_pending_staged', pref_id=entry['id'])
    return entry


def resolve_pending(pending_id: str, accept: bool,
                    edited_text: str | None = None) -> dict:
    """Confirm (accept) or dismiss a staged proposal.

    On accept, the (optionally user-edited) text is written into the profile
    via :func:`apply_new_preference`. Either way the proposal is removed from
    the pending list. Returns ``{'resolved': bool, 'accepted': bool,
    'profile': <save result or None>}``.
    """
    from lib.json_store import write_json_atomic

    pending = load_pending()
    target = next((p for p in pending if p.get('id') == pending_id), None)
    if target is None:
        return {'resolved': False, 'accepted': False, 'profile': None}
    pending = [p for p in pending if p.get('id') != pending_id]
    write_json_atomic(_pending_path(), pending)

    save_res = None
    if accept:
        text = (edited_text or target.get('text') or '').strip()
        save_res = apply_new_preference(text, header=target.get('header')
                                        or _DEFAULT_HEADER)
    audit_log('user_profile_pending_resolved', pref_id=pending_id,
              accepted=bool(accept))
    return {'resolved': True, 'accepted': bool(accept), 'profile': save_res}
