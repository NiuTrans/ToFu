#!/usr/bin/env python3
"""Regression guard for pt_3879f00e sub-part 3: every consumer of
``core/health_stream_timer.js``'s ``twStart`` / ``twUpdate`` / ``twStop``
functions MUST typeof-gate the call, so the module can be moved into
``_CLASSIC_ASSET_FILES`` without tripping ``ReferenceError`` on the first SSE
frame after boot.

The audit in ``docs/EPIC_E_DEFER_AUDIT.md`` enumerated ~40 unguarded call
sites across 8 files as the sole deferrability blocker. This test locks
the invariant: any new ``twUpdate(convId)`` bare-statement call added
after this test lands will trip it, forcing the author to add the
``if (typeof twUpdate === 'function')`` gate (the pattern used everywhere
else already).

The pattern this test forbids is a bare STATEMENT of the form
``<indent>tw{Start,Update,Stop}(<args>);\n`` at line start. It does NOT
forbid the guarded form ``if (typeof twX === 'function') twX(...)`` — that's
the whole point. It also does NOT touch inline usages inside an expression
(e.g. ``foo(twStop)`` is not a statement); those are inert without a call.

Files scanned: every ``static/js/**/*.js`` file that is currently in the
core bundle (``_BUNDLE_FILES``) OR streaming-adjacent (ui/ subdir). We
scan them all rather than a fixed allowlist so a future file added to the
core bundle can't reintroduce the pattern silently.
"""

from __future__ import annotations

import os
import re
import sys

from tests._runtime_sections import runtime_section_path, runtime_sections_dir

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_JS = runtime_sections_dir()


# The forbidden pattern — a BARE statement invocation of a tw* function at
# line start. Explicitly ANCHORED on the start of a line so an inline
# occurrence inside a template literal or a longer expression doesn't
# false-positive. The regex matches only when the entire NON-COMMENT
# content of the line is the call.
_BARE_TW_CALL_RE = re.compile(
    r'^\s*(twStart|twUpdate|twStop)\s*\([^)]*\)\s*;?\s*$'
)


# Built bundle outputs (lib/js_bundler._BUILT_BUNDLE_RE): bundle-<8hex>.js,
# feature-<8hex>.js, i18n-(zh|en)-<8hex>.js. These are runtime-assembled
# CONCATENATIONS of the very sources this test scans — including
# health_stream_timer.js itself, whose three INTERNAL twStop() calls are
# (correctly) name-skipped at the source path. Whether a built artifact
# trips the line-anchored regex depends on the minifier: with esbuild the
# bundle is one line (no match); without node_modules (public CI test-unit
# lane) the pure-python _minify_js keeps newlines and the internal calls
# match — a false positive for a contract about SOURCE call sites.
_BUILT_ARTIFACT_RE = re.compile(
    r'^(?:(?:bundle|feature)-[0-9a-f]{8}|i18n-(?:zh|en)-[0-9a-f]{8})\.js$'
)


def _iter_js_files():
    for dirpath, _dirnames, filenames in os.walk(_STATIC_JS):
        for name in filenames:
            if not name.endswith('.js'):
                continue
            # Skip BUILT artifacts — they duplicate the sources (modulo
            # minification) and are gitignored/rebuilt at startup; only
            # source call sites are this test's contract.
            if _BUILT_ARTIFACT_RE.match(name):
                continue
            # Skip the health_stream_timer.js module ITSELF — it defines
            # the functions and calls them internally (obviously guarded
            # by being the definer).
            if name == 'health_stream_timer.js':
                continue
            # Skip agent/backup artefacts. The ``.nc_copy`` suffix marks
            # never-committed staging files (see lib/agent_artifacts.py:
            # anything with the ``.nc_copy`` marker is transient work-tree
            # scaffolding, not shipped source). These do NOT enter the
            # bundle, so a bare tw*() call in one of them cannot ReferenceError
            # at runtime; enforcing the guard there would just gate a stale
            # backup on the LIVE deferability contract.
            if '.nc_copy' in name:
                continue
            full = os.path.join(dirpath, name)
            yield full


@_unit
def test_no_bare_tw_call_sites_outside_health_stream_timer():
    """Every tw*() call outside health_stream_timer.js MUST be
    typeof-guarded. A regression would either be a NEW bare statement
    like ``twUpdate(convId);`` or a mis-edit that removed the ``if
    (typeof … === 'function')`` prefix from an existing site.

    This test scans every ``static/js/**/*.js`` file and reports each
    offender with file:line:content so a future contributor can fix
    them in one shot.
    """
    violations: list[str] = []
    for path in _iter_js_files():
        try:
            with open(path, encoding='utf-8') as f:
                for idx, line in enumerate(f, start=1):
                    if _BARE_TW_CALL_RE.match(line):
                        rel = os.path.relpath(path, _ROOT)
                        violations.append(f'{rel}:{idx}:{line.rstrip()}')
        except (OSError, UnicodeDecodeError):
            # Binary artefact snuck into static/js — skip
            continue
    assert not violations, (
        'Bare tw*() call sites found — these would ReferenceError if '
        'core/health_stream_timer.js were moved to _CLASSIC_ASSET_FILES '
        '(pt_3879f00e sub-part 3). Wrap each in '
        "``if (typeof twX === 'function') twX(...)`` so the deferrability "
        'sweep stays intact:\n\n' + '\n'.join('  ' + v for v in violations)
    )


@_unit
def test_typeof_guards_are_actually_present():
    """Positive sanity check: at least ONE guarded form MUST exist per
    scanned SSE-handler file (paired with the deletion above — a naive
    "delete all tw* calls" edit would flip both tests green vacuously).

    The list of files below is the concrete set the audit enumerated as
    the sweep target. Each MUST carry at least one
    ``typeof tw{Start,Update,Stop} === 'function'`` guard.
    """
    required_files = [
        'project.js',
        'ui/sse_handlers_lifecycle.js',
        'ui/sse_handlers_io.js',
        'ui/sse_handlers_swarm.js',
        'ui/sse_handlers_tool.js',
        'ui/sse_handlers_misc.js',
        'ui/sse_pipeline.js',
        # Files the audit missed but the guard test discovered on first
        # run — kept in the required-set so a future edit can't silently
        # remove the guard from these two either.
        'ui/stream_lifecycle.js',
        'ui/tool_rounds.js',
    ]
    guard_re = re.compile(
        r"typeof\s+tw(Start|Update|Stop)\s*===\s*'function'"
    )
    missing = []
    for rel in required_files:
        full = runtime_section_path(rel)
        with open(full, encoding='utf-8') as f:
            src = f.read()
        if not guard_re.search(src):
            missing.append(rel)
    assert not missing, (
        'These SSE-handler files are the ones the sweep landed in — '
        'each MUST retain at least one typeof-guarded tw*() call. If '
        'the guards vanish here without the file being deleted, the '
        'deferrability sweep was regressed:\n  ' + '\n  '.join(missing)
    )


if __name__ == '__main__':
    tests = [
        test_no_bare_tw_call_sites_outside_health_stream_timer,
        test_typeof_guards_are_actually_present,
    ]
    for fn in tests:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
