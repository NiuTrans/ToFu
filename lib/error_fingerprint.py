"""lib/error_fingerprint.py — Stable error-signature fingerprinting.

A single, dependency-free primitive shared by the two ends of Tofu's
self-diagnosis feedback loop:

  * ``lib/tasks_pkg/executor.py`` stamps a ``fingerprint`` onto every
    structured ``tool_error`` audit event at the moment a tool fails.
  * ``lib/optimizer/analyzer.py`` groups those events (and raw error-log
    lines) by fingerprint to surface *recurring / unresolved* issues —
    the capability the removed ``project_error_tracker.py`` once provided.

The fingerprint normalises the volatile parts of a message (digits,
hex ids, quoted paths/urls, memory addresses, uuids) so two occurrences
of the same underlying failure collapse to one signature, while
genuinely different failures stay distinct.

Kept deliberately tiny and import-light: the executor is a hot path and
must not pull in heavy modules just to tag an error.
"""

from __future__ import annotations

import re

# Order matters: collapse the most specific volatile tokens first so a
# later, broader rule can't shadow them.
_UUID_RE = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
                      r'[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE)
_HEXADDR_RE = re.compile(r'\b0x[0-9a-fA-F]+\b')
_LONGHEX_RE = re.compile(r'\b[0-9a-f]{16,}\b', re.IGNORECASE)
_QUOTED_RE = re.compile(r"""(['"])(?:\\.|(?!\1).)*\1""")
_PATH_RE = re.compile(r'(?:/[\w.\-]+){2,}')
_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)
_NUM_RE = re.compile(r'\d+')
_WS_RE = re.compile(r'\s+')

# Cap so a pathological multi-KB exception message can't bloat audit lines
# or the optimizer prompt.
_MAX_LEN = 200


def fingerprint(text: str, *, exc_type: str = '') -> str:
    """Return a stable, low-cardinality signature for an error message.

    Args:
        text: The raw error / exception message (``str(exc)``).
        exc_type: Optional exception class name to prefix, so that two
            different exception types with similar text don't collide
            (e.g. ``ValueError: bad x`` vs ``KeyError: bad x``).

    Returns:
        A normalised signature string such as
        ``"ValueError: unknown root '<S>' for path <PATH>"`` →
        ``"valueerror: unknown root <s> for path <path>"`` with all
        volatile tokens replaced by placeholders. Empty input yields
        ``"<empty>"`` (so callers always get a groupable key).
    """
    s = (text or '').strip()
    if not s:
        return f'{exc_type.lower()}: <empty>' if exc_type else '<empty>'

    s = _URL_RE.sub('<URL>', s)
    s = _UUID_RE.sub('<UUID>', s)
    s = _HEXADDR_RE.sub('<ADDR>', s)
    s = _PATH_RE.sub('<PATH>', s)
    s = _QUOTED_RE.sub('<S>', s)
    s = _LONGHEX_RE.sub('<HEX>', s)
    s = _NUM_RE.sub('#', s)
    s = _WS_RE.sub(' ', s).strip()
    s = s[:_MAX_LEN]

    sig = s.lower()
    if exc_type:
        sig = f'{exc_type.lower()}: {sig}'
    return sig
