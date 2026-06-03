#!/usr/bin/env python3
"""Migration script: rewrite ``data = request.get_json(silent=True) or {}``
to ``data = parse_body()`` across all routes/*.py files.

Pattern rewritten:
    data = request.get_json(silent=True) or {}        → data = parse_body()
    data = request.get_json(force=True, silent=True) or {}  → data = parse_body(force=True)

Pattern LEFT ALONE:
    data = request.get_json(force=True)              → manual review (no silent=True;
                                                       semantics differ on bad JSON)
    request.get_json(...)                              direct call without dict-coalesce

Strategy: minimal, conservative regex. Only rewrites when the entire RHS
is the well-known idiom. Adds ``parse_body`` to the
``from lib.request_parser import …`` line (creates if absent).

Usage:
    python tests/_migrate_request_parser.py            # dry-run
    python tests/_migrate_request_parser.py --apply    # apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from glob import glob

ROUTES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'routes')


_RE_PLAIN = re.compile(
    r"^(\s*)(\w+)\s*=\s*request\.get_json\(silent=True\)\s*or\s*\{\}\s*$",
    re.MULTILINE,
)
_RE_FORCE = re.compile(
    r"^(\s*)(\w+)\s*=\s*request\.get_json\(force=True,\s*silent=True\)\s*or\s*\{\}\s*$",
    re.MULTILINE,
)


def _ensure_imports(src: str) -> str:
    """Add parse_body to the request_parser import line, creating it if absent."""
    line_re = re.compile(
        r"^from lib\.request_parser import (.+)$", re.MULTILINE)
    m = line_re.search(src)
    if m:
        existing = {x.strip() for x in m.group(1).split(',') if x.strip()}
        if 'parse_body' in existing:
            return src
        merged = sorted(existing | {'parse_body'})
        new_line = f"from lib.request_parser import {', '.join(merged)}"
        return src.replace(m.group(0), new_line)

    # Insert after the existing api_response import (most files now have one)
    api_re = re.compile(
        r"^(from lib\.api_response import [^\n]+)\n", re.MULTILINE)
    m = api_re.search(src)
    insertion = "from lib.request_parser import parse_body\n"
    if m:
        return src[:m.end()] + insertion + src[m.end():]

    # Fallback: after first lib.* import
    lib_re = re.compile(r"^(from lib\.[^\s]+ import [^\n]+)\n", re.MULTILINE)
    matches = list(lib_re.finditer(src))
    if matches:
        idx = matches[-1].end()
        return src[:idx] + insertion + src[idx:]

    # Last resort: after first flask import
    fl_re = re.compile(r"^(from flask import [^\n]+)\n", re.MULTILINE)
    m = fl_re.search(src)
    if m:
        return src[:m.end()] + insertion + src[m.end():]

    return insertion + src


def rewrite(src: str) -> tuple[str, int]:
    n = 0

    def _sub_plain(m):
        nonlocal n
        n += 1
        indent, var = m.group(1), m.group(2)
        return f'{indent}{var} = parse_body()'

    def _sub_force(m):
        nonlocal n
        n += 1
        indent, var = m.group(1), m.group(2)
        return f'{indent}{var} = parse_body(force=True)'

    out = _RE_PLAIN.sub(_sub_plain, src)
    out = _RE_FORCE.sub(_sub_force, out)
    if n:
        out = _ensure_imports(out)
    return out, n


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true')
    p.add_argument('--file', default='')
    args = p.parse_args()

    files = sorted(glob(os.path.join(ROUTES_DIR, '*.py')))
    if args.file:
        files = [f for f in files if os.path.basename(f) == args.file]

    total = 0
    changed = 0
    for path in files:
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        new_src, n = rewrite(src)
        if n == 0:
            continue
        changed += 1
        total += n
        print(f'  {os.path.basename(path):35s} {n} rewrites')
        if args.apply:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_src)

    mode = 'APPLIED' if args.apply else 'DRY-RUN'
    print(f'\n  ═══ {mode}: {changed} files, {total} rewrites ═══\n')


if __name__ == '__main__':
    main()
