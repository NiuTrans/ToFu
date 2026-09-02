"""tests/test_action_registry_nested_calls.py — interpreter grammar contract.

The action interpreter (frontend/src/action-registry.ts) replaced inline
``onclick`` eval with an allowlist grammar. Its ``resolveValue`` originally
recognised only literals / ``event`` / ``this`` / property paths / registered
bare names — so a call-shaped argument such as ``_elementOrdinal(this)`` fell
through as raw source text instead of invoking the registered selector.

Pinned behaviours (real jsdom click through the REAL registry source):

  1. Nested call args resolve to their return value: ``captureValue(_elementOrdinal(this))``
     receives ``7`` (number), never the raw source string.
  2. Multi-arg mixed nesting works: ``_takeTwo(_elementOrdinal(this), 3)`` → ``[7, 3]``.
  3. Assignment RHS nesting works: ``this.dataset.idx=_elementOrdinal(this)`` → ``"7"``.
  4. ``event.target.closest('.x')`` inside an ``if`` condition now CALLS the
     method (bilingual-header guard form) instead of resolving to undefined.
  5. Unknown nested names refuse LOUDLY: console.error ``[actions] refused``,
     target handler never invoked, click does not throw.
  6. Controls: plain literal args and quoted strings unchanged.

Skips when node+jsdom dev-deps are absent (convention of
test_frontend_cmd_collapse.py).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
REGISTRY = os.path.join(ROOT, 'frontend', 'src', 'action-registry.ts')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# Runs under plain node (≥23 for type stripping): imports a .mts snapshot of
# the real registry so no bundler is needed, drives REAL jsdom click events.
_HARNESS = r"""
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

