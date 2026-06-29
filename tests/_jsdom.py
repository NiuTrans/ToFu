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

import os
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


def run_harness(target_js, body_js, *, extra_targets=None, min_pass=1,
                timeout=60, label=''):
    """Run a jsdom harness body and assert no FAIL lines + enough PASS lines.

    Args:
        target_js: Absolute path to the primary JS file under test (argv[2]).
        body_js: The harness JS source (typically uses the shared ``setup()``).
        extra_targets: Optional additional JS paths (argv[4:]).
        min_pass: Minimum number of ``PASS`` lines required.
        timeout: node subprocess timeout (seconds).
        label: Optional prefix for failure messages.

    Skips (not fails) when node/jsdom are unavailable.
    """
    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed (run `npm install`)')

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
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, f'{pre}harness failures:\n{output}'
    npass = output.count('PASS')
    assert npass >= min_pass, (
        f'{pre}expected >= {min_pass} PASS lines, got {npass}:\n{output}'
    )
    return output
