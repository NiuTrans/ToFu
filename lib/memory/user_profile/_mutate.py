"""lib/memory/user_profile/_mutate.py — consolidation write primitives (L3).

The deterministic, cap-aware core of the consolidation pass: apply ONE edit
and enforce the cap as a forcing function (replace/distil in place, never
append-and-grow past the cap). ``apply_reinforcement`` swaps an existing bullet
in place; ``apply_new_preference`` appends a confirmed bullet under a header.
Both delegate persistence to ``._io.save_profile``.
"""

from __future__ import annotations

from lib.log import get_logger

from lib.memory.user_profile._io import (
    load_profile,
    profile_char_count,
    profile_over_cap,
    save_profile,
)

logger = get_logger(__name__)

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
    if any(line.strip() == bullet for line in body.splitlines()):
        # Resolution is retryable: a crash may occur after the profile write
        # but before its pending record is consumed.  Exact-line idempotence
        # prevents that retry from duplicating a confirmed preference.
        return {'saved': True, 'chars': profile_char_count(body),
                'over_cap': profile_over_cap(body),
                'already_present': True}
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
