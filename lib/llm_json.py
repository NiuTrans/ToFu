"""lib/llm_json.py — Shared helpers for parsing JSON out of LLM output.

Several independent subsystems ask an LLM for "STRICT JSON only" and then
have to defend against the model wrapping it in a ```json fence, emitting
prose around it, or truncating mid-object at the token ceiling. Before this
module each grew its own copy:

  * ``lib/optimizer/proposer.py:_strip_fences``       (fence strip)
  * ``lib/orchestration_composer.py:_strip_fences``   (byte-identical copy)
  * ``lib/orchestration_composer.py:_extract_json``   (fence + first {...})
  * ``lib/scheduler/_shared.py:parse_json_decision``  (inline fence strip)
  * ``lib/daily_report/llm.py:_extract_json_result``  (fence + brace-walk +
                                                        truncation repair)

This module is the single home for that logic. It is deliberately
dependency-free (stdlib ``json`` + ``re`` only) so any layer can import it
without a circular-import risk.

Public API
----------
  strip_code_fences(text) -> str
      Remove a leading/trailing markdown code fence (```lang ... ```).

  extract_json(text, *, repair=False) -> object | None
      Parse the first JSON value out of ``text``. Tries a direct parse of
      the fence-stripped string, then the first balanced ``{...}`` / ``[...]``
      block (brace-depth aware, string-literal aware), then — when
      ``repair=True`` — a best-effort salvage of output truncated
      mid-generation. Returns the parsed object/list, or ``None``.
"""

from __future__ import annotations

import json
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def strip_code_fences(text: str | None) -> str:
    """Strip a single leading/trailing markdown code fence.

    Handles ```` ```json ```` / ```` ``` ```` openers (with or without a
    language tag) and the matching closing fence. Mirrors the behaviour of
    the former ``_strip_fences`` copies: drop the opening fence line, then
    the trailing fence, and ``strip()`` the result. Text with no fence is
    returned stripped, unchanged.

    Args:
        text: Raw LLM output (or ``None``).

    Returns:
        The content with any wrapping fence removed.
    """
    s = (text or '').strip()
    if s.startswith('```'):
        # Drop the opening fence line (```json / ``` / ```python …).
        s = s.split('\n', 1)[-1] if '\n' in s else s[3:]
        if s.endswith('```'):
            s = s[:-3]
    return s.strip()


def _first_balanced_block(s: str) -> Any | None:
    """Return the first balanced ``{...}`` / ``[...]`` JSON value in ``s``.

    Brace-depth + string-literal aware so a ``}`` inside a string does not
    close the block prematurely. Tries whichever of ``{`` / ``[`` appears
    first. Returns the parsed value or ``None``.
    """
    for opener, closer in (('{', '}'), ('[', ']')):
        start = s.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(s)):
            ch = s[i]
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug('[llm_json] balanced-block parse failed: %s', e)
                    break
    return None


def _repair_truncated_json(s: str) -> Any | None:
    """Best-effort repair of JSON truncated mid-generation.

    Walks the fragment tracking the bracket/string stack, trims back to the
    last complete value, then appends the missing closers and re-parses.
    Returns the parsed object, or ``None`` if it can't be salvaged.
    """
    start = min((i for i in (s.find('{'), s.find('[')) if i != -1), default=-1)
    if start == -1:
        return None

    stack: list[str] = []
    in_string = False
    escape_next = False
    last_safe = -1

    for i in range(start, len(s)):
        ch = s[i]
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if ch == '\\':
                escape_next = True
            elif ch == '"':
                in_string = False
                last_safe = i + 1
            continue
        if ch == '"':
            in_string = True
        elif ch in '{[':
            stack.append('}' if ch == '{' else ']')
        elif ch in '}]':
            if stack:
                stack.pop()
            last_safe = i + 1
        elif ch in '0123456789truefalsenul.-+eE':
            last_safe = i + 1
        elif ch in ' \t\r\n,:':
            if ch in ' \t\r\n':
                last_safe = max(last_safe, i)

    if last_safe <= start:
        return None

    fragment = s[start:last_safe].rstrip().rstrip(',')
    candidate = fragment + ''.join(reversed(stack))
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[llm_json] truncated-JSON repair failed: %s', e)
        return None


def extract_json(text: str | None, *, repair: bool = False) -> Any | None:
    """Parse the first JSON value out of (possibly messy) LLM output.

    Strategy, in order:
      1. Strip a wrapping markdown fence and ``json.loads`` the whole thing.
      2. Fall back to the first balanced ``{...}`` / ``[...]`` block
         (brace-depth + string aware).
      3. When ``repair=True``, salvage output that was truncated
         mid-generation (unbalanced) by closing the open containers.

    Args:
        text: Raw LLM output (or ``None``).
        repair: Enable the truncation-repair last resort. Off by default so
            the base behaviour matches the former ``_extract_json`` copies.

    Returns:
        The parsed object/list, or ``None`` when nothing parseable is found.
    """
    if not text:
        return None
    s = strip_code_fences(text)
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[llm_json] direct parse failed, trying extraction: %s', e)

    block = _first_balanced_block(s)
    if block is not None:
        return block

    if repair:
        return _repair_truncated_json(s)
    return None


__all__ = ['strip_code_fences', 'extract_json']
