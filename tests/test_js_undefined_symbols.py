"""Static gate: no JS symbol may be REFERENCED but never DEFINED in the bundle.

Epic pt_fb854394c1f34eea. Bug class: a retired-global alias ships as a live
browser ReferenceError — invisible to ``node --check`` (syntax only) and to
the jsdom harnesses (which pre-inject mock globals, and never execute
``setTimeout``/callback bodies). Root case: ``ff7176dd`` retired
``streamBufs`` but left 7 ``dBuf`` reads inside a 300 ms deferred repaint —
uncaught ReferenceError on every refresh onto a generating conversation
(fixed in 90ddbb96; 10 hits in one afternoon in logs/error.log).

The gate runs ``tests/_undef_scan.js`` — a REAL parser (TypeScript compiler
API) with scope analysis over every file in ``lib.js_bundler``'s
``_BUNDLE_FILES`` + ``_DEFERRED_FILES`` (manifest order, browser <script>
shared-global semantics) plus index.html's inline <script> blocks. It flags
identifiers read-but-never-declared, honouring the deliberate idioms the
codebase uses (see the scanner header for the full list):

  * cross-file globals (top-level declarations of ANY bundle file),
  * ``window.X =`` / IIFE-alias ``global.X =`` declarations,
  * typeof-guarded optional globals (``if (typeof X === 'function') X()``),
  * plain ``window.X`` feature-probe reads (cannot throw), while
    ``window.X.deep`` / ``window.X()`` on a missing X still flags.

Synthetic NEUTER tests below prove each flagging path bites; the bundle test
asserts the real tree stays at zero.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

import pytest

from tests._jsdom import ROOT, node_deps_available

_SCANNER = os.path.join(ROOT, 'tests', '_undef_scan.js')

# Vendor libraries loaded via <script> tags or lazy loaders (static/vendor/)
# whose globals bundle files legitimately read. grep static/vendor to extend.
_VENDOR_GLOBALS = [
    'hljs', 'marked', 'DOMPurify',           # eager <script defer> in index.html
    'katex', 'renderMathInElement',          # lazy: core.js _ensureKatex()
    'pdfjsLib',                              # lazy: core.js _ensurePdfJs()
    'html2canvas',                           # lazy: export-images
]


def _run_scan(files, *, html=None, extra_globals=None, cwd=None):
    """Invoke the scanner; return parsed JSON. Skips when node is absent."""
    if not node_deps_available():
        pytest.skip('node + dev-deps not installed (run `npm install`)')
    spec = {
        'files': files,
        'html': html or [],
        'extraGlobals': list(extra_globals or []),
    }
    with tempfile.NamedTemporaryFile(
        'w', suffix='.json', delete=False, encoding='utf-8') as fh:
        json.dump(spec, fh)
        spec_path = fh.name
    try:
        proc = subprocess.run(
            ['node', _SCANNER, ROOT, spec_path],
            capture_output=True, text=True, timeout=120, cwd=cwd or ROOT)
    finally:
        os.remove(spec_path)
    assert proc.returncode == 0, (
        f'scanner failed rc={proc.returncode}:\n{proc.stderr[:2000]}')
    return json.loads(proc.stdout)


def _bundle_files():
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    return [f'static/js/{f}' for f in (*_BUNDLE_FILES, *_DEFERRED_FILES)]


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    return os.path.relpath(str(p), ROOT)


# ── The gate itself ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_no_undefined_symbols_in_bundle():
    """Zero read-but-never-defined identifiers across the served JS."""
    out = _run_scan(_bundle_files(), html=['index.html'],
                    extra_globals=_VENDOR_GLOBALS)
    v = out['violations']
    assert not v, (
        f'{len(v)} undefined-symbol read(s) in the bundle '
        f'(first 20):\n' + '\n'.join(
            f"  {x['file']}:{x['line']}:{x['col']}  {x['name']}" for x in v[:20]))
    s = out['sloppy']
    assert not s, (
        f'{len(s)} sloppy-global assignment(s) — undeclared `X = …` leaks '
        f'onto window and is a strict-mode ReferenceError (first 10):\n'
        + '\n'.join(f"  {x['file']}:{x['line']}  {x['name']}" for x in s[:10]))


@pytest.mark.unit
def test_every_manifest_file_scanned():
    """Drift guard: a manifest entry that silently 404s would shrink the
    gate's coverage — every listed file must exist on disk."""
    missing = [f for f in _bundle_files()
               if not os.path.isfile(os.path.join(ROOT, f))]
    assert not missing, f'manifest file(s) missing on disk: {missing}'