const ROOT = process.argv[2];
const { JSDOM } = require(require('path').join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!DOCTYPE html><html><body>
  <div id="m1" class="item"><button id="b1" data-tofu-action="event.stopPropagation();captureValue(_elementOrdinal(this))">Capture</button></div>
  <div id="m2" class="item"><button id="b2" data-tofu-action="_takeTwo(_elementOrdinal(this), 3)">Two</button></div>
  <div id="m3" class="item"><button id="b3" data-tofu-action="this.dataset.idx=_elementOrdinal(this)">Assign</button></div>
  <div id="h1" data-tofu-action="if(event.target.closest('.keep'))return;this.dataset.hit='1'">
    <span id="k1" class="keep">keep</span><span id="p1" class="plain">plain</span>
  </div>
  <div id="m4" class="item"><button id="b4" data-tofu-action="captureValue(_nope(this))">Unknown</button></div>
  <div id="m5" class="item"><button id="b5" data-tofu-action="captureValue(7,'x')">Control</button></div>
</body></html>`, { url: 'http://localhost/' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.Element = dom.window.Element;
// Single-realm shim: the registry news up an AbortController for its
// document listeners; jsdom's addEventListener only accepts a SAME-realm
// AbortSignal (browsers have one realm, so this is harness-only).
globalThis.AbortController = dom.window.AbortController;

const registry = await import(pathToFileURL(process.argv[3]).href);

const received = { copy: [], two: [] };
const resolve = (name) => {
  if (name === 'captureValue') return (...a) => { received.copy.push(a); };
  if (name === '_takeTwo') return (...a) => { received.two.push(a); };
  if (name === '_elementOrdinal') return (el) =>
    (el && el.closest && el.closest('.item')) ? 7 : -1;
  return undefined;  // _nope → unknown, on purpose
};
registry.installActionRegistry(resolve);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function realClick(el) {
  const ev = new dom.window.MouseEvent('click', { bubbles: true, cancelable: true });
  let stopped = false;
  const orig = ev.stopPropagation.bind(ev);
  ev.stopPropagation = () => { stopped = true; orig(); };
  el.dispatchEvent(ev);
  return stopped;
}

// ── 1. action compound: nested arg + stopPropagation prefix ──
const stopped1 = realClick(document.getElementById('b1'));
check('nested_call_arg_resolved', JSON.stringify(received.copy[0]) === '[7]');
check('nested_call_arg_is_number', typeof received.copy[0]?.[0] === 'number');
check('stop_propagation_prefix_fired', stopped1 === true);

// ── 2. multi-arg mixed nesting ──
document.getElementById('b2').click();
check('multi_arg_nested', JSON.stringify(received.two[0]) === '[7,3]');

// ── 3. assignment RHS nesting ──
const b3 = document.getElementById('b3');
b3.click();
check('assignment_rhs_nested', b3.dataset.idx === '7');

// ── 4. event-method condition (bilingual-header guard form) ──
const h1 = document.getElementById('h1');
document.getElementById('k1').dispatchEvent(
  new dom.window.MouseEvent('click', { bubbles: true }));
check('guard_true_skips_body', h1.dataset.hit === undefined);
document.getElementById('p1').dispatchEvent(
  new dom.window.MouseEvent('click', { bubbles: true }));
check('guard_false_runs_body', h1.dataset.hit === '1');

// ── 5. unknown nested name: loud refusal, no handler call, no throw ──
const refusals = [];
const origErr = console.error;
console.error = (...a) => { refusals.push(a.map(String).join(' ')); };
const copyCountBefore = received.copy.length;
let threw = false;
try {
  document.getElementById('b4').click();
} catch { threw = true; }
console.error = origErr;
check('unknown_nested_no_throw', threw === false);
check('unknown_nested_refused_loud', refusals.some((m) => m.includes('[actions] refused')));
check('unknown_nested_target_not_called', received.copy.length === copyCountBefore);

// ── 6. controls: literals + quoted strings unchanged ──
document.getElementById('b5').click();
check('plain_args_control', JSON.stringify(received.copy.at(-1)) === '[7,"x"]');

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nested_call_grammar_contract():
    harness = os.path.join(HERE, '_action_registry_harness.mjs')
    snapshot = os.path.join(HERE, '_action_registry_snapshot.mts')
    shutil.copyfile(REGISTRY, snapshot)  # run the REAL registry source
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, snapshot],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for path in (harness, snapshot):
            try:
                os.remove(path)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'action-registry nested-call failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'


@pytest.mark.unit
def test_registry_grammar_pins_no_eval_and_recursion():
    """Source-level nails: allowlist stays eval-free; the nested-call
    recursion seam and call-before-property-path ordering are present."""
    source = open(REGISTRY, encoding='utf-8').read()
    assert 'new Function' not in source
    assert 'eval(' not in source
    assert 'invokeCall(value, event, element, resolve)' in source
    # the call-shape branch must precede the event./this./window. fast paths
    call_branch = source.index('.test(value)) {')
    property_paths = source.index("value.startsWith('event.')")
    assert call_branch < property_paths


# ── 2026-08-21 incident anchor: swarm panel dead clicks ──
# The freshly-built swarm panel header used
#   data-tofu-action="this.closest('.sw-panel').classList.toggle('sw-collapsed')"
# The interpreter's method-call branch resolves the receiver
# ``this.closest('.sw-panel').classList`` — a CALL chained into a PROPERTY —
# through the property-path fast path, which yields undefined, so the click
# was refused with ``Unknown action method: toggle`` (console-only) and the
# panel never opened. Same for the agent card's ``sw-a-open`` toggle.
# Grammar rule: ``call(...).method(...)`` is supported (receiver ends in
# ``)``), but ``call(...).property.anything`` is NOT. Guard: no
# data-tofu-action value anywhere may chain a property off a call result.
_ACTION_ATTR = re.compile(r'data-tofu-action(?:-[a-z]+)?\s*=\s*(["\'])(.*?)\1')
_PROPERTY_AFTER_CALL = re.compile(r'\)\s*\.[A-Za-z_$][\w$]*\.')

# Bite-proof: the detector must catch the exact historical expressions.
_HISTORICAL_BROKEN = [
    "this.closest('.sw-panel').classList.toggle('sw-collapsed')",
    "this.closest('.sw-agent').classList.toggle('sw-a-open')",
]
# …and must NOT flag the supported call-receiver form.
_HISTORICAL_OK = ["this.closest('.stg-mx-editor').remove()"]


@pytest.mark.unit
def test_no_property_chain_after_call_in_actions():
    for expr in _HISTORICAL_BROKEN:
        assert _PROPERTY_AFTER_CALL.search(expr), f'detector blind to: {expr}'
    for expr in _HISTORICAL_OK:
        assert not _PROPERTY_AFTER_CALL.search(expr), f'detector over-fires: {expr}'

    scanned = [os.path.join(ROOT, 'frontend', 'src', 'runtime', 'app-runtime.js'),
               os.path.join(ROOT, 'index.html')]
    html_directories = (
        os.path.join(ROOT, 'static', 'settings_panels'),
        os.path.join(
            ROOT, 'frontend', 'src', 'application-shell', 'fragments'),
    )
    for directory in html_directories:
        if os.path.isdir(directory):
            scanned.extend(
                os.path.join(directory, name)
                for name in sorted(os.listdir(directory))
                if name.endswith('.html'))
    offenders = []
    for path in scanned:
        with open(path, encoding='utf-8') as f:
            source = f.read()
        for match in _ACTION_ATTR.finditer(source):
            value = match.group(2)
            if _PROPERTY_AFTER_CALL.search(value):
                offenders.append(f'{os.path.relpath(path, ROOT)}: {value}')
    assert not offenders, (
        'data-tofu-action chains a property off a call result — the action '
        'interpreter refuses it and the click silently dies. Use a named '
        'action instead (see _swarmToggleClass):\n' + '\n'.join(offenders))

if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
