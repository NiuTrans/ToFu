"""lib/utils.py — Shared utility functions used across the lib/ and routes/ layers.

Provides small, dependency-free helpers that multiple modules need.
"""

import ast
import copy
import hashlib
import json
import re
import sys

from lib.log import get_logger

__all__ = ['safe_json', 'safe_float', 'repair_json', 'fast_deepcopy',
           'fast_deepcopy_with_retained_bytes', 'retained_graph_bytes',
           'HASHLIB_GIL_RELEASE_BYTES', 'sha256_holding_gil', 'update_holding_gil']

logger = get_logger(__name__)

_DEEPCOPY_ATOMIC_TYPES = frozenset({type(None), bool, int, float, complex, str, bytes})
# Existing task-event accounting includes one owner/callback allowance per graph.
_DETACHED_GRAPH_OVERHEAD_BYTES = 1024


def fast_deepcopy(value):
    """Return the same result as ``copy.deepcopy`` with a builtin-tree fast path.

    Exact ``dict``/``list`` containers are copied inline and atomic scalars are
    shared, as ``copy.deepcopy`` does. Every other type (subclasses, tuples,
    sets, objects with ``__deepcopy__``) is delegated to ``copy.deepcopy``
    with the same memo, so shared and cyclic references keep their identity
    relationships. Event snapshots and routing aggregates are almost entirely
    builtin JSON trees, where the generic per-node dispatch dominated.
    """
    return _fast_deepcopy(value, {})


def _keep_alive_during_copy(value, memo):
    # Same guard as copy.deepcopy: a source container freed mid-copy (a live
    # task graph mutated by another thread) must not let a new object reuse
    # its id and alias the memoized copy.
    try:
        memo[id(memo)].append(value)
    except KeyError:
        memo[id(memo)] = [value]


def _fast_deepcopy(value, memo):
    kind = type(value)
    if kind in _DEEPCOPY_ATOMIC_TYPES:
        return value
    if kind is dict:
        copied = memo.get(id(value))
        if copied is not None:
            return copied
        result = {}
        memo[id(value)] = result
        _keep_alive_during_copy(value, memo)
        for key, child in value.items():
            if type(key) not in _DEEPCOPY_ATOMIC_TYPES:
                key = _fast_deepcopy(key, memo)
            result[key] = (child if type(child) in _DEEPCOPY_ATOMIC_TYPES
                           else _fast_deepcopy(child, memo))
        return result
    if kind is list:
        copied = memo.get(id(value))
        if copied is not None:
            return copied
        result = []
        memo[id(value)] = result
        _keep_alive_during_copy(value, memo)
        append = result.append
        for child in value:
            append(child if type(child) in _DEEPCOPY_ATOMIC_TYPES
                   else _fast_deepcopy(child, memo))
        return result
    return copy.deepcopy(value, memo)


def retained_graph_bytes(*values) -> int:
    """Charge each reachable dict/list/tuple graph object once by identity."""
    pending = list(values)
    seen = set()
    total = _DETACHED_GRAPH_OVERHEAD_BYTES
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return total


class _MeasuredGraphCopy:
    """Fuse builtin-tree copying and identity accounting without a second walk.

    The unmeasured fast path above stays unchanged. Non-builtin deepcopy hooks
    may replace keys or return arbitrary aliases; those retain the existing
    deepcopy implementation and measure the final reachable result instead.
    """

    def __init__(self):
        self.memo = {}
        self.seen = set()
        self.retained_bytes = _DETACHED_GRAPH_OVERHEAD_BYTES
        self.used_fallback = False

    def copy(self, value):
        kind = type(value)
        identity = id(value)
        seen = self.seen
        if kind in _DEEPCOPY_ATOMIC_TYPES:
            if identity not in seen:
                seen.add(identity)
                self.retained_bytes += sys.getsizeof(value)
            return value
        memo = self.memo
        if identity in memo:
            return memo[identity]
        getsize = sys.getsizeof
        charge = 0
        if kind is dict:
            result = {}
            memo[identity] = result
            _keep_alive_during_copy(value, memo)
            seen.add(id(result))
            for key, child in value.items():
                if type(key) not in _DEEPCOPY_ATOMIC_TYPES:
                    key = self.copy(key)
                elif id(key) not in seen:
                    seen.add(id(key))
                    charge += getsize(key)
                if type(child) not in _DEEPCOPY_ATOMIC_TYPES:
                    child = self.copy(child)
                elif id(child) not in seen:
                    seen.add(id(child))
                    charge += getsize(child)
                result[key] = child
            self.retained_bytes += charge + getsize(result)
            return result
        if kind is list:
            result = []
            memo[identity] = result
            _keep_alive_during_copy(value, memo)
            seen.add(id(result))
            append = result.append
            for child in value:
                if type(child) not in _DEEPCOPY_ATOMIC_TYPES:
                    child = self.copy(child)
                elif id(child) not in seen:
                    seen.add(id(child))
                    charge += getsize(child)
                append(child)
            self.retained_bytes += charge + getsize(result)
            return result
        self.used_fallback = True
        return copy.deepcopy(value, memo)


