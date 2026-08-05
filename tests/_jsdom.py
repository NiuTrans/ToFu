"""Shared Python runner for jsdom-based frontend test harnesses.

WHY
---
Every ``tests/test_frontend_*.py`` jsdom test used to repeat the same Python
plumbing: a ``_node_deps_available()`` dep guard, a ``_run()`` that writes the
harness JS to a temp file, shells out to ``node``, and parses stdout for
``PASS``/``FAIL`` lines. This module centralises that so a new frontend test is
just a harness JS string + one ``run_harness(...)`` call.

Pair it with ``tests/_jsdom_harness.js`` (the shared JS bootstrap). A per-test
harness body does::

    from tests._jsdom import run_harness, JS_DIR
    import os

    _BODY = r'''
    const { setup } = require(process.env.JSDOM_HARNESS);
    const { check, report } = setup({
      root: process.argv[3],
      html: '<!DOCTYPE html><body><div id="convList"></div></body>',
      targets: [process.argv[2]],
      globals: { activeStreams: new Map() },
    });
    // ... assertions ...
    report();
    '''

    def test_thing():
        run_harness(
            target_js=os.path.join(JS_DIR, 'ui', 'conversation_list.js'),
            body_js=_BODY,
            min_pass=4,
        )

``run_harness`` skips cleanly (via ``pytest.skip``) when node + jsdom aren't
installed, so non-frontend CI lanes and contributor machines without npm don't
hard-fail. ``argv`` contract: ``argv[2]`` = first target JS, ``argv[3]`` = repo
root, ``argv[4:]`` = any additional target JS paths. The ``JSDOM_HARNESS`` env
var points at the shared bootstrap module so the body can ``require`` it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
_SHARED_HARNESS = os.path.join(HERE, '_jsdom_harness.js')


def node_deps_available() -> bool:
    """True iff ``node`` is on PATH and ``node_modules/jsdom`` exists."""
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


RESULT_MARKER = '__JSDOM_RESULT__'

_FRONTEND_DEP_SKIP_RE = re.compile(r'node|jsdom|npm|tsc', re.IGNORECASE)


def frontend_required() -> bool:
    """True when the lane declares frontend suites MUST run.

    Lanes that promise to exercise the frontend (CI frontend job,
    ``make test-frontend``) set ``TOFU_REQUIRE_FRONTEND=1``: there a missing
    node/jsdom is a RED lane, never a silent skip. Unset elsewhere keeps the
    legacy clean-skip behaviour so contributors without npm don't hard-fail.
    See docs/TESTING_STRATEGY.md §4 (P0-1).
    """
    return os.environ.get('TOFU_REQUIRE_FRONTEND', '').strip().lower() in (
        '1', 'true', 'yes', 'on')


def skip_or_fail(reason):
    """pytest.skip normally; pytest.fail when TOFU_REQUIRE_FRONTEND=1."""
    if frontend_required():
        pytest.fail(
            f'TOFU_REQUIRE_FRONTEND=1 but frontend deps unavailable: {reason}')
    pytest.skip(reason)


def is_frontend_dep_skip(nodeid, reason) -> bool:
    """True iff a skipped ``test_frontend_*`` item was lost to a MISSING DEP
    (node/jsdom/npm/tsc) — the silent-loss class the conftest session sentinel
    counts. Data-conditional skips (e.g. 'no unsent run records') and skips in
    non-frontend files are NOT counted.
    """
    base = os.path.basename((nodeid or '').split('::', 1)[0])
    if not base.startswith('test_frontend_'):
        return False
    return bool(_FRONTEND_DEP_SKIP_RE.search(reason or ''))


def parse_harness_result(output):
    """Parse harness stdout → ``(pass_count, fail_count, structured)``.

    The shared harness's ``report()`` prints a structured trailer
    ``__JSDOM_RESULT__ {\"pass\": N, \"fail\": M}`` — authoritative when
    present. Legacy bodies without the trailer fall back to LINE-ANCHORED
    counting (``PASS ``/``FAIL`` at line start). A bare
    ``output.count('PASS')`` substring match counted 'BYPASS'/'PASSword' as
    passes, so it is deliberately gone.
    """
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith(RESULT_MARKER):
            try:
                data = json.loads(line[len(RESULT_MARKER):].strip())
                return int(data.get('pass', 0)), int(data.get('fail', 0)), True
            except (ValueError, TypeError, AttributeError):
                break
    npass = sum(1 for ln in output.splitlines() if ln.startswith('PASS '))
    nfail = sum(1 for ln in output.splitlines() if ln.startswith('FAIL'))
    return npass, nfail, False


def frontend_module_guard(*, need_jsdom=False, reason=''):
    """Module-level dep guard honouring TOFU_REQUIRE_FRONTEND.

    Drop-in for hand-written ``pytest.skip(..., allow_module_level=True)``
    guards: under TOFU_REQUIRE_FRONTEND=1 a missing dep fails collection (red)
    instead of silently dropping the whole module from the lane.
    """
    if need_jsdom:
        available = node_deps_available()
        default = 'node + jsdom dev-deps not installed (run `npm install`)'
    else:
        available = shutil.which('node') is not None
        default = 'node not on PATH'
    if available:
        return
    msg = reason or default
    if frontend_required():
        pytest.fail(
            f'TOFU_REQUIRE_FRONTEND=1 but frontend deps unavailable: {msg}',
            pytrace=False)
    pytest.skip(msg, allow_module_level=True)


def run_harness(target_js, body_js, *, extra_targets=None, min_pass=1,
                expect_pass=None, timeout=60, label=''):
    """Run a jsdom harness body and assert no failures + enough PASSes.

    Args:
        target_js: Absolute path to the primary JS file under test (argv[2]).
        body_js: The harness JS source (typically uses the shared ``setup()``).
        extra_targets: Optional additional JS paths (argv[4:]).
        min_pass: Minimum number of PASS assertions required (legacy floor).
        expect_pass: EXACT number of PASS assertions required. Preferred over
            ``min_pass`` for new suites (docs/TESTING_STRATEGY.md §6) — an
            accidentally dropped assertion can no longer pass silently.
        timeout: node subprocess timeout (seconds).
        label: Optional prefix for failure messages.

    Skips (not fails) when node/jsdom are unavailable — unless
    TOFU_REQUIRE_FRONTEND=1, in which case a missing dep is a hard failure.
    """
    if not node_deps_available():
        skip_or_fail('node + jsdom dev-deps not installed (run `npm install`)')

    extra_targets = extra_targets or []
    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=HERE, delete=False, encoding='utf-8'
    ) as fh:
        harness_path = fh.name
        fh.write(body_js)
    try:
        proc = subprocess.run(
            ['node', harness_path, target_js, ROOT, *extra_targets],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, 'JSDOM_HARNESS': _SHARED_HARNESS},
        )
    finally:
        try:
            os.remove(harness_path)
        except OSError:
            pass

    output = (proc.stdout or '').strip()
    pre = f'{label}: ' if label else ''
    assert proc.returncode == 0, f'{pre}node failed: {proc.stderr}\n{output}'
    npass, nfail, _structured = parse_harness_result(output)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails and nfail == 0, f'{pre}harness failures:\n{output}'
    if expect_pass is not None:
        assert npass == expect_pass, (
            f'{pre}expect_pass={expect_pass} but harness reported {npass} '
            f'PASS assertions:\n{output}'
        )
    else:
        assert npass >= min_pass, (
            f'{pre}expected >= {min_pass} PASS assertions, got {npass}:\n{output}'
        )
    return output
