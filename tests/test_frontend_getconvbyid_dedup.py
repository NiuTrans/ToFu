"""jsdom/node regression for the conversation-lookup dedup (getConvById).

WHY
---
~20 source sites open-coded `conversations.find((c) => c.id === X)` (with
subtly divergent guards — some tolerate an undefined `conversations`, some
don't; some `|| null`, some return `undefined`). This dedup adds ONE canonical
helper `getConvById(id)` in core.js (and routes `getActiveConv()` through it),
then points the peripheral, non-conversation-switch call sites at it.

This is verified, not self-reported:
  1. RUNTIME — brace-extract the REAL shipped `getConvById` from core.js and
     run it under node: it finds a seeded conv by id, returns null for an
     unknown id / falsy id / when `conversations` is undefined, and never
     throws. A NEUTER (a broken lookup that returns the first conv regardless
     of id) makes the by-id assertion FAIL — proving the assertion bites.
  2. SOURCE — the delegating callers (context-bar `_activeConv`, folders
     `setConversationFolder`, timer `_jumpToTimerConv`)
     route through `getConvById`/`getActiveConv` instead of re-implementing the
     `.find`. Source-level so it's robust without spinning a full jsdom per
     module.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section, runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CORE_SRC = runtime_section_path('core.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_fn(src: str, name: str) -> str:
    """Brace-match-extract a top-level `function name(...) { ... }` block."""
    start = src.index('function ' + name + '(')
    depth = 0
    i = src.index('{', start)
    while i < len(src):
        c = src[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError('unbalanced braces extracting ' + name)


def _build_harness(neuter: bool) -> str:
    with open(_CORE_SRC, encoding='utf-8') as f:
        src = f.read()
    fn = _extract_fn(src, 'getConvById')
    assert 'conversations.find' in fn, 'getConvById must do the by-id find'

    if neuter:
        # Botched dedup: ignore the id and return the first conversation
        # regardless — the `by_id` + `unknown_id_null` checks must then flip.
        fn = fn.replace(
            'return conversations.find((c) => c && c.id === id) || null;',
            'return conversations[0] || null;', 1)
        assert 'conversations[0]' in fn, 'neuter did not apply'

    return (
        # `conversations` starts DEFINED (the real init state) …
        "var conversations = [\n"
        "  { id: 'a', title: 'A' }, { id: 'b', title: 'B' }, { id: 'c', title: 'C' },\n"
        "];\n"
        + fn + "\n"
        "var out = [];\n"
        "function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }\n"
        # by-id lookup returns the RIGHT object
        "check('by_id', (getConvById('b') || {}).id === 'b');\n"
        "check('by_id_first', (getConvById('a') || {}).id === 'a');\n"
        # unknown id → null (NOT the first conv — this is what the neuter breaks)
        "check('unknown_id_null', getConvById('zzz') === null);\n"
        # falsy id → null, no throw
        "check('falsy_null', getConvById('') === null && getConvById(null) === null);\n"
        # undefined `conversations` global tolerated → null, no ReferenceError
        "conversations = undefined;\n"
        "var threw = false; var r;\n"
        "try { r = getConvById('a'); } catch (e) { threw = true; }\n"
        "check('undefined_tolerated', !threw && r === null);\n"
        "console.log(out.join('\\n'));\n"
    )


def _run(harness: str) -> str:
    path = os.path.join(HERE, '_getconvbyid_harness.js')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(harness)
    try:
        proc = subprocess.run(['node', path], capture_output=True, text=True, timeout=30)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


_EXPECTED = ('by_id', 'by_id_first', 'unknown_id_null', 'falsy_null', 'undefined_tolerated')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_getconvbyid_semantics():
    output = _run(_build_harness(neuter=False))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'getConvById failures:\n' + output
    for must in _EXPECTED:
        assert ('PASS ' + must) in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_getconvbyid_lookup_is_load_bearing():
    """Neuter the lookup to ignore the id (return the first conv) → by-id +
    unknown-id checks FAIL, while the tolerant-guard checks still PASS."""
    output = _run(_build_harness(neuter=True))
    for m in ('by_id', 'unknown_id_null'):
        assert ('FAIL ' + m) in output, f'NC: expected {m} to FAIL:\n{output}'
    for m in ('falsy_null', 'undefined_tolerated'):
        assert ('PASS ' + m) in output, \
            f'NC must be surgical — {m} should still PASS:\n{output}'


def test_delegating_callers_use_canonical_helper():
    """The peripheral call sites this pass migrated must route through the
    canonical helper instead of re-implementing `conversations.find`.
    Source-level (no node) — robust and fast."""
    def _read(*parts):
        return runtime_section('/'.join(parts))

    core = _read('core.js')
    # The canonical helper exists and getActiveConv delegates to it.
    assert 'function getConvById(id) {' in core, 'core.js must define getConvById'
    assert 'function getActiveConv() {\n  return getConvById(activeConvId);\n}' in core, (
        'getActiveConv must delegate to getConvById')

    ctxbar = _read('context-bar.js')
    assert 'return getConvById(' in ctxbar, '_activeConv must delegate to getConvById'
    assert 'conversations.find' not in ctxbar, (
        'context-bar.js must not re-implement the conversation .find')

    folders = _read('core', 'folders.js')
    assert 'const c = getConvById(convId);' in folders, (
        'setConversationFolder must delegate to getConvById')

    timer = _read('timer.js')
    assert 'const conv = getConvById(convId);' in timer, (
        '_jumpToTimerConv must delegate to getConvById')
    assert 'conversations.find' not in timer, (
        'timer.js must not re-implement the conversation .find')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
