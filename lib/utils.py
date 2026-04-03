"""lib/utils.py — Shared utility functions used across the lib/ and routes/ layers.

Provides small, dependency-free helpers that multiple modules need.
"""

import json
import re

from lib.log import get_logger

__all__ = ['safe_json', 'safe_float', 'repair_json']

logger = get_logger(__name__)


def safe_json(raw, default=None, label=''):
    """Parse a JSON string from DB, returning *default* on failure instead of crashing.

    Parameters
    ----------
    raw : str | None
        The raw JSON string (typically from a DB column).
    default :
        Value to return when *raw* is falsy or unparseable.
    label : str
        Human-readable column/field name for the warning log.
    """
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning('corrupt JSON in DB column %s: %s', label, exc, exc_info=True)
        return default


def repair_json(raw: str) -> dict:
    """Best-effort repair of common LLM JSON malformations.

    Handles: trailing commas, unterminated strings, missing closing braces/brackets,
    invalid backslash escape sequences (e.g. ``\\U``, ``\\m``, ``\\.``).
    Raises json.JSONDecodeError if repair fails.
    """
    s = raw.strip()
    if not s:
        logger.debug('repair_json: empty input, returning {}')
        return {}

    # 1. Strip trailing commas before } or ]
    s = re.sub(r',\s*([}\]])', r'\1', s)

    # 2. Try parsing after comma fix
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        logger.debug('repair_json: initial parse failed, attempting repair on %d-char input', len(s))

    # 2b. Fix invalid \escape sequences inside JSON strings.
    #     Valid JSON escapes: \", \\, \/, \b, \f, \n, \r, \t, \uXXXX
    #     LLMs often produce \U, \m, \. etc. (e.g. Windows paths like C:\Users).
    #     We double the backslash so the invalid escape becomes a literal backslash.
    def _fix_escapes(m):
        """Replace invalid \\X sequences inside a JSON string value with \\\\X."""
        val = m.group(0)
        # Fix \u not followed by exactly 4 hex digits (e.g. \user → \\user)
        val = re.sub(r'\\u(?![0-9a-fA-F]{4})', r'\\\\u', val)
        # Fix remaining invalid escapes: \X where X is NOT one of the valid JSON escapes
        val = re.sub(r'\\(?!["\\\\/ bfnrtu])', r'\\\\', val)
        return val

    s_esc = re.sub(r'"(?:[^"\\]|\\.)*"', _fix_escapes, s)
    if s_esc != s:
        try:
            return json.loads(s_esc)
        except json.JSONDecodeError:
            logger.debug('repair_json: escape-fix parse failed, continuing repair')
        s = s_esc  # keep the escape fix for subsequent repairs

    # 3. Fix unterminated strings (odd number of unescaped quotes)
    quote_count = len(re.findall(r'(?<!\\)"', s))
    if quote_count % 2 == 1:
        s += '"'

    # 4. Balance braces / brackets
    opens = s.count('{') - s.count('}')
    opens_b = s.count('[') - s.count(']')
    s += ']' * max(opens_b, 0)
    s += '}' * max(opens, 0)

    # 5. Strip trailing commas again (may appear after quote closure)
    s = re.sub(r',\s*([}\]])', r'\1', s)

    return json.loads(s)  # let it raise if still broken


# Backward-compat alias: old code used the underscore-prefixed name
_repair_json = repair_json


def safe_float(v, default=0.0):
    """Parse a numeric value to float, returning *default* on failure.

    Handles common sentinel values from web-scraped financial data:
    empty string, '-', '--', and None.
    """
    try:
        if v in ('', '-', '--', None):
            return default
        return float(v)
    except (ValueError, TypeError):
        logger.debug('safe_float: cannot convert %r to float, returning default %s', v, default, exc_info=True)
        return default
