"""lib/ids.py — the single home for short random identifiers.

Dozens of call sites across the backend hand-rolled the same
``uuid.uuid4().hex[:N]`` (optionally with a ``prefix``) idiom to mint short
ids — chatcmpl-/msg_/run-/led_/pt_/hg_/… Each copy was a chance for the length
or alphabet to drift. ``short_id`` is the one implementation they all delegate
to.

Deliberately dependency-free (stdlib ``uuid`` only) so ANY layer can import it
without an import-cycle risk — in particular ``lib/compat/_common`` re-exports
it, and the billing / conversations / tasks / routes layers import it directly.

Note: ``lib/log.py`` is intentionally NOT a consumer — it is the foundational
logging module that must import nothing from ``lib.*`` (see its module comment),
so its one ``set_req_id`` request-id mint stays inline.
"""

from __future__ import annotations

import uuid


def short_id(prefix: str = '', n: int = 24) -> str:
    """Return ``<prefix><n lowercase-hex chars>``.

    A uuid4 hex string truncated to ``n`` characters, with an optional literal
    ``prefix`` prepended. This is byte-for-byte the shape of the scattered
    ``f'{prefix}{uuid.uuid4().hex[:n]}'`` literals it replaces — same hex
    alphabet, same length, same collision domain.

    Args:
        prefix: literal string prepended verbatim (e.g. ``'chatcmpl-'``,
            ``'led_'``, ``''`` for a bare id).
        n: number of hex characters to keep (default 24).

    Returns:
        The composed id string.
    """
    return f'{prefix}{uuid.uuid4().hex[:n]}'


__all__ = ['short_id']
