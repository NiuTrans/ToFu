"""lib/utils.py — Shared utility functions used across the lib/ and routes/ layers.

Provides small, dependency-free helpers that multiple modules need.
"""

import ast
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


def _loads_first_obj(s: str):
    """json.loads with fallback to raw_decode for trailing-garbage ("Extra data") cases.

    ``strict=False`` tolerates raw control characters (literal newlines / tabs)
    inside string values — a common malformation when weaker models emit
    multi-line ``write_file`` content without escaping the newlines. Strict
    parsing rejects these with ``Invalid control character at: ...`` and the
    whole tool call would otherwise be dropped.
    """
    try:
        return json.loads(s, strict=False)
    except json.JSONDecodeError as e:
        if 'Extra data' not in str(e):
            raise
        obj, _ = json.JSONDecoder(strict=False).raw_decode(s)
        if isinstance(obj, dict):
            logger.debug('repair_json: extracted first JSON object, discarded trailing %d chars', len(s) - _)
            return obj
        raise


def _parser_guided_delimiter_fix(s: str, max_fixes: int = 8):
    """Recover ``Expecting ':'/',' delimiter`` errors by inserting the missing char.

    The stdlib JSON parser reports the EXACT byte offset where a structural
    delimiter is missing. We insert the demanded character at ``e.pos`` and
    re-parse, repeating up to *max_fixes* times. This is far safer than a
    regex guess: the parser itself certifies the result by parsing cleanly.

    Only the two delimiter errors are acted on — any other JSONDecodeError
    (unescaped inner quote, bad property name, …) is re-raised so we never
    paper over a genuinely-ambiguous payload. Returns the parsed object or
    raises the last JSONDecodeError.
    """
    cur = s
    for _ in range(max_fixes):
        try:
            return json.loads(cur, strict=False)
        except json.JSONDecodeError as e:
            msg = str(e)
            if "Expecting ':' delimiter" in msg:
                cur = cur[:e.pos] + ':' + cur[e.pos:]
            elif "Expecting ',' delimiter" in msg:
                cur = cur[:e.pos] + ',' + cur[e.pos:]
            else:
                raise
    return json.loads(cur, strict=False)


def _python_literal_fix(s: str) -> dict:
    """Recover single-quoted / Python-dict-repr payloads via ``ast.literal_eval``.

    Weaker models sometimes emit a Python ``dict`` repr instead of JSON
    (single quotes, ``True``/``False``/``None``). ``ast.literal_eval`` parses
    these safely (no code execution) — only literals are evaluated. Result is
    accepted ONLY when it's a dict, mirroring the JSON tool-arg contract.
    """
    val = ast.literal_eval(s)
    if isinstance(val, dict):
        return val
    raise json.JSONDecodeError('ast.literal_eval did not yield a dict', s, 0)


def repair_json(raw: str) -> dict:
    """Best-effort repair of common LLM JSON malformations.

    Handles: trailing commas, unterminated strings, missing closing braces/brackets,
    invalid backslash escape sequences (e.g. ``\\U``, ``\\m``, ``\\.``),
    raw control characters inside string values (literal newlines/tabs),
    trailing garbage after a complete JSON object ("Extra data"),
    structural delimiter errors (missing ``:`` / ``,`` — parser-guided),
    and Python-dict-repr / single-quoted payloads (via ``ast.literal_eval``).
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
        return _loads_first_obj(s)
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
        val = re.sub(r'\\(?!["\\\\/bfnrtu])', r'\\\\', val)
        return val

    s_esc = re.sub(r'"(?:[^"\\]|\\.)*"', _fix_escapes, s)
    if s_esc != s:
        try:
            return _loads_first_obj(s_esc)
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

    try:
        return _loads_first_obj(s)
    except json.JSONDecodeError as e:
        logger.debug('repair_json: structural-balance parse failed, trying delimiter/literal fixes: %s', e)

    # 6. Structural delimiter recovery — insert the exact ':'/',' the parser
    #    demands at its reported offset, then re-validate. Top failure mode in
    #    the log audit (10/13 read_files calls): "Expecting ',' delimiter".
    try:
        obj = _parser_guided_delimiter_fix(s)
        logger.debug('repair_json: recovered via parser-guided delimiter insertion')
        return obj
    except (json.JSONDecodeError, ValueError):
        logger.debug('repair_json: delimiter fix failed, trying python-literal fallback')

    # 7. Python-dict-repr / single-quoted payload — ast.literal_eval (no exec).
    try:
        obj = _python_literal_fix(s)
        logger.debug('repair_json: recovered via ast.literal_eval (python-dict repr)')
        return obj
    except (ValueError, SyntaxError, json.JSONDecodeError):
        logger.debug('repair_json: python-literal fallback failed')

    return _loads_first_obj(s)  # let it raise if still broken


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
