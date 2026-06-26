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
  * ONE file, global / per-user scope, hard-capped (~800 tokens ≈ 2.5 KB).
  * NOT part of the BM25 corpus — it is never "searched", it is always present.
  * Stored as ``<data>/memories/.tofu_user_profile.md`` (``.tofu`` prefix per
    the artifact registry — see ``lib/agent_artifacts.py``).
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
    'profile_path',
    'load_profile',
    'save_profile',
    'profile_char_count',
    'profile_over_cap',
    'render_profile_block',
    'profile_summary_for_event',
    'apply_reinforcement',
    'apply_new_preference',
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


def _server_memories_dir() -> str:
    """Return ``<data>/memories`` (parent of the global store).

    Resolved fresh each call (mirrors ``storage._server_data_dir``) so tests
    can redirect via ``$TOFU_DATA_DIR``.
    """
    from lib.memory.storage import _server_data_dir
    return os.path.join(_server_data_dir(), 'memories')


def profile_path() -> str:
    """Absolute path to the single personal-preference profile file.

    ``<data>/memories/.tofu_user_profile.md``. The ``.tofu`` prefix makes
    every artifact consumer (gitignore / export / self-update) recognise it
    mechanically (see ``lib/agent_artifacts.py``), and rooting it under
    ``data/`` keeps it project-independent (follows the user across projects).
    """
    from lib.agent_artifacts import USER_PROFILE_FILE
    return os.path.join(_server_memories_dir(), USER_PROFILE_FILE)


def load_profile() -> str:
    """Return the profile body (markdown), or '' when no profile exists yet.

    Never raises — a read failure degrades to an empty profile (the feature
    is advisory; a missing/broken profile must never block a turn).
    """
    path = profile_path()
    try:
        if not os.path.isfile(path):
            return ''
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError as e:
        logger.warning('[UserProfile] read failed (%s): %s', path, e)
        return ''


def profile_char_count(body: str | None = None) -> int:
    """Char count of the profile body (loads from disk when *body* is None)."""
    if body is None:
        body = load_profile()
    return len(body or '')


def profile_over_cap(body: str | None = None) -> bool:
    """True when the (given or on-disk) profile exceeds the hard cap."""
    return profile_char_count(body) > USER_PROFILE_CHAR_CAP


def save_profile(body: str) -> dict:
    """Persist the profile body atomically. Returns a small status dict.

    The body is stored verbatim (markdown). We do NOT silently truncate at
    the cap — truncation mid-sentence corrupts meaning — instead we persist
    and FLAG ``over_cap`` so the consolidation pass (layer 3) knows it must
    distil on the next pass. Empty/whitespace body deletes the file (a user
    clearing their profile should leave no stale block).

    Returns ``{'path', 'chars', 'over_cap', 'saved': bool}``.
    """
    from lib.json_store import write_text_atomic

    path = profile_path()
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


def render_profile_block(body: str | None = None) -> str | None:
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
        body = load_profile()
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


def profile_summary_for_event(body: str | None = None,
                              max_items: int = 8) -> list[str]:
    """Extract a short list of preference bullet lines for the UI chip.

    Pulls markdown bullet lines (``- ``/``* ``) from the profile so the
    "preferences applied" chip can show WHICH preferences were in play this
    turn without dumping the whole file. Header lines and blanks are skipped.
    """
    if body is None:
        body = load_profile()
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


def apply_reinforcement(old_text: str, new_text: str) -> dict:
    """Replace an existing preference line IN PLACE (Hermes-style substring).

    ``old_text`` must be a unique substring of the current profile (typically
    a full bullet line). It is swapped for ``new_text``. This is the
    auto-applied path: a reinforcement of something already known, so it
    NEVER grows the file unboundedly (length delta only).

    Returns ``{'saved', 'matched', 'chars', 'over_cap'}``. ``matched`` is
    False (no write) when ``old_text`` isn't found or is ambiguous.
    """
    body = load_profile()
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
    res = save_profile(updated)
    res['matched'] = True
    return res


def apply_new_preference(text: str, header: str = _DEFAULT_HEADER) -> dict:
    """Append a NEW preference bullet under *header* (used after confirm).

    Cap is the forcing function: if appending would exceed the cap, the
    caller (consolidation pass) must distil first. We DO append here and
    flag ``over_cap`` so the next pass knows to consolidate — but we never
    silently drop the user's confirmed preference.

    Returns ``{'saved', 'chars', 'over_cap'}``.
    """
    text = (text or '').strip().lstrip('-*').strip()
    if not text:
        return {'saved': False, 'chars': profile_char_count(), 'over_cap': False}
    body = load_profile()
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
    return save_profile(new_body)


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