def fast_deepcopy_with_retained_bytes(value):
    """Return a detached graph and its exact existing retained-byte charge.

    Shared/cyclic references use the same memo as ``fast_deepcopy``. The
    result's allocator sizes, not source sizes or JSON lengths, own the charge.
    """
    owner = _MeasuredGraphCopy()
    result = owner.copy(value)
    retained = (retained_graph_bytes(result) if owner.used_fallback
                else owner.retained_bytes)
    return result, retained


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


def _bracket_match_fix(s: str) -> str:
    """Rewrite mismatched closing brackets to the type the open stack demands.

    A distinct malformation from a *missing* closer: the model closes a
    container with the WRONG bracket type — e.g. it writes
    ``[{"path": "x", "end_line": 214]`` (the object closed with ``]`` instead
    of ``}``). Neither the count-and-append balance step nor the parser-guided
    ``:``/``,`` insertion can recover this: one only appends closers, the other
    only inserts delimiters — neither *changes* a wrong closer.

    This walks the string tracking the open-container stack (string-literal
    and escape aware), rewrites each ``}``/``]`` to match its innermost opener,
    and appends any still-missing closers in reverse-open order. For
    well-formed JSON every closer already matches and the stack empties cleanly
    → the output is byte-identical, so this can never corrupt a valid payload.
    """
    out = list(s)
    stack = []          # open brackets, e.g. ['[', '{']
    in_str = False
    escaped = False
    closer_for = {'{': '}', '[': ']'}
    for i, ch in enumerate(out):
        if in_str:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in '{[':
            stack.append(ch)
        elif ch in '}]':
            if not stack:
                continue  # stray closer — leave it, let the parse fail honestly
            want = closer_for[stack.pop()]
            if ch != want:
                out[i] = want
    tail = ''.join(closer_for[b] for b in reversed(stack))
    return ''.join(out) + tail


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
        logger.debug('repair_json: delimiter fix failed, trying bracket-match fix')

    # 6b. Mismatched-closer recovery — rewrite a wrong-type closing bracket
    #     (``[{...}]`` written as ``[{...]``) to match the open stack, then
    #     re-run the delimiter fix (the corrected closer can expose a still-
    #     missing ',' one level up). No-op on well-formed JSON.
    s_bm = _bracket_match_fix(s)
    if s_bm != s:
        try:
            obj = _parser_guided_delimiter_fix(s_bm)
            logger.debug('repair_json: recovered via bracket-match fix')
            return obj
        except (json.JSONDecodeError, ValueError):
            logger.debug('repair_json: bracket-match fix failed, trying python-literal fallback')

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


# CPython's hashlib releases the GIL for every update of at least this many
# bytes (HASHLIB_GIL_MINSIZE in Modules/hashlib.h). Digesting many medium
# buffers in a loop therefore hands the GIL away once per item and, beside a
# runnable thread, waits up to a switch interval (5 ms) to take it back; in
# round preparation that handoff, not hashing, dominated snapshot projection.
# Slices below the threshold keep the GIL for the whole digest; the interpreter
# still yields at its normal switch interval between slices.
HASHLIB_GIL_RELEASE_BYTES = 2048
_GIL_HELD_SLICE_BYTES = HASHLIB_GIL_RELEASE_BYTES - 1


def update_holding_gil(hasher, data: bytes) -> None:
    """Equivalent to ``hasher.update(data)``; never releases the GIL."""
    size = len(data)
    if size < HASHLIB_GIL_RELEASE_BYTES:
        hasher.update(data)
        return
    view = memoryview(data)
    for start in range(0, size, _GIL_HELD_SLICE_BYTES):
        hasher.update(view[start:start + _GIL_HELD_SLICE_BYTES])


def sha256_holding_gil(data: bytes = b''):
    """Equivalent to ``hashlib.sha256(data)``; never releases the GIL."""
    hasher = hashlib.sha256()
    update_holding_gil(hasher, data)
    return hasher
