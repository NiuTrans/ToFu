"""tests/test_orchestration_nested_canvas.py — Group nested-canvas round-trip.

The Orchestration Studio's GROUP (subflow) feature lets a user double-click a
node to descend into its child flow, edit it, and surface back. That logic
lives entirely in module-global state in ``static/js/orchestration.js``
(``_orchNodes`` / ``_orchEdges`` / ``_orchStack`` + ``_orchEnterGroup`` /
``_orchExitGroup`` / ``_orchToDefinition``) with NO backend seam — so the
Python suites and the tsc ratchet are both blind to a regression in it.

This test drives that state machine headlessly in jsdom (see
``tests/orch_nested_roundtrip_harness.js``) and then re-validates the
round-tripped root definition against the REAL backend schema
(``lib.orchestration.validate_definition`` + ``expand_subflows``), closing the
loop from frontend state logic to the backend contract a freshly-authored
group must satisfy.

Skips gracefully when Node / jsdom aren't installed (mirrors the tsc ratchet),
so non-frontend CI lanes don't hard-fail.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from lib.orchestration import (
    expand_subflows, render_role_brief, resolve_scope, validate_definition,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
HARNESS = os.path.join(HERE, 'orch_nested_roundtrip_harness.js')


ORCH_JS = os.path.join(ROOT, 'static', 'js', 'orchestration.js')


def _orch_base_css_rule_heads() -> list[str]:
    """Return the selector heads of every top-level rule in orchestration.js's
    injected base CSS (the ``var css = `...`;`` literal, before the first
    ``@media`` block). Walks brace depth so descendant selectors and the bodies
    are never mistaken for rule heads. Pure string parsing — no Node needed."""
    import re

    src = open(ORCH_JS, encoding='utf-8').read()
    m = re.search(r'var css = `(.*?)`;', src, re.S)
    assert m, 'could not locate the injected CSS literal in orchestration.js'
    base = m.group(1).split('@media', 1)[0]   # responsive overrides are exempt

    heads: list[str] = []
    depth = 0
    cur = ''
    for ch in base:
        if ch == '{':
            if depth == 0:
                heads.append(cur.strip())
                cur = ''
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                cur = ''
        elif depth == 0:
            cur += ch
    return heads


def test_no_duplicate_base_css_selectors():
    """CI guard against the `.orch-hint` class-collision bug class.

    orchestration.js injects ALL its styles as one scoped literal. A second
    rule keyed on the *exact same single class* silently shadows the first —
    that is how a section-hint `.orch-hint` inherited the canvas overlay's
    `position:absolute;inset:0` and smeared text across the canvas.

    We flag only SIMPLE single-class heads (``.orch-foo``) defined more than
    once. The deliberate base+override pattern (a shared
    ``.orch-node-start,.orch-node-stop{...}`` rule plus per-class background
    overrides) is NOT flagged: a comma group / compound / pseudo head is not a
    bare single-class head, so it never collides with the override rule.
    Responsive ``@media`` overrides are exempt by construction.
    """
    import re
    from collections import Counter

    heads = _orch_base_css_rule_heads()
    simple = [h for h in heads if re.fullmatch(r'\.orch-[a-z0-9-]+', h)]
    dups = {sel: n for sel, n in Counter(simple).items() if n > 1}
    assert not dups, (
        'Duplicate single-class CSS rule(s) in orchestration.js base styles — '
        'the second definition silently shadows the first (see the .orch-hint '
        f'collision). Rename one or merge them: {dups}'
    )


def _jsdom_available() -> bool:
    if shutil.which('node') is None:
        return False
    probe = subprocess.run(
        ['node', '-e', "require.resolve('jsdom')"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return probe.returncode == 0


def _extract(out: str, prefix: str) -> dict:
    line = [ln for ln in out.splitlines() if ln.startswith(prefix)]
    assert line, f'harness emitted no {prefix}\n{out}'
    return json.loads(line[0][len(prefix):])


def _run_harness() -> tuple[str, dict, dict, dict]:
    """Run the jsdom harness; return (stdout, nested, structured, io) defs."""
    proc = subprocess.run(
        ['node', HARNESS], cwd=ROOT,
        capture_output=True, text=True, timeout=120,
    )
    out = (proc.stdout or '') + '\n' + (proc.stderr or '')
    assert proc.returncode == 0, f'harness exited {proc.returncode}:\n{out}'
    assert 'ALL_OK' in out, f'harness did not reach ALL_OK:\n{out}'
    nested = _extract(out, 'RESULT_JSON=')
    structured = _extract(out, 'RESULT_JSON2=')
    io = _extract(out, 'RESULT_JSON3=')
    return out, nested, structured, io


@pytest.fixture(scope='module')
def harness_result():
    if not _jsdom_available():
        pytest.skip('node/jsdom not available (run `npm install` at repo root)')
    return _run_harness()


def test_harness_assertions_pass(harness_result):
    """All in-JS state assertions (enter/exit/commit, depth-2, flush,
    structured fields) held."""
    out, _, _, _ = harness_result
    # The harness counts its own assertions; make sure it ran a meaningful set
    # (guards against a silently-empty run that still prints ALL_OK).
    count_line = [ln for ln in out.splitlines() if ln.startswith('CHECKS=')]
    assert count_line, out
    assert int(count_line[0][len('CHECKS='):]) >= 30, out


def test_roundtripped_definition_is_backend_valid(harness_result):
    """The root definition the studio serializes after a nested edit must pass
    the SAME validator the REST store + engine enforce."""
    _, defn, _, _ = harness_result
    verdict = validate_definition(defn)
    assert verdict['ok'], verdict['errors']


def test_group_survives_as_isolated_black_box(harness_result):
    """The serialized group is an isolated subflow and stays intact (not
    flattened) through expand_subflows — i.e. it is a real black box."""
    _, defn, _, _ = harness_result
    group = [n for n in defn['nodes'] if n.get('type') == 'subflow'][0]
    assert resolve_scope(group) == 'isolated'
    flat = expand_subflows(defn)
    assert any(n.get('id') == group['id'] for n in flat['nodes']), \
        'isolated group must NOT be flattened by expand_subflows'


def test_nested_child_definition_also_validates(harness_result):
    """The embedded child (and its depth-2 nested child) authored through the
    nested canvas are themselves valid definitions — the commit produced a
    well-formed sub-graph at every level."""
    _, defn, _, _ = harness_result
    group = [n for n in defn['nodes'] if n.get('type') == 'subflow'][0]
    child = group['params']['definition']
    assert validate_definition(child)['ok'], 'depth-1 child invalid'
    # The harness plants a coder + a nested group at depth 1, and a writer at
    # depth 2 — confirm the committed edits are actually present.
    assert any(n.get('role') == 'coder' for n in child['nodes']), \
        'depth-1 edit (coder) missing from committed child'
    inner = [n for n in child['nodes'] if n.get('type') == 'subflow'][0]
    inner_child = inner['params']['definition']
    assert validate_definition(inner_child)['ok'], 'depth-2 child invalid'
    assert any(n.get('role') == 'writer' for n in inner_child['nodes']), \
        'depth-2 edit (writer) missing from committed nested child'


def _role(defn, role):
    return [n for n in defn['nodes'] if n.get('role') == role][0]


def test_structured_fields_serialize_with_backend_shape(harness_result):
    """The inspector's structured fields (list/select/bool), driven through
    _orchSetParam, must serialize into params with the SHAPE the backend
    validator expects — list→array, select→enum-or-absent, bool→true/false."""
    _, _, sdef, _ = harness_result
    verdict = validate_definition(sdef)
    assert verdict['ok'], verdict['errors']

    worker = _role(sdef, 'worker')
    assert isinstance(worker['params']['must_do'], list), 'must_do not a list'
    assert worker['params']['must_do'] == ['ship it', 'write tests'], \
        worker['params']['must_do']
    # Emptied list field was omitted entirely, not stored as [''] or ''.
    assert 'must_not_do' not in worker['params'], \
        'emptied list field should be absent'

    critic = _role(sdef, 'critic')
    assert isinstance(critic['params']['must_check'], list)
    # Unset select → key ABSENT (never the empty string, which would be an
    # invalid enum value and fail validation).
    assert 'verdict_format' not in critic['params'], \
        'unset select must be absent, not an empty string'
    assert critic['params'].get('adversarial') is False, \
        'unchecked bool kept as false'


def test_structured_fields_render_into_brief(harness_result):
    """The structured params the inspector produced must compose into the
    delegation brief the engine sends — closing inspector→render_role_brief."""
    _, _, sdef, _ = harness_result
    brief = render_role_brief(_role(sdef, 'worker'))
    assert brief.startswith('Build the widget.'), brief
    assert '### Must Do' in brief
    assert '- ship it' in brief
    assert '- write tests' in brief
    # must_not_do was cleared → its section must NOT appear.
    assert 'Must Not Do' not in brief, brief


def test_typed_io_contract_serializes_and_validates(harness_result):
    """The edge-selection + typed I/O editor flow (Scenario 5) produces a
    backend-valid definition whose worker exposes summary/changes outputs and
    whose writer wires a typed input to worker.changes — closing the
    frontend I/O-editor → backend io-validator loop."""
    _, _, _, iodef = harness_result
    verdict = validate_definition(iodef)
    assert verdict['ok'], verdict['errors']

    worker = [n for n in iodef['nodes'] if n.get('role') == 'worker'][0]
    outs = worker['params']['io']['outputs']
    names = [o['name'] for o in outs]
    assert names == ['summary', 'changes'], names
    assert outs[1]['type'] == 'artifact'

    writer = [n for n in iodef['nodes'] if n.get('role') == 'writer'][0]
    inp = writer['params']['io']['inputs'][0]
    assert inp['from'] == worker['id'] + '.changes', inp
    assert inp['type'] == 'artifact'
