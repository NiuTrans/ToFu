"""tests/test_frontend_catalog_discoverability.py — the MCP/Skills catalog must
not hide capability it actually ships.

Two behaviours are pinned, both asserted on the RENDERED RESULT rather than on
source text, so a reasonable rewrite of the renderers keeps them biting:

1. **Every category that has entries gets a pill.** The pill bar used to carry
   a hand-copied literal whitelist while ``lib/mcp/registry.py::CATEGORIES``
   was the real source of truth. The lists drifted (10 vs 12), so
   ``Local Life & Travel (China)`` (5 entries) and ``Science & Research`` (2)
   rendered NO pill — their cards existed but could not be filtered to. This
   is the charter's "backend single source of truth got hand-copied into the
   frontend" anti-pattern: a copy never goes red when the original grows.

2. **A declared ``install_note`` reaches the user.** Both catalogs authored
   these notes (8 of them) and ``SkillCatalogEntry`` documents the field as
   "shown under the card", but NOTHING in the frontend rendered it — the
   getting-started instruction for every travel server was invisible. A
   comment promising behaviour with no assertion behind it is exactly the
   ``_BUDGET_EXEMPT_TOOLS`` failure mode.

The renderers are SPLICED from the shipped sources at run time (never copied
into this file — charter: a copied predicate silently stops tracking its
source), and located by SEARCHING for the function definition so a future
module split re-points itself instead of dying on a hardcoded path.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
     tests/test_frontend_catalog_discoverability.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
STATIC_JS = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ── Locate + splice the shipped renderers ────────────────────────────

# Runtime-BUILT bundle outputs (bundle-<8hex>.js / feature-<8hex>.js /
# i18n-<lang>-<8hex>.js), anchored to the 8-hex hash exactly like
# lib/js_bundler.py::_BUILT_BUNDLE_RE — NEVER a bare 'feature-*' glob, which
# would also match the tracked SOURCE feature-loader.js. Built artifacts are
# concatenations of the shipped sources: scanning them double-counts every
# definition and false-trips the "single source of truth" assertion below in
# any tree where the app has actually RUN (a built bundle is left on disk).
_BUILT_BUNDLE_RE = re.compile(
    r'^(?:(?:bundle|feature)-[0-9a-f]{8}|i18n-(?:zh|en)-[0-9a-f]{8})\.js$')


def _find_defining_file(sig: str, *search_dirs: str) -> str:
    """Return the single .js file DEFINING ``sig``.

    Anchored on the function signature (a semantic unit), not a path: the pill
    renderers may legitimately move during a module split, and a hardcoded
    path would turn that move into an unreadable false red. Three states are
    each reported distinctly so a real regression cannot masquerade as drift.
    """
    hits = []
    for d in search_dirs:
        for name in sorted(os.listdir(d)):
            if not name.endswith('.js') or _BUILT_BUNDLE_RE.match(name):
                continue
            p = os.path.join(d, name)
            with open(p, encoding='utf-8') as fh:
                if sig in fh.read():
                    hits.append(p)
    assert hits, (
        f'No static/js file DEFINES {sig!r} — the renderer was deleted, not '
        f'merely relocated. That is a real regression: without it the settings '
        f'panel renders no category pills at all.'
    )
    assert len(hits) == 1, (
        f'{sig!r} is defined in MORE than one file ({hits}) — the single '
        f'source of truth has been duplicated.'
    )
    return hits[0]


def _splice_fn(path: str, sig: str) -> str:
    """Slice a top-level ``function name(...) {`` through its closing brace."""
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    start = src.index(sig)
    end = src.index('\n}\n', start) + len('\n}\n')
    return src[start:end]


def _splice_const(path: str, name: str) -> str:
    """Slice a top-level ``var NAME = [ ... ];`` declaration."""
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    m = re.search(r'^var %s = \[.*?\];' % re.escape(name), src,
                  re.S | re.M)
    assert m, f'{name} not found as a top-level array in {path}'
    return m.group(0)


_MCP_ORDER_SIG = 'function _mcpOrderedCategories(cats) {'
_SKILLS_ORDER_SIG = 'function _skillsOrderedCategories(cats) {'


def _mcp_orderer() -> str:
    path = _find_defining_file(_MCP_ORDER_SIG,
                               os.path.join(STATIC_JS, 'settings'))
    return (_splice_const(path, '_CAT_ORDER') + '\n'
            + _splice_fn(path, _MCP_ORDER_SIG))


def _skills_orderer() -> str:
    path = _find_defining_file(_SKILLS_ORDER_SIG, STATIC_JS)
    return (_splice_const(path, '_SKILLS_CAT_ORDER') + '\n'
            + _splice_fn(path, _SKILLS_ORDER_SIG))


_ORDER_HARNESS = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
const orderer = eval(process.argv[3]);
const cats = JSON.parse(process.argv[4]);
console.log(JSON.stringify(orderer(cats)));
"""


def _run_orderer(js_text: str, fn_name: str, cats: dict) -> list:
    src = os.path.join(HERE, f'_cd_src_{fn_name}.js')
    harness = os.path.join(HERE, f'_cd_harness_{fn_name}.js')
    with open(src, 'w', encoding='utf-8') as fh:
        fh.write(js_text)
    with open(harness, 'w', encoding='utf-8') as fh:
        fh.write(_ORDER_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, src, fn_name, json.dumps(cats)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node failed: {proc.stderr[:600]}'
        return json.loads(proc.stdout.strip())
    finally:
        for p in (src, harness):
            if os.path.exists(p):
                os.remove(p)


# ── Backend category truth ───────────────────────────────────────────

def _backend_categories(module_path: str) -> list[str]:
    """Resolve CATEGORIES to its literal string values without importing.

    Import is avoided deliberately: ``lib.mcp.registry`` is reachable only
    through chains that a sibling's unrelated syntax error can break, which
    would turn this guard into a false red about something it does not test.
    """
    with open(module_path, encoding='utf-8') as fh:
        src = fh.read()
    consts = dict(re.findall(r"^(CAT_[A-Z_]+)\s*=\s*'([^']+)'", src, re.M))
    m = re.search(r'^CATEGORIES = \[(.*?)\]', src, re.S | re.M)
    assert m, f'CATEGORIES not found in {module_path}'
    names = re.findall(r'CAT_[A-Z_]+', m.group(1))
    return [consts[n] for n in names if n in consts]


def _entry_categories(module_path: str) -> set[str]:
    """Categories that actually have >=1 catalog entry."""
    with open(module_path, encoding='utf-8') as fh:
        src = fh.read()
    consts = dict(re.findall(r"^(CAT_[A-Z_]+)\s*=\s*'([^']+)'", src, re.M))
    used = re.findall(r"'category':\s*(CAT_[A-Z_]+)", src)
    return {consts[u] for u in used if u in consts}


MCP_REGISTRY = os.path.join(ROOT, 'lib', 'mcp', 'registry.py')
SKILLS_CATALOG = os.path.join(ROOT, 'lib', 'skills', 'catalog.py')


# ── 1. No category with entries may be unreachable ───────────────────

@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_every_mcp_category_with_entries_gets_a_pill():
    """RESULT-level: feed the orderer the real per-category counts and require
    every populated category back.

    The old literal whitelist fails this the moment the backend adds a
    category — which is precisely what happened.
    """
    populated = _entry_categories(MCP_REGISTRY)
    assert populated, 'scan surface empty — the category regex stopped matching'
    cats = {c: 1 for c in sorted(populated)}
    rendered = _run_orderer(_mcp_orderer(), '_mcpOrderedCategories', cats)

    missing = sorted(populated - set(rendered))
    assert not missing, (
        f'MCP categories with real entries render NO pill: {missing}. '
        f'Their cards exist but cannot be filtered to. The pill set must be '
        f'derived from the catalog data, not from a hand-copied list.'
    )


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_every_skills_category_with_entries_gets_a_pill():
    populated = _entry_categories(SKILLS_CATALOG)
    assert populated, 'scan surface empty — the category regex stopped matching'
    cats = {c: 1 for c in sorted(populated)}
    rendered = _run_orderer(_skills_orderer(), '_skillsOrderedCategories', cats)

    missing = sorted(populated - set(rendered))
    assert not missing, (
        f'Skills categories with real entries render NO pill: {missing}.'
    )


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_a_category_absent_from_the_preference_list_still_renders():
    """The load-bearing property: an UNKNOWN category must be appended, not
    dropped.

    This is what makes the fix structural rather than "add the two missing
    strings". Without it the next category added to the backend disappears
    again and no test goes red.
    """
    cats = {'Development': 2, 'Totally New Category': 3}
    rendered = _run_orderer(_mcp_orderer(), '_mcpOrderedCategories', cats)
    assert 'Totally New Category' in rendered, (
        f'an unknown category was DROPPED instead of appended: {rendered}'
    )
    # Known ones keep their curated order ahead of the unknown tail.
    assert rendered.index('Development') < rendered.index('Totally New Category')


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_empty_categories_do_not_render_a_pill():
    """Complement: derivation must not start inventing pills for categories
    with zero entries — otherwise "render everything" would pass the tests
    above while making the bar useless."""
    declared = _backend_categories(MCP_REGISTRY)
    populated = _entry_categories(MCP_REGISTRY)
    empty = [c for c in declared if c not in populated]
    assert empty, (
        'no declared-but-empty category exists right now, so this complement '
        'cannot be exercised — re-check the scan surface'
    )
    cats = {c: 1 for c in sorted(populated)}
    rendered = _run_orderer(_mcp_orderer(), '_mcpOrderedCategories', cats)
    leaked = sorted(set(empty) & set(rendered))
    assert not leaked, f'pills rendered for categories with no entries: {leaked}'


# ── 2. install_note must reach the user ──────────────────────────────

def _authored_notes(module_path: str) -> list[str]:
    """The install_note VALUES authored in a catalog (skip the TypedDict decl)."""
    with open(module_path, encoding='utf-8') as fh:
        src = fh.read()
    return re.findall(r"'install_note':\s*'((?:[^'\\]|\\.)*)'", src)


def test_install_note_is_actually_rendered_somewhere():
    """The field is authored 8× across both catalogs; a renderer must consume it.

    Asserted as "some shipped renderer reads the field", not as an exact DOM
    string, so restyling the note does not false-red. Before this guard the
    answer was zero renderers.
    """
    notes = _authored_notes(MCP_REGISTRY) + _authored_notes(SKILLS_CATALOG)
    assert notes, (
        'no install_note authored in either catalog — scan surface is empty, '
        'so this guard would pass vacuously'
    )

    consumers = []
    for d, names in (
        (os.path.join(STATIC_JS, 'settings'), ['mcp.js']),
        (STATIC_JS, ['skills.js']),
    ):
        for name in names:
            p = os.path.join(d, name)
            with open(p, encoding='utf-8') as fh:
                if 'install_note' in fh.read():
                    consumers.append(os.path.relpath(p, ROOT))

    assert len(consumers) == 2, (
        f'install_note is authored {len(notes)}× in the catalogs but consumed '
        f'by only {consumers} — a note the user never sees is worse than no '
        f'note, because the author believes the guidance shipped. Both the MCP '
        f'and Skills card renderers must render it.'
    )


def test_every_credential_bearing_travel_entry_explains_how_to_get_the_key():
    """Content contract: if a card needs a key, it must say where to get one.

    Not styling — this is the actual cognitive-load requirement. Without it a
    user faces a bare password box with no route to a credential.
    """
    with open(MCP_REGISTRY, encoding='utf-8') as fh:
        src = fh.read()
    # Entry blocks in the China local-life category.
    blocks = re.findall(r"\{\s*'id':\s*'([^']+)'(.*?)\n    \},",
                        src, re.S)
    checked = 0
    for sid, body in blocks:
        if 'CAT_LOCAL_CN' not in body:
            continue
        needs_key = bool(re.search(r"'required':\s*True", body))
        if not needs_key:
            continue
        checked += 1
        has_route = ("'install_note'" in body
                     or re.search(r"'hint':\s*'[^']+'", body))
        assert has_route, (
            f'{sid} requires a credential but offers no route to obtain it '
            f'(no install_note, no hint) — the user gets a bare password field.'
        )
    assert checked >= 3, (
        f'expected several credential-bearing China travel entries, scanned '
        f'only {checked} — the block regex likely stopped matching'
    )


# ── 3. Getting a credential must be a CLICKABLE route, not prose ─────
#
# Layer 2. The travel entries previously carried the console path inside
# `hint`, which is the input's PLACEHOLDER: uncklickable, truncated by the
# field width, gone the instant the user types — and, worst of all, REPLACED
# wholesale by the "already saved" notice on a reinstall, i.e. it vanished
# exactly when someone was rotating an expired key. `obtain_url` /
# `obtain_steps` are structured fields rendered as a real link.

_OBTAIN_SIG = 'function _mcpObtainBlock(spec) {'
_PLACEHOLDER_SIG = 'function _mcpPlaceholder(spec, hasStored) {'

_RENDER_HARNESS = r"""
const fs = require('fs');
// Minimal shims: the spliced helpers use escapeHtml + t() from the app bundle.
global.escapeHtml = function (s) {
  return String(s === undefined || s === null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
};
global.t = function (k, vars) {
  var out = 'T[' + k + ']';
  if (vars) Object.keys(vars).forEach(function (v) { out += '{' + v + '=' + vars[v] + '}'; });
  return out;
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
const fn = eval(process.argv[3]);
const args = JSON.parse(process.argv[4]);
console.log(JSON.stringify(fn.apply(null, args)));
"""


def _splice_helpers() -> str:
    """Splice the obtain-block + placeholder helpers from the shipped file."""
    path = _find_defining_file(_OBTAIN_SIG, os.path.join(STATIC_JS, 'settings'))
    return (_splice_fn(path, _PLACEHOLDER_SIG) + '\n'
            + _splice_fn(path, _OBTAIN_SIG))


def _run_fn(js_text: str, fn_name: str, args: list):
    src = os.path.join(HERE, f'_cd_h_{fn_name}.js')
    harness = os.path.join(HERE, f'_cd_hh_{fn_name}.js')
    with open(src, 'w', encoding='utf-8') as fh:
        fh.write(js_text)
    with open(harness, 'w', encoding='utf-8') as fh:
        fh.write(_RENDER_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, src, fn_name, json.dumps(args)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node failed: {proc.stderr[:600]}'
        return json.loads(proc.stdout.strip())
    finally:
        for p in (src, harness):
            if os.path.exists(p):
                os.remove(p)


def _travel_specs() -> list[tuple[str, dict]]:
    """(entry_id, env_spec) for every credential-bearing China travel entry."""
    from lib.mcp.registry import CATALOG, CAT_LOCAL_CN
    out = []
    for e in CATALOG:
        if e.get('category') != CAT_LOCAL_CN:
            continue
        for s in (e.get('env_specs') or []):
            if s.get('required'):
                out.append((e['id'], s))
    return out


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_obtain_route_renders_a_real_clickable_link():
    """RESULT-level: the shipped helper emits an <a href> for a declared route."""
    specs = _travel_specs()
    assert specs, 'scan surface empty — no credential-bearing travel entries'

    helpers = _splice_helpers()
    checked = 0
    for sid, spec in specs:
        if not spec.get('obtain_url'):
            continue
        checked += 1
        html = _run_fn(helpers, '_mcpObtainBlock', [spec])
        assert '<a ' in html and 'href="' in html, (
            f'{sid}/{spec["key"]}: declared obtain_url but no <a href> was '
            f'rendered — the user still cannot click through. Got: {html!r}'
        )
        assert spec['obtain_url'] in html, (
            f'{sid}: the rendered link does not point at the declared URL'
        )
        assert 'target="_blank"' in html and 'noopener' in html, (
            f'{sid}: external link must open in a new tab with noopener'
        )
    assert checked >= 3, (
        f'expected the travel entries to declare obtain_url, only {checked} do'
    )


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_obtain_block_is_empty_when_no_route_is_declared():
    """Complement: no route declared → render NOTHING.

    Without this, "always emit a link block" would satisfy the test above
    while putting an empty affordance on every credential-free server.
    """
    helpers = _splice_helpers()
    html = _run_fn(helpers, '_mcpObtainBlock', [{'key': 'X'}])
    assert html == '', f'expected empty string for a route-less spec, got {html!r}'


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_obtain_link_rejects_a_non_http_scheme():
    """A catalog entry is server-owned, but this is the one place a string
    becomes a clickable target — a javascript: URL must not survive."""
    helpers = _splice_helpers()
    html = _run_fn(helpers, '_mcpObtainBlock',
                   [{'key': 'X', 'obtain_url': 'javascript:alert(1)'}])
    assert 'javascript:' not in html, f'dangerous scheme rendered: {html!r}'


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_saved_credential_does_not_erase_the_input_hint():
    """The reinstall defect: `hasStored` used to REPLACE the hint.

    A user rotating an expired key opens the same modal and needs to know what
    to paste; the old code swapped that guidance for "saved, leave blank".
    Both facts must survive — this asserts the hint is still the placeholder
    when a value is already stored.
    """
    helpers = _splice_helpers()
    spec = {'key': 'AMAP_MAPS_API_KEY', 'hint': '粘贴你的 Key'}
    stored = _run_fn(helpers, '_mcpPlaceholder', [spec, True])
    fresh = _run_fn(helpers, '_mcpPlaceholder', [spec, False])
    assert stored == spec['hint'], (
        f'the hint was evicted by the saved-notice when a value exists: '
        f'{stored!r} — that is the exact moment a key is being rotated'
    )
    assert fresh == spec['hint']


def test_shared_credential_is_detected_rather_than_re_requested():
    """Two cards can legitimately share one credential.

    Measured: ROLLINGGO_API_KEY is declared by rollinggo-hotel AND
    rollinggo-flight; GITHUB_PERSONAL_ACCESS_TOKEN by github AND github-batch.
    The renderer must be able to see that, otherwise installing the second one
    asks for a key the user already gave us and they go re-apply.
    """
    from lib.mcp.registry import CATALOG
    import collections
    key2ids = collections.defaultdict(list)
    for e in CATALOG:
        for s in (e.get('env_specs') or []):
            key2ids[s['key']].append(e['id'])
    shared = {k: v for k, v in key2ids.items() if len(v) > 1}
    assert 'ROLLINGGO_API_KEY' in shared, (
        'the RollingGo pair no longer shares a key — re-check the scan surface'
    )

    path = _find_defining_file('function _mcpSharedCredentialSources(key, selfId) {',
                               os.path.join(STATIC_JS, 'settings'))
    body = _splice_fn(path, 'function _mcpSharedCredentialSources(key, selfId) {')
    # It must consult STORED keys (what the user actually supplied), not the
    # declared spec — a sibling that merely declares the key has nothing to
    # reuse yet.
    assert 'stored_env_keys' in body, (
        'shared-credential detection must key off stored_env_keys (what is '
        'actually saved), not off the declared env_specs'
    )
    assert 'selfId' in body, 'must exclude the entry being installed itself'


# ── 4. Suggestions must never dead-end ───────────────────────────────
#
# Layer 3. The "nothing installed" empty state offers a few servers to start
# with. The load-bearing constraint is NOT that suggestions exist — it is that
# every suggested card can actually be finished by the user unaided. A chip
# for a server whose credential needs a business process is worse than no
# chip: it spends the user's trust and their time.

_SUGGEST_SIG = 'function _mcpSelfServeSuggestions(limit) {'

_SUGGEST_HARNESS = r"""
const fs = require('fs');
global._mcpCatalog = JSON.parse(process.argv[3]);
eval(fs.readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify(_mcpSelfServeSuggestions(50).map(function (e) { return e.id; })));
"""


def _run_suggest(catalog: list) -> list:
    path = _find_defining_file(_SUGGEST_SIG, os.path.join(STATIC_JS, 'settings'))
    body = _splice_fn(path, _SUGGEST_SIG)
    src = os.path.join(HERE, '_cd_sug.js')
    harness = os.path.join(HERE, '_cd_sug_h.js')
    with open(src, 'w', encoding='utf-8') as fh:
        fh.write(body)
    with open(harness, 'w', encoding='utf-8') as fh:
        fh.write(_SUGGEST_HARNESS)
    try:
        proc = subprocess.run(['node', harness, src, json.dumps(catalog)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node failed: {proc.stderr[:600]}'
        return json.loads(proc.stdout.strip())
    finally:
        for p in (src, harness):
            if os.path.exists(p):
                os.remove(p)


def _catalog_as_api_would_serve() -> list:
    """The catalog shape the frontend receives (nothing installed yet)."""
    from lib.mcp.registry import CATALOG
    out = []
    for e in CATALOG:
        out.append({
            'id': e['id'], 'name': e.get('name', e['id']),
            'description': e.get('description', ''),
            'featured': bool(e.get('featured')),
            'env_specs': e.get('env_specs') or [],
            'installed': False, 'stored_env_keys': [],
        })
    return out


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_suggestions_never_include_an_entry_the_user_cannot_finish():
    """Every suggested id must be self-serve, checked against the REAL catalog.

    Self-serve == needs no credential, or every required credential declares
    an obtain_url. Measured at authoring time: 15 qualify, 36 do not.
    """
    catalog = _catalog_as_api_would_serve()

    def _self_serve(e):
        req = [s for s in e['env_specs'] if s.get('required')]
        return (not req) or all(s.get('obtain_url') for s in req)

    gated = {e['id'] for e in catalog if not _self_serve(e)}
    assert gated, 'scan surface empty — no gated entries, guard would be vacuous'

    suggested = _run_suggest(catalog)
    assert suggested, 'no suggestions at all — the empty state has no next step'

    leaked = sorted(set(suggested) & gated)
    assert not leaked, (
        f'suggested entries the user cannot finish unaided: {leaked}. A chip '
        f'that dead-ends costs more than showing nothing.'
    )


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_already_installed_entries_are_not_suggested():
    """Complement: suggesting what is already installed makes the strip noise.

    Also stops "suggest everything" from passing the guard above.
    """
    catalog = _catalog_as_api_would_serve()
    for e in catalog:
        e['installed'] = True
    assert _run_suggest(catalog) == [], (
        'entries already installed were still suggested'
    )


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_a_credential_free_entry_is_suggestable():
    """Positive control: the pool must not be empty by construction.

    Without this, tightening the filter to "return nothing" would satisfy both
    tests above while removing the feature entirely.
    """
    catalog = [{
        'id': 'nokey', 'name': 'No Key Needed', 'description': '',
        'featured': False, 'env_specs': [], 'installed': False,
        'stored_env_keys': [],
    }]
    assert _run_suggest(catalog) == ['nokey']
