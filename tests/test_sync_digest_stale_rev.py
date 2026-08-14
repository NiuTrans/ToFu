#!/usr/bin/env python3
"""Guard: buildSyncDigest must not report a rev whose staleness is BY DESIGN.

THE DEFECT (measured in production, 2026-07-26)
-----------------------------------------------
Eight consecutive 60s drift reports, two different conversations::

    ms0zuc59: c=523 s=645 → 523/646 → 523/647 → … → 523/652
    ms14r8qs: c=62  s=155 → 62/155  → 62/157  → … → 62/163

The client rev never moved once while the server's climbed. That is
bit-for-bit the shape of a DROPPED FRAME — and it was not one.

``conv._serverRev`` records "the rev at which this tab last fetched the
BODY", not "the newest rev this tab is aware of". On a notify for a conv
the user is not viewing, ``cross_tab_sync.js`` deliberately does NOT
refetch: it sets ``_needsLoad = true``, refreshes the sidebar, and its
comment states "Never repaints the viewport". The body — and therefore
``_serverRev`` — is meant to stay behind until the conv is next opened.

So the rev dimension of the P5 probe could not distinguish "background
conv, working as designed" from "notify frame dropped, never converges"
(owner hard constraint #4's actual target). Every background conv was a
false positive, and P6 would have used that list as its evidence for
deleting fallback branches.

THE FIX: a conv that knows its body is stale reports ``rev: null`` —
"do not compare me". The server already skips non-numeric revs. ``taskIds``
is untouched: it is written ONLY by server frames, so it remains a valid
convergence signal for background convs and is the dimension P6 evidence
should rest on.

These probes drive the REAL shipped ``buildSyncDigest`` under jsdom — not
a reimplementation — so a regression in the shipped file fails here.

Run: python3 tests/test_sync_digest_stale_rev.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._runtime_sections import runtime_section_path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REDUCER = runtime_section_path('core/conv_state_reducer.js')

try:
    import pytest
except ImportError:
    pytest = None


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _have_node():
    return shutil.which('node') is not None


def _run_digest(convs, *, neuter=False):
    """Load the REAL reducer in jsdom-ish scope and call buildSyncDigest.

    ``neuter=True`` reverts the shipped guard (reports _serverRev
    unconditionally) to prove the guard is load-bearing.
    """
    with open(REDUCER, encoding='utf-8') as f:
        src = f.read()

    if neuter:
        # Restore the pre-fix expression exactly.
        needle = ('const bodyIsStale = conv._needsLoad === true;\n'
                  '      const rev = (!bodyIsStale && typeof conv._serverRev === \'number\')\n'
                  '        ? conv._serverRev : null;')
        assert needle in src, (
            'NEUTER anchor missing — the shipped guard was reworded; update '
            'this test so it keeps proving the guard is load-bearing')
        src = src.replace(
            needle,
            "const rev = (typeof conv._serverRev === 'number') ? conv._serverRev : null;")

    harness = f"""
