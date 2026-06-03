#!/usr/bin/env python3
"""Migration script: rewrite ``requests.{get,post}(url, ..., proxies=_proxies_for(url), ...)``
to use ``lib.http_client`` helpers (``http_get``, ``http_post``).

Conservative — only rewrites when the call explicitly passes
``proxies=_proxies_for(url)`` or ``proxies=proxies_for(url)``, which is
exactly the pattern http_client.http_get/http_post replaces (auto-applied).

Patterns rewritten:
    requests.get(url, ..., proxies=_proxies_for(url), ...)   → http_get(url, ...)
    requests.post(url, ..., proxies=_proxies_for(url), ...)  → http_post(url, ...)

Patterns LEFT ALONE:
    requests.get(...) without proxies=                       — needs manual review
    requests.Session() / session.get(...)                    — keep their session
    custom retries / raise_for_status                        — keep as-is
    requests.post(stream=True, ...)                          — needs http_stream context manager

Usage:
    python tests/_migrate_http_client.py            # dry-run
    python tests/_migrate_http_client.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from glob import glob


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Match a full requests.METHOD(...) call, with the closing paren at the
# same indent as the opening line. Multi-line OK.
def _find_calls(src: str, method: str):
    """Yield (start, end, full_text) for each requests.<method>(...) call.

    Uses a simple paren counter — robust to multi-line, nested calls,
    string literals (basic single-line string handling).
    """
    pattern = re.compile(rf'\brequests\.{method}\(')
    for m in pattern.finditer(src):
        start = m.start()
        # Walk forward, balance parens
        i = m.end()
        depth = 1
        in_string = None  # ' or " or None
        while i < len(src):
            ch = src[i]
            if in_string:
                if ch == '\\' and i + 1 < len(src):
                    i += 2
                    continue
                if ch == in_string:
                    in_string = None
                i += 1
                continue
            if ch in ('"', "'"):
                in_string = ch
                i += 1
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    yield start, i + 1, src[start:i + 1]
                    break
            i += 1


# Match ``proxies=<call>(<url-arg>)`` argument, optionally surrounded by whitespace
# We look for the substring inside the call.
_PROXIES_RE = re.compile(
    r',\s*proxies\s*=\s*_?proxies_for\([^)]*\)\s*'
)


def _has_proxies_for_arg(call_text: str) -> bool:
    return bool(_PROXIES_RE.search(call_text))


def _strip_proxies_arg(call_text: str) -> str:
    """Remove the ``, proxies=_proxies_for(url)`` argument and return the call.

    Only strips a single occurrence; preserves trailing comma/whitespace
    cleanliness.
    """
    return _PROXIES_RE.sub('', call_text, count=1)


def _ensure_imports(src: str, helpers: set[str]) -> str:
    """Add http_client import line for the helpers used."""
    if not helpers:
        return src
    line_re = re.compile(
        r"^from lib\.http_client import (.+)$", re.MULTILINE)
    m = line_re.search(src)
    if m:
        existing = {x.strip() for x in m.group(1).split(',') if x.strip()}
        merged = sorted(existing | helpers)
        new_line = f"from lib.http_client import {', '.join(merged)}"
        return src.replace(m.group(0), new_line)

    # Insert after the FIRST `from lib.proxy import …` line if any.
    # We only match SINGLE-LINE imports (no trailing `(`). For multi-line
    # parenthesised imports (`from lib.proxy import (\n  foo,\n  bar,\n)`),
    # injecting in the middle would break the import — fall through.
    proxy_re = re.compile(
        r"^(from lib\.proxy import (?!\()[^\n]+)\n", re.MULTILINE)
    m = proxy_re.search(src)
    insertion = f"from lib.http_client import {', '.join(sorted(helpers))}\n"
    if m:
        return src[:m.end()] + insertion + src[m.end():]

    # Fallback: after the LAST single-line lib.* import
    lib_re = re.compile(
        r"^(from lib\.[^\s]+ import (?!\()[^\n]+)\n", re.MULTILINE)
    matches = list(lib_re.finditer(src))
    if matches:
        idx = matches[-1].end()
        return src[:idx] + insertion + src[idx:]

    # Last resort: top of file
    return insertion + src


def rewrite(src: str) -> tuple[str, dict[str, int]]:
    """Apply rewrites; return (new_src, {method: count})."""
    counts: dict[str, int] = {'get': 0, 'post': 0, 'put': 0, 'delete': 0, 'head': 0}
    helpers = set()

    for method in ('get', 'post', 'put', 'delete', 'head'):
        # Iterate over a snapshot so we don't double-rewrite
        # (rewriting changes offsets — process in reverse).
        calls = list(_find_calls(src, method))
        # Skip calls that don't contain `proxies=_proxies_for(...)` — those
        # are explicit and we don't touch them.
        calls = [c for c in calls if _has_proxies_for_arg(c[2])]
        # Skip stream=True calls — they need http_stream context manager
        # (semantics differ from http_get/http_post)
        calls = [c for c in calls if 'stream=True' not in c[2]]
        # Skip calls inside triple-quoted strings (docstrings, examples)
        def _in_docstring(start_pos: int) -> bool:
            # Count both """ and ''' before this position; odd → inside doc
            n_dq = src[:start_pos].count('"""')
            n_sq = src[:start_pos].count("'''")
            return (n_dq % 2 == 1) or (n_sq % 2 == 1)
        calls = [c for c in calls if not _in_docstring(c[0])]
        if not calls:
            continue
        # Process in reverse so earlier offsets stay stable
        for start, end, text in reversed(calls):
            new_text = _strip_proxies_arg(text)
            new_text = new_text.replace(f'requests.{method}(',
                                          f'http_{method}(', 1)
            src = src[:start] + new_text + src[end:]
            counts[method] += 1
            helpers.add(f'http_{method}')

    if any(counts.values()):
        src = _ensure_imports(src, helpers)
    return src, {k: v for k, v in counts.items() if v}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true')
    p.add_argument('--scope', default='lib,routes',
                    help='Comma-separated dirs to walk (default: lib,routes)')
    args = p.parse_args()

    from glob import glob as _glob
    files: list[str] = []
    for scope in args.scope.split(','):
        scope = scope.strip()
        d = os.path.join(PROJECT_ROOT, scope)
        if os.path.isdir(d):
            files.extend(_glob(os.path.join(d, '**', '*.py'), recursive=True))

    files = sorted(files)
    total = {'get': 0, 'post': 0, 'put': 0, 'delete': 0, 'head': 0}
    changed = 0

    for path in files:
        # Skip http_client itself (it imports requests legitimately)
        if path.endswith('lib/http_client.py'):
            continue
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        new_src, counts = rewrite(src)
        if not counts:
            continue
        changed += 1
        rel = os.path.relpath(path, PROJECT_ROOT)
        print(f'  {rel:40s} ' + ', '.join(f'{k}={v}' for k, v in counts.items()))
        for k, v in counts.items():
            total[k] += v
        if args.apply:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_src)

    mode = 'APPLIED' if args.apply else 'DRY-RUN'
    grand = sum(total.values())
    print(f'\n  ═══ {mode}: {changed} files, {grand} rewrites ═══\n')
    for k, v in total.items():
        if v:
            print(f'    {k:8s} {v}')


if __name__ == '__main__':
    main()
