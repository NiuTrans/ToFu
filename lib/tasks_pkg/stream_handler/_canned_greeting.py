"""Canned-greeting upstream-artifact detector (pure decision logic).

2026-07-28 06:25→ incident: the sankuai gateway's only Opus 5 request-id
(``yuju-claude-opus-5-evaDaily``, a daily eval build) began intermittently
answering ANY request — including mid-tool-work continuations with 90–380k
cached tokens of context — with the identical 29-char greeting
``"Hi! How can I help you today?"`` and a CLEAN ``finish_reason=stop`` plus a
real M-TraceId and real usage. 68+ events in ~5 hours across 14
conversations, on every API key. Every transport-level guard (zero-byte /
premature-close / empty-stop) keys off MISSING output, so this "successful"
degenerate response sailed through, ended the turn, and was persisted over
the accumulated tool work.

The shape is undetectable at the transport layer but trivially detectable by
CONTENT + INCONGRUENCE: a greeting opener is only a legitimate reply when the
conversation's last real user turn was itself greeting-shaped small-talk. A
canned greeting answering a brain kickoff / a real question / a tool result
is an upstream artifact and must be retried, not accepted.

Pure and side-effect free so the verdict is unit-testable without a DB /
route harness (project NC-bite discipline); the retry mechanics live in
``_analyse.py`` and the VU relay guard in ``autopilot.py``.
"""

from __future__ import annotations

import re

from lib.log import get_logger

logger = get_logger(__name__)


# A canned greeting is short by construction. The observed artifact is 29
# chars; the whole opener family (en + zh, with pleasantry padding) stays
# well under 60. A >60-char message that merely STARTS with a greeting is a
# real answer and must never be retried.
_CANNED_GREETING_MAX_CHARS = 60

# Opener-greeting family: a salutation, up to a few chars of pleasantry,
# then an offer-to-help. The middle window is deliberately bounded so
# "Hi! Here is the 40-char answer to your question" cannot match.
_CANNED_GREETING_RE = re.compile(
    r'^\s*(?:hi|hello|hey|greetings|good\s+(?:morning|afternoon|evening|day)'
    r'|你好|您好|嗨|哈喽)'
    r'[\s!！?？,，.。·~～:：;；—–-]{0,3}'
    r'.{0,24}?'
    r'(?:how\s+(?:can|may|could)\s+i\s+(?:help|assist)'
    r'|what\s+can\s+i\s+do|can\s+i\s+(?:help|assist)'
    r'|我能为您做些什么|我可以为你做些什么|有什么可以帮'
    r'|需要.{0,6}帮助|怎么帮|如何帮)',
    re.IGNORECASE | re.DOTALL,
)

# The complement: a last-user-message that is PURE small-talk makes a
# greeting reply legitimate. Kept tight — salutations and bare thanks only,
# so "继续" / "?" / a brain kickoff never read as an invitation to greet.
_SMALLTALK_MAX_CHARS = 30
_SMALLTALK_RE = re.compile(
    r'^\s*(?:hi+|hello+|hey+|yo|hiya|howdy|greetings'
    r'|good\s+(?:morning|afternoon|evening|day)'
    r'|你好|您好|嗨|哈喽|在吗|在么|早|早上好|上午好|下午好|晚上好'
    r'|thanks|thank\s+you|thx|谢谢|感谢|多谢)'
    r'[\s!！?？,，.。·~～:：;；—–-]*$',
    re.IGNORECASE,
)


def _message_text(content) -> str:
    """Flatten a message's content (str or Anthropic-style block list) to text.

    Tool-result / image blocks carry no ``text`` field and flatten to '' —
    which is correct: a greeting answering a tool result is incongruent, and
    '' never matches the small-talk complement, so the retry still fires.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(
            str(b.get('text', '')) for b in content
            if isinstance(b, dict) and b.get('type') == 'text'
        )
    return ''


def last_user_is_smalltalk(messages: list) -> bool:
    """True iff the last user-role message is pure greeting/thanks small-talk."""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get('role') == 'user':
            text = _message_text(m.get('content')).strip()
            if not text or len(text) > _SMALLTALK_MAX_CHARS:
                return False
            return bool(_SMALLTALK_RE.match(text))
    # No user turn at all: nothing was asked, so a greeting cannot be a
    # legitimate answer. Not small-talk.
    return False


def is_canned_greeting_reply(content, messages) -> bool:
    """True iff ``content`` is a canned greeting that is incongruent here.

    Three gates, each a distinct no-false-positive boundary:
      * short (<= ``_CANNED_GREETING_MAX_CHARS``) — real answers are longer;
      * matches the opener-greeting family — "好的。" / "You're welcome!" /
        real prose never do;
      * the last real user turn is NOT small-talk — a user who said "你好"
        or "在吗" made the greeting legitimate, so it is never retried.

    ``messages`` empty → False (incongruence unprovable; fail safe toward
    no-retry). No user turn at all → True (nothing was asked; a greeting is
    definitionally an artifact).
    """
    text = (content or '').strip()
    if not text or len(text) > _CANNED_GREETING_MAX_CHARS:
        return False
    if not _CANNED_GREETING_RE.search(text):
        return False
    if not messages:
        logger.debug('[canned-greeting] messages unavailable — not retrying '
                     '(fail-safe): %.60s', text)
        return False
    return not last_user_is_smalltalk(messages)
