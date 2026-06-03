#!/usr/bin/env python3
"""Migration script: mechanically rewrite `return jsonify({...}), N` to
the unified `lib.api_response` helpers across all routes/*.py files.

Patterns handled
----------------
P1: return jsonify({'ok': True})                         → return api_ok()
P2: return jsonify({'ok': True, **kwargs})               → return api_ok({**kwargs})
P3: return jsonify({'error': 'msg'}), 400                → return api_bad_request('msg')
P4: return jsonify({'error': 'msg'}), 401                → return api_unauthorized('msg')
P5: return jsonify({'error': 'msg'}), 403                → return api_forbidden('msg')
P6: return jsonify({'error': 'msg'}), 404                → return api_not_found('msg')
P7: return jsonify({'error': 'msg'}), 409                → return api_conflict('msg')
P8: return jsonify({'error': 'msg'}), 500                → return api_internal_error('msg')
P9: return jsonify({'ok': False, 'error': 'msg'}), N     → corresponding helper

Patterns left ALONE (caller will keep using jsonify directly):
- multiline jsonify(...) — too complex to rewrite mechanically
- jsonify({...}), 200 with extra fields — usage varies too much
- response objects that have .headers set after jsonify
- single-arg return jsonify(<list>) (paginated lists)

Usage:
    python tests/_migrate_api_response.py            # dry-run
    python tests/_migrate_api_response.py --apply    # write changes
    python tests/_migrate_api_response.py --file foo # restrict to one file
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from glob import glob

ROUTES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'routes')

# ── Regex patterns (single-line only) ──────────────────────────────

# Match: return jsonify({'ok': True})
_P1 = re.compile(r"return jsonify\(\{['\"]ok['\"]:\s*True\}\)\s*$", re.MULTILINE)

# Match: return jsonify({'error': 'literal string'}), <status>
_P_ERR_STR = re.compile(
    r"return jsonify\(\{['\"]error['\"]:\s*(['\"][^'\"]*['\"])\}\),\s*(\d{3})"
)

# Match: return jsonify({'error': str(e)}), <status>
_P_ERR_STR_E = re.compile(
    r"return jsonify\(\{['\"]error['\"]:\s*str\((\w+)\)\}\),\s*(\d{3})"
)

# Match: return jsonify({'error': f'...'}), <status>  — preserve f-string
_P_ERR_FSTRING = re.compile(
    r"return jsonify\(\{['\"]error['\"]:\s*(f['\"][^'\"]*['\"])\}\),\s*(\d{3})"
)

# Match: return jsonify({'ok': False, 'error': 'literal'}), <status>
_P_OK_FALSE_STR = re.compile(
    r"return jsonify\(\{['\"]ok['\"]:\s*False,\s*['\"]error['\"]:\s*(['\"][^'\"]*['\"])\}\),\s*(\d{3})"
)

# Match: return jsonify({'ok': False, 'error': str(e)}), <status>
_P_OK_FALSE_STR_E = re.compile(
    r"return jsonify\(\{['\"]ok['\"]:\s*False,\s*['\"]error['\"]:\s*str\((\w+)\)\}\),\s*(\d{3})"
)

# Match: return jsonify({'ok': False, 'error': str(e)[:N]}), <status>
_P_OK_FALSE_STR_E_TRUNC = re.compile(
    r"return jsonify\(\{['\"]ok['\"]:\s*False,\s*['\"]error['\"]:\s*str\((\w+)\)\[:\d+\]\}\),\s*(\d{3})"
)

# Map status codes to helper names. Codes not listed here are
# rewritten via the generic api_error(..., status=N) helper.
_STATUS_HELPERS = {
    '400': 'api_bad_request',
    '401': 'api_unauthorized',
    '403': 'api_forbidden',
    '404': 'api_not_found',
    '405': 'api_method_not_allowed',
    '409': 'api_conflict',
    '413': 'api_payload_too_large',
    '500': 'api_internal_error',
}

# Status codes we'll rewrite to api_error(..., status=N)
_GENERIC_STATUSES = {'502', '503', '504'}


def _emit(msg, status):
    """Pick the most specific helper for this status, or fall back to api_error."""
    helper = _STATUS_HELPERS.get(status)
    if helper:
        return f'return {helper}({msg})'
    if status in _GENERIC_STATUSES:
        return f'return api_error({msg}, status={status})'
    return None  # caller should leave the original unchanged


def _helpers_used(new_src: str, old_src: str) -> set[str]:
    """Detect which api_response helpers are now referenced."""
    used = set()
    for h in (
        'api_ok', 'api_created', 'api_no_content',
        'api_error', 'api_bad_request', 'api_not_found',
        'api_unauthorized', 'api_forbidden', 'api_conflict',
        'api_payload_too_large', 'api_method_not_allowed',
        'api_internal_error',
    ):
        # Only count NEW usage (introduced by this script's rewrites)
        # by looking at the diff between old and new sources.
        old_count = len(re.findall(rf'\b{h}\b', old_src))
        new_count = len(re.findall(rf'\b{h}\b', new_src))
        if new_count > old_count:
            used.add(h)
    return used


def _ensure_imports(src: str, helpers: set[str]) -> str:
    """Add `from lib.api_response import ...` to the file."""
    if not helpers:
        return src

    # Check if there's already an api_response import line
    import_line_re = re.compile(
        r"^from lib\.api_response import (.+)$", re.MULTILINE)
    m = import_line_re.search(src)
    if m:
        # Merge with existing
        existing = {x.strip() for x in m.group(1).split(',') if x.strip()}
        merged = sorted(existing | helpers)
        new_line = f"from lib.api_response import {', '.join(merged)}"
        return src.replace(m.group(0), new_line)

    # No existing import — insert after the last lib.* import.
    # We anchor on the existing `from lib.log import ...` line if present,
    # else after the first "from lib." line, else after the first import.
    insertion = f"from lib.api_response import {', '.join(sorted(helpers))}\n"

    log_re = re.compile(r"^(from lib\.log import [^\n]+)\n", re.MULTILINE)
    m = log_re.search(src)
    if m:
        idx = m.end()
        return src[:idx] + insertion + src[idx:]

    lib_re = re.compile(r"^(from lib\.[^\s]+ import [^\n]+)\n", re.MULTILINE)
    matches = list(lib_re.finditer(src))
    if matches:
        # Insert after the LAST `from lib.X` line so ordering stays alphabetical-ish
        idx = matches[-1].end()
        return src[:idx] + insertion + src[idx:]

    # Fallback: after first `from flask import ...`
    flask_re = re.compile(r"^(from flask import [^\n]+)\n", re.MULTILINE)
    m = flask_re.search(src)
    if m:
        return src[:m.end()] + insertion + src[m.end():]

    # Last resort: top of file (after module docstring, if any)
    return insertion + src


def rewrite(src: str) -> tuple[str, dict[str, int]]:
    """Apply all rewrite rules. Returns (new_src, counts_per_pattern)."""
    counts: dict[str, int] = {}

    def _bump(name: str):
        counts[name] = counts.get(name, 0) + 1

    new_src = src

    # P1: return jsonify({'ok': True})
    def _sub_p1(m):
        _bump('ok_true')
        return 'return api_ok()'
    new_src = _P1.sub(_sub_p1, new_src)

    # P_OK_MULTI: return jsonify({'ok': True, 'foo': bar, ...})  → api_ok({...})
    # Limited to single-line jsonify calls. Captures everything after
    # 'ok': True, and re-emits as api_ok({...}) preserving the dict literal.
    _P_OK_MULTI = re.compile(
        r"return jsonify\(\{['\"]ok['\"]:\s*True,\s*([^}]+?)\}\)\s*$",
        re.MULTILINE,
    )

    def _sub_ok_multi(m):
        rest = m.group(1).strip().rstrip(',').strip()
        # Don't touch if it contains a nested dict/function call we can't safely re-emit
        if '(' in rest and rest.count('(') != rest.count(')'):
            _bump('skipped_multi_complex')
            return m.group(0)
        _bump('ok_true_multi')
        return f'return api_ok({{{rest}}})'
    new_src = _P_OK_MULTI.sub(_sub_ok_multi, new_src)

    # P_OK_FALSE_STR: return jsonify({'ok': False, 'error': 'msg'}), N
    def _sub_ok_false_str(m):
        msg, status = m.group(1), m.group(2)
        repl = _emit(msg, status)
        if repl is None:
            _bump('skipped_unknown_status')
            return m.group(0)
        _bump(f'ok_false_str_{status}')
        return repl
    new_src = _P_OK_FALSE_STR.sub(_sub_ok_false_str, new_src)

    # P_OK_FALSE_STR_E_TRUNC: return jsonify({'ok': False, 'error': str(e)[:N]}), N
    def _sub_ok_false_str_e_trunc(m):
        var, status = m.group(1), m.group(2)
        repl = _emit(var, status)
        if repl is None:
            _bump('skipped_unknown_status')
            return m.group(0)
        _bump(f'ok_false_str_e_trunc_{status}')
        return repl
    new_src = _P_OK_FALSE_STR_E_TRUNC.sub(_sub_ok_false_str_e_trunc, new_src)

    # P_OK_FALSE_STR_E: return jsonify({'ok': False, 'error': str(e)}), N
    def _sub_ok_false_str_e(m):
        var, status = m.group(1), m.group(2)
        repl = _emit(var, status)
        if repl is None:
            _bump('skipped_unknown_status')
            return m.group(0)
        _bump(f'ok_false_str_e_{status}')
        return repl
    new_src = _P_OK_FALSE_STR_E.sub(_sub_ok_false_str_e, new_src)

    # P_ERR_STR: return jsonify({'error': 'msg'}), N
    def _sub_err_str(m):
        msg, status = m.group(1), m.group(2)
        repl = _emit(msg, status)
        if repl is None:
            _bump('skipped_unknown_status')
            return m.group(0)
        _bump(f'err_str_{status}')
        return repl
    new_src = _P_ERR_STR.sub(_sub_err_str, new_src)

    # P_ERR_FSTRING: return jsonify({'error': f'...'}), N
    def _sub_err_fstring(m):
        msg, status = m.group(1), m.group(2)
        repl = _emit(msg, status)
        if repl is None:
            _bump('skipped_unknown_status')
            return m.group(0)
        _bump(f'err_fstring_{status}')
        return repl
    new_src = _P_ERR_FSTRING.sub(_sub_err_fstring, new_src)

    # P_ERR_STR_E: return jsonify({'error': str(e)}), N
    def _sub_err_str_e(m):
        var, status = m.group(1), m.group(2)
        repl = _emit(var, status)
        if repl is None:
            _bump('skipped_unknown_status')
            return m.group(0)
        _bump(f'err_str_e_{status}')
        return repl
    new_src = _P_ERR_STR_E.sub(_sub_err_str_e, new_src)

    return new_src, counts


def process_file(path: str, apply: bool) -> tuple[bool, dict[str, int]]:
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    orig = src

    new_src, counts = rewrite(src)
    if new_src == orig:
        return False, counts

    helpers = _helpers_used(new_src, orig)
    new_src = _ensure_imports(new_src, helpers)

    if apply:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_src)

    return True, counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true',
                    help='Write changes (default: dry-run)')
    p.add_argument('--file', default='', help='Restrict to a single file basename')
    args = p.parse_args()

    files = sorted(glob(os.path.join(ROUTES_DIR, '*.py')))
    if args.file:
        files = [f for f in files if os.path.basename(f) == args.file]

    total_counts: dict[str, int] = {}
    files_changed = 0
    for path in files:
        # Skip __init__ and the task-routes factory
        if os.path.basename(path) in ('__init__.py', '_task_routes.py', 'push.py'):
            continue
        changed, counts = process_file(path, apply=args.apply)
        if changed:
            files_changed += 1
            print(f'  {os.path.basename(path):35s} '
                  + ', '.join(f'{k}={v}' for k, v in sorted(counts.items())))
        for k, v in counts.items():
            total_counts[k] = total_counts.get(k, 0) + v

    total_rewrites = sum(total_counts.values())
    mode = 'APPLIED' if args.apply else 'DRY-RUN'
    print()
    print(f'  ═══ {mode}: {files_changed} files, {total_rewrites} rewrites ═══')
    print()
    for k, v in sorted(total_counts.items()):
        print(f'    {k:35s} {v}')


if __name__ == '__main__':
    main()