globalThis.window = globalThis;
{src}
const convs = {json.dumps(convs)};
// _authoritativeActiveTaskIds is a Set in production; rehydrate from the
// plain array the JSON carries.
for (const c of convs) {{
  if (Array.isArray(c._authTaskIds)) {{
    c._authoritativeActiveTaskIds = new Set(c._authTaskIds);
  }}
}}
console.log(JSON.stringify(buildSyncDigest(convs)));
"""
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(harness)
        path = fh.name
    try:
        out = subprocess.run([shutil.which('node'), path],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f'node failed: {out.stderr[:600]}')
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


# ── Face 1: the production shape must stop being reported ────────────────

@_unit
def test_background_conv_with_stale_body_reports_null_rev():
    """The exact ms0zuc59 shape: frozen _serverRev + _needsLoad set."""
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    digest = _run_digest([
        {'id': 'ms0zuc59', '_serverRev': 523, '_needsLoad': True,
         '_authoritativeActiveTaskIdsRev': [1, 'r'], '_authTaskIds': []},
    ])
    assert len(digest) == 1, 'the conv must still be reported (taskIds matter)'
    assert digest[0]['rev'] is None, (
        'a conv that knows its body is stale must NOT report a rev — '
        'comparing it produced 8 straight false "dropped frame" alerts')
    assert digest[0]['taskIds'] == [], (
        'taskIds must survive: it is server-frame-written and is the '
        'dimension P6 evidence rests on')


@_unit
def test_fresh_conv_still_reports_its_rev():
    """The guard must not blind the probe on convs that ARE current."""
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    digest = _run_digest([
        {'id': 'cv-fresh', '_serverRev': 240, '_needsLoad': False,
         '_authoritativeActiveTaskIdsRev': [1, 'r'], '_authTaskIds': ['t1']},
    ])
    assert len(digest) == 1
    assert digest[0]['rev'] == 240, (
        'a conv whose body is current MUST still report its rev — otherwise '
        'the fix would disable the very hole constraint #4 targets')
    assert digest[0]['taskIds'] == ['t1']


@_unit
def test_conv_with_only_stale_rev_and_no_auth_marker_is_dropped_entirely():
    """rev=null + no authoritative marker → nothing to compare → omit.

    Pins the pre-existing ``if (!hasAuth && rev === null) continue`` skip
    against the new null path, so a stale-bodied conv with no server-frame
    history does not become an empty entry the server must handle.
    """
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    digest = _run_digest([
        {'id': 'cv-nothing', '_serverRev': 99, '_needsLoad': True},
    ])
    assert digest == [], (
        'no authoritative marker AND no comparable rev = contributes nothing')


@_unit
def test_mixed_fleet_reports_only_the_comparable_revs():
    """The realistic case: one open conv + several background ones."""
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    digest = _run_digest([
        {'id': 'cv-open', '_serverRev': 300, '_needsLoad': False,
         '_authoritativeActiveTaskIdsRev': [9, 'r'], '_authTaskIds': ['t9']},
        {'id': 'cv-bg1', '_serverRev': 523, '_needsLoad': True,
         '_authoritativeActiveTaskIdsRev': [1, 'r'], '_authTaskIds': []},
        {'id': 'cv-bg2', '_serverRev': 62, '_needsLoad': True,
         '_authoritativeActiveTaskIdsRev': [2, 'r'], '_authTaskIds': ['t2']},
    ])
    by_id = {d['convId']: d for d in digest}
    assert by_id['cv-open']['rev'] == 300
    assert by_id['cv-bg1']['rev'] is None
    assert by_id['cv-bg2']['rev'] is None
    assert by_id['cv-bg2']['taskIds'] == ['t2'], (
        'background convs keep contributing taskIds — that is what proves '
        'server frames are landing')


# ── NEUTER: prove the guard is load-bearing ──────────────────────────────

@_unit
def test_NEUTER_removing_the_stale_guard_reproduces_the_false_positive():
    if not _have_node():
        print('SKIP (node unavailable)')
        return
    convs = [{'id': 'ms0zuc59', '_serverRev': 523, '_needsLoad': True,
              '_authoritativeActiveTaskIdsRev': [1, 'r'], '_authTaskIds': []}]

    shipped = _run_digest(convs)
    neutered = _run_digest(convs, neuter=True)

    assert neutered[0]['rev'] == 523, (
        'without the guard the frozen background rev IS reported — this is '
        'the production false positive being reproduced')
    assert shipped[0]['rev'] is None, (
        'the shipped code must suppress it; if these two ever agree, the '
        'guard has been removed and the false positives are back')


if __name__ == '__main__':
    if not _have_node():
        print('SKIP: node not available')
        sys.exit(0)
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                failures += 1
                print('FAIL', name)
                print('     ', e)
    print('ALL PASSED' if not failures else f'{failures} FAILED')
    sys.exit(1 if failures else 0)