# ── NEUTER: the flagging paths must bite ────────────────────────────────

@pytest.mark.unit
def test_flags_undefined_bare_read_inside_settimeout(tmp_path):
    """The dBuf pattern itself: a retired-global read hidden inside a
    setTimeout callback body — the hole eval-harnesses never execute."""
    f = _write(tmp_path, 'a.js', """
function showStreamingUIForConv(convId) {
  setTimeout(() => {
    const dBuf = undefined;
    document.title = dBuf.content;
  }, 300);
  setTimeout(() => { window.__x = retiredGlobal.read(); }, 300);
}
""")
    out = _run_scan([f])
    names = {x['name'] for x in out['violations']}
    assert 'retiredGlobal' in names, out
    assert 'dBuf' not in names  # declared locally in its own callback scope


@pytest.mark.unit
def test_accepts_cross_file_declaration(tmp_path):
    a = _write(tmp_path, 'a.js', 'const sharedHelper = () => 1;\n'
                                 'function alsoShared() { return 2; }\n')
    b = _write(tmp_path, 'b.js', 'const x = sharedHelper() + alsoShared();\n')
    out = _run_scan([a, b])
    assert out['violations'] == [], out


@pytest.mark.unit
def test_accepts_window_and_iife_alias_declarations(tmp_path):
    a = _write(tmp_path, 'a.js', """
window.fromLiteral = 1;
(function (global) {
  const Api = { ping() { return 1; } };
  global.Api = Api;
})(typeof window !== 'undefined' ? window : this);
""")
    b = _write(tmp_path, 'b.js', 'const y = fromLiteral + Api.ping();\n')
    out = _run_scan([a, b])
    assert out['violations'] == [], out


@pytest.mark.unit
def test_typeof_guard_patterns_are_safe_but_unguarded_flags(tmp_path):
    f = _write(tmp_path, 'a.js', """
if (typeof optLib === 'function') optLib();
const t = typeof optTwo !== 'undefined' ? optTwo.value : null;
typeof optThree !== 'undefined' && optThree.go();
function later() { return optLib.other + naked; }
""")
    out = _run_scan([f])
    names = {x['name'] for x in out['violations']}
    # Guarded idioms pass; the unguarded `naked` read flags. Note `optLib`
    # flags too — its guard only covers the if-branch, not `later()`.
    assert 'naked' in names
    assert 'optTwo' not in names and 'optThree' not in names
    assert 'optLib' in names


@pytest.mark.unit
def test_window_reads_flag_only_when_throwing(tmp_path):
    f = _write(tmp_path, 'a.js', """
const probe = window.maybeFeature;           // safe: undefined, no throw
if (window.deployOverride) probe = 1;        // safe: truthiness probe
window.missingObj.method();                  // THROWS (call on undefined)
const deep = window.missingToo.deep;         // THROWS (chain on undefined)
if (typeof window.guardedFn === 'function') window.guardedFn();  // guarded
""")
    out = _run_scan([f])
    names = {x['name'] for x in out['violations']}
    assert names == {'missingObj', 'missingToo'}, out


@pytest.mark.unit
def test_regex_and_string_lookalikes_are_not_references(tmp_path):
    """The regex-prototype's failure mode: identifier-ish text inside
    regex literals / strings / comments must never be scanned."""
    f = _write(tmp_path, 'a.js', """
const re = /retiredGlobal+/g;
const s = 'retiredGlobal.read()';
const t = `template ${'inner'} retiredGlobal`;
// retiredGlobal in a comment
/* and a block retiredGlobal comment */
""")
    out = _run_scan([f])
    assert out['violations'] == [], out


@pytest.mark.unit
def test_sloppy_assignment_reported_and_declares(tmp_path):
    f = _write(tmp_path, 'a.js', 'function f() { leaked = []; return leaked; }\n'
                                 'const after = leaked.length;\n')
    out = _run_scan([f])
    assert [s['name'] for s in out['sloppy']] == ['leaked']
    # Sloppy semantics: once assigned, subsequent reads resolve.
    assert out['violations'] == [], out


@pytest.mark.unit
def test_scopes_do_not_leak_locals_outward(tmp_path):
    f = _write(tmp_path, 'a.js', """
function outer() { const hidden = 1; return hidden; }
function elsewhere() { return hidden; }
const { destructured } = { destructured: 1 };
function usesDestructure() { return destructured; }
""")
    out = _run_scan([f])
    names = {x['name'] for x in out['violations']}
    assert names == {'hidden'}, out
