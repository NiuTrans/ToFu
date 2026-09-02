"""Resolve production runtime sources for focused JavaScript harnesses.

The section manifest is the execution-order authority. Tests locate symbols
through it instead of pinning paths, so moving code between sections cannot
turn a product test into a source-layout failure.
"""

from __future__ import annotations

import os
import re

from tests._runtime_sections import (
    runtime_section_names,
    runtime_section_path,
    runtime_sections_dir,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def bundle_files():
    """Every shipped JS file, in the order the browser ends up executing it.

    The eager core manifest plus classic islands that are still reachable from
    the Vite feature graph. Native Vite replacements remain in the server's
    classic allow-list during migration, but are deliberately excluded here:
    those legacy sources no longer execute and must not satisfy a source scan.

    Core-only was a SCAN-SURFACE BUG, not a scoping choice: 21 deferred files
    (all of paper/*, project-brain*, orchestration*, image-gen*, task-mode)
    were invisible, so a lookup for a symbol living in one of them fell into
    the "not defined by any bundled file" branch and was reported as a PRODUCT
    REGRESSION. Measured 2026-07-28: `_activeReviewLang` (paper/report.js),
    `_loadPaperLibrary` (native Paper library owner) and `_refreshAttention`
    (project-brain.js) all produced "the implementation was REMOVED" while the
    files were on disk and shipping to users — a precisely-worded false
    attribution that would send the next reader off to restore code that never
    left.
    """
    return runtime_section_names()


def _unbundled_files_defining(symbol):
    """On-disk `static/js` files that define *symbol* but ship in NO bundle.

    Only consulted to explain a miss. A file here is a real (and different)
    product problem — the code exists but no user can reach it — so it must
    not be silently conflated with "the implementation was removed".
    """
    del symbol
    return []


def _defines(path, symbol):
    """True when *path* defines ``function <symbol>(`` or ``const/let/var
    <symbol> =`` at the top level (column 0) or as an indented module member."""
    try:
        src = open(path, encoding='utf-8').read()
    except OSError:
        return False
    pat = re.compile(
        r'^[ \t]*(?:async\s+)?function\s+' + re.escape(symbol) + r'\s*\('
        r'|^[ \t]*(?:const|let|var)\s+' + re.escape(symbol) + r'\s*=',
        re.M)
    return bool(pat.search(src))


def files_defining(symbol, *, subtree=''):
    """Bundle-relative paths (in execution order) that define *symbol*.

    *subtree* optionally narrows the search (e.g. ``'core/'``) when a
    same-named local in an unrelated module would otherwise hijack the lookup.
    It defaults to EVERYTHING shipped: a symbol's home is decided by the
    bundler's manifests, and pre-filtering by directory is how the deferred
    tree became invisible in the first place. Narrow only when a measured
    collision demands it.
    """
    return [name for name in bundle_files()
            if (not subtree or name.startswith(subtree))
            and _defines(runtime_section_path(name), symbol)]


def sources_defining(*symbols, subtree=''):
    """Absolute paths to eval, in EXECUTION ORDER, so *symbols* all resolve.

    Raises with a FOUR-STATE diagnosis (the distinction a hard-coded path
    cannot make). The states name DIFFERENT problems and must not share a
    message — conflating the first two sends the reader to restore code that
    never left:
      none, and nowhere on disk -> the implementation is GONE: a real product
          regression; restore it before touching the guard.
      none, but present on disk -> the file ships in NO bundle, so no user can
          reach that code. Also a product problem, but the fix is the MANIFEST
          (_BUNDLE_FILES / _CLASSIC_ASSET_FILES), not the implementation.
      many -> the single source of truth was copied: collapse it first.
      one  -> resolved; the caller evals the returned files.
    """
    out = []
    for sym in symbols:
        hits = files_defining(sym, subtree=subtree)
        if not hits:
            stray = _unbundled_files_defining(sym)
            if stray:
                raise AssertionError(
                    f'{sym} is defined by {stray} but that file is in NEITHER '
                    f'_BUNDLE_FILES nor _CLASSIC_ASSET_FILES — it is never served, so '
                    f'no user can reach this code. The implementation is INTACT; '
                    f'fix the bundler manifest, not the source.')
            where = f' under {subtree!r}' if subtree else ''
            raise AssertionError(
                f'{sym} is not defined by any shipped file{where}, and no file '
                f'under frontend/src/runtime defines it either — the implementation was '
                f'REMOVED. This is a product regression, not harness drift: '
                f'restore it before touching the guard.')
        if len(hits) > 1:
            raise AssertionError(
                f'{sym} is defined by {len(hits)} bundled files ({hits}) — the '
                f'single source of truth was duplicated; collapse it before '
                f're-pointing the guard.')
        out.append(hits[0])
    order = bundle_files()
    uniq = sorted(set(out), key=order.index)
    return [runtime_section_path(name) for name in uniq]


def conv_family_sources(*, override=None):
    """Return every shipped conversation-core section in execution order.

    ``override`` replaces a shipped source with a test mutation. Use this for
    behavior spanning several conversation sections; single-symbol tests
    should prefer :func:`sources_defining`.
    """
    family = [name for name in bundle_files()
              if name.startswith('core/conv_')
              or name.startswith('core/conversation_')]
    out = []
    for rel in family:
        if override and rel in override:
            out.append(override[rel])
        else:
            out.append(runtime_section_path(rel))
    return out


def source_argv(*symbols, override=None, subtree=''):
    """Ordered abs paths for ``node harness <paths...>``, with optional override.

    *override* maps a bundle-relative path to
    a substitute file — the NEUTER pattern several guards use: write a mutated
    copy to tmp_path and eval that instead of the shipped file, leaving the real
    tree untouched. Passing an override for a path this symbol set does not need
    raises, so a stale neuter target is reported instead of silently ignored
    (which would make the NEUTER "not bite" and read as a passing guard).
    """
    paths = sources_defining(*symbols, subtree=subtree)
    if not override:
        return paths
    out = []
    matched = set()
    for p in paths:
        rel = next((name for name in bundle_files()
                    if runtime_section_path(name) == p), '')
        if rel in override:
            out.append(override[rel])
            matched.add(rel)
        else:
            out.append(p)
    unused = set(override) - matched
    if unused:
        raise AssertionError(
            f'override targets {sorted(unused)} are not among the files needed '
            f'for {symbols} ({[os.path.relpath(p, JS_DIR) for p in paths]}) — '
            f'the neuter would silently NOT bite. Re-point it.')
    return out


def eval_prelude(*symbols, subtree=''):
    """A node snippet that eval's every file needed to define *symbols*.

    Use this instead of hard-coding a section path.
    """
    paths = sources_defining(*symbols, subtree=subtree)
    lines = ["const fs = require('fs');"]
    for p in paths:
        lines.append(f'eval(fs.readFileSync({p!r}, "utf8"));')
    return '\n'.join(lines)
