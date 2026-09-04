"""tests/test_frontend_recent_project_search.py — Recent-projects search box.

The Project Co-Pilot modal's "Recent" card gained a search box so the user can
quickly find a project among many recents. The render path (project.js) is:

  renderRecentProjects()  → caches the server list into ``_recentProjects``
  _filterRecentProjects(q)→ sets ``_recentFilter`` and re-renders
  _renderRecentList()     → filters by case-insensitive path substring,
                            highlights the match (HTML-escaped), updates the
                            ``#recentCount`` badge, and shows an empty-state
                            when nothing matches.

This drives the authored Project section through the logical runtime test view
under jsdom, seeds a set of
recent projects via an in-scope bridge (the state lives in a top-level ``let``,
unreachable from the outer eval scope — same trick as the _streamTimers tests),
and asserts filter / highlight / count / empty-state / XSS-escape behaviour.
A double-neuter proves the filter and the escaping are each load-bearing.
Skips cleanly without node/jsdom.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
REPOSITORY_ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
_PROJECT_SRC = os.path.join(JS_DIR, 'project.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!DOCTYPE html><body>
  <div id="recentProjectPaths" hidden>
    <span id="recentCount"></span>
    <input id="recentSearchInput">
    <button id="recentSearchClear" hidden></button>
    <div id="recentPathsList"></div>
  </div>
</body>`, { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
// project.js now delegates conv lookups to core.js's getConvById/getActiveConv;
// stub them so the cross-file references resolve (not exercised by this test).
global.getConvById = () => null;
global.getActiveConv = () => null;

let src = fs.readFileSync(SRC, 'utf8');
// The recent-list state lives in a top-level `let` (module scope), unreachable
// from here — append an in-scope bridge to seed it.
src += '\n;globalThis.__seedRecent = (arr) => { _recentProjects = arr; };';
eval(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const listEl = document.getElementById('recentPathsList');
const countEl = document.getElementById('recentCount');
const clearBtn = document.getElementById('recentSearchClear');

// Seed a realistic set of recents (one with an XSS-y basename).
globalThis.__seedRecent([
  { path: '/home/user/chatui', count: 3 },
  { path: '/home/user/tofu-personal', count: 12 },
  { path: '/home/user/ds_aime24', count: 16 },
  { path: '/srv/data/<img src=x onerror=alert(1)>', count: 1 },
]);

// (0) Unfiltered render shows all + count = total, clear button hidden.
_filterRecentProjects('');
check('all_rendered', (listEl.querySelectorAll('.recent-path-item').length) === 4);
check('count_total', countEl.textContent === '4');
check('clear_hidden_when_empty_query', clearBtn.hidden === true);

// (1) Filter narrows to matching paths (case-insensitive) and updates count.
_filterRecentProjects('TOFU');
const afterTofu = listEl.querySelectorAll('.recent-path-item');
check('filter_narrows', afterTofu.length === 1);
check('filter_correct_item', listEl.innerHTML.includes('tofu-personal'));
check('filter_excludes_others', !listEl.innerHTML.includes('ds_aime24'));
check('count_fraction', countEl.textContent === '1/4');
check('clear_shown_when_query', clearBtn.hidden === false);

// (2) Match is highlighted (HTML-escaped <mark>).
check('match_highlighted', listEl.innerHTML.includes('<mark class="recent-hl">'));

// (3) No-match → empty state.
_filterRecentProjects('zzzznope');
check('empty_state', listEl.innerHTML.includes('recent-paths-empty'));
check('empty_count', countEl.textContent === '0/4');

// (4) XSS: searching the payload itself must escape the match (a naive
//     highlighter that emits the raw matched slice would inject a LIVE <img>
//     element inside <mark>). The search term IS the dangerous substring so
//     the *match* branch of the highlighter is what's under test. Assert on
//     the live DOM (no <img> node), not on innerHTML — attribute-value
//     re-serialization is not executable and would false-positive.
_filterRecentProjects('<img');
check('xss_no_live_element', listEl.querySelectorAll('img').length === 0);
const _nameSpan = listEl.querySelector('.recent-path-name');
check('xss_name_escaped', !!_nameSpan && _nameSpan.textContent.includes('<img'));
check('xss_present_escaped', listEl.innerHTML.includes('&lt;img'));
// The HIGHLIGHTED match itself must be escaped — a naive highlighter that
// emits the raw matched slice would produce `recent-hl"><img` (unescaped).
check('match_escaped', listEl.innerHTML.includes('recent-hl">&lt;img') &&
                       !listEl.innerHTML.includes('recent-hl"><img'));

// (5) Clearing the filter restores the full list.
_filterRecentProjects('');
check('restored_all', listEl.querySelectorAll('.recent-path-item').length === 4);

console.log(out.join('\n'));
"""


def _run(src_path):
    harness = os.path.join(HERE, '_recent_search_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, src_path, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


_EXPECTED = (
    'all_rendered', 'count_total', 'clear_hidden_when_empty_query',
    'filter_narrows', 'filter_correct_item', 'filter_excludes_others',
    'count_fraction', 'clear_shown_when_query', 'match_highlighted',
    'empty_state', 'empty_count', 'xss_no_live_element', 'xss_name_escaped',
    'xss_present_escaped', 'match_escaped', 'restored_all',
)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_recent_project_search():
    output = _run(_PROJECT_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'recent-search failures:\n' + output
    for must in _EXPECTED:
        assert ('PASS ' + must) in output, output


def _nc(anchor, replacement, must_fail, must_still_pass):
    """Patch a COPY of project.js, run, assert target checks flip to FAIL while
    a control stays PASS, then assert the shipped file is byte-identical."""
    with open(_PROJECT_SRC, encoding='utf-8') as f:
        original = f.read()
    assert anchor in original, f'NC anchor not found: {anchor[:70]!r}'
    patched = original.replace(anchor, replacement, 1)
    assert patched != original, 'NC replacement was a no-op'
    copy_path = _PROJECT_SRC + '.nc_copy.js'
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        for m in must_fail:
            assert ('FAIL ' + m) in output, f'NC: expected {m} to FAIL:\n{output}'
        for m in must_still_pass:
            assert ('PASS ' + m) in output, \
                f'NC must be surgical — {m} should still PASS:\n{output}'
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_PROJECT_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_filter_is_load_bearing():
    """Neuter the substring filter (always keep every item) → the narrow /
    empty-state / count-fraction checks flip to FAIL, while highlight+escape
    (separate concern) still pass on the unfiltered set."""
    _nc(
        anchor='? _recentProjects.filter(\n        (item) => item.path.toLowerCase().includes(q.toLowerCase()),\n      )',
        replacement='? _recentProjects.slice()',
        must_fail=['filter_narrows', 'empty_state'],
        must_still_pass=['all_rendered', 'count_total'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_highlight_escape_is_load_bearing():
    """Neuter the highlight escaping (emit the raw match) → the XSS-escape check
    flips to FAIL, while the filter narrowing (separate concern) still passes."""
    _nc(
        anchor='const match = escapeHtml(text.slice(idx, idx + query.length));',
        replacement='const match = text.slice(idx, idx + query.length);',
        must_fail=['match_escaped'],
        must_still_pass=['filter_narrows', 'match_highlighted'],
    )


_I18N_SRC = os.path.join(
    REPOSITORY_ROOT, 'frontend', 'src', 'i18n', 'locales', 'zh.json')

# Loads the authoritative zh catalogue + project.js under jsdom, applies the
# same data-i18n* projection needed by this fixture, and drives
# _renderRecentList() to
# assert the JS-rendered empty-state uses t() (zh), not a hardcoded English
# literal. The DOM mirrors the real index.html markup for the Recent card.
_I18N_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const I18N = process.argv[2];
const SRC = process.argv[3];
const ROOT = process.argv[4];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!DOCTYPE html><body>
  <div id="recentProjectPaths" hidden>
    <div class="pm-section-label">
      <span data-i18n="pm.recent">Recent</span>
      <span class="pm-count" id="recentCount"></span>
      <button class="recent-paths-clear" id="recentClearAll" title="Clear all" data-i18n-title="pm.recentClearAll"></button>
    </div>
    <div class="recent-search">
      <input id="recentSearchInput" class="recent-search-input" placeholder="Search recent…" data-i18n-placeholder="pm.recentSearchPlaceholder">
      <button class="recent-search-clear" id="recentSearchClear" title="Clear search" data-i18n-title="pm.recentClearSearch" hidden></button>
    </div>
    <div class="recent-paths-list" id="recentPathsList"></div>
  </div>
</body>`, { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
global.localStorage = dom.window.localStorage;
global.navigator = dom.window.navigator;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.getConvById = () => null;
global.getActiveConv = () => null;

const catalog = JSON.parse(fs.readFileSync(I18N, 'utf8'));
global.t = (key) => Object.prototype.hasOwnProperty.call(catalog, key)
  ? catalog[key] : key;
global._applyI18n = (root = document) => {
  root.querySelectorAll('[data-i18n]').forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  root.querySelectorAll('[data-i18n-placeholder]').forEach((element) => {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  });
  root.querySelectorAll('[data-i18n-title]').forEach((element) => {
    element.title = t(element.dataset.i18nTitle);
  });
};
let src = fs.readFileSync(SRC, 'utf8');
src += '\n;globalThis.__seedRecent = (arr) => { _recentProjects = arr; };';
eval(src);
_applyI18n();                                    // apply the static data-i18n* attrs

const out = [];
function check(name, cond, got) { out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : ' :: got=' + JSON.stringify(got))); }

const listEl = document.getElementById('recentPathsList');

// (a) static label + tooltips + placeholder localized to zh
check('label_zh', document.querySelector('[data-i18n="pm.recent"]').textContent === '最近',
      document.querySelector('[data-i18n="pm.recent"]').textContent);
check('placeholder_zh', document.getElementById('recentSearchInput').getAttribute('placeholder') === '搜索最近…',
      document.getElementById('recentSearchInput').getAttribute('placeholder'));
check('clear_all_title_zh', document.getElementById('recentClearAll').getAttribute('title') === '清空全部',
      document.getElementById('recentClearAll').getAttribute('title'));
check('clear_search_title_zh', document.getElementById('recentSearchClear').getAttribute('title') === '清除搜索',
      document.getElementById('recentSearchClear').getAttribute('title'));

// (b) JS-rendered empty states localized to zh
globalThis.__seedRecent([{ path: '/home/user/chatui', count: 3 }]);
_filterRecentProjects('zzzznope');               // no match → pm.recentNoMatch
check('nomatch_zh', listEl.textContent.trim() === '无匹配项目', listEl.textContent.trim());

globalThis.__seedRecent([]);
_filterRecentProjects('');                        // empty list, filter reset → pm.recentEmpty
check('empty_zh', listEl.textContent.trim() === '暂无最近项目', listEl.textContent.trim());

// (c) no raw key leaked as VISIBLE TEXT (t() fell back to the key). Check
//     textContent, not innerHTML — the data-i18n* ATTRIBUTES legitimately
//     contain the key string and must not false-positive.
check('no_raw_key', !document.body.textContent.includes('pm.recent'), 'raw pm.recent key leaked as text');

console.log(out.join('\n'));
"""

_I18N_EXPECTED = (
    'label_zh', 'placeholder_zh', 'clear_all_title_zh', 'clear_search_title_zh',
    'nomatch_zh', 'empty_zh', 'no_raw_key',
)


def _run_i18n(project_src, i18n_src=None):
    harness = os.path.join(HERE, '_recent_i18n_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_I18N_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, i18n_src or _I18N_SRC, project_src, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_recent_search_i18n_zh():
    """Authoritative zh catalogue → label / placeholder / tooltips / empty
    states all render Chinese, no raw key leaks."""
    output = _run_i18n(_PROJECT_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'i18n zh failures:\n' + output
    for must in _I18N_EXPECTED:
        assert ('PASS ' + must) in output, output


def _nc_i18n(target_file, anchor, replacement, must_fail, must_still_pass):
    """Patch a COPY of target_file, run the i18n harness against it, assert the
    target zh checks flip to FAIL while a control stays PASS, then confirm the
    shipped file is byte-identical."""
    with open(target_file, encoding='utf-8') as f:
        original = f.read()
    assert anchor in original, f'NC anchor not found: {anchor[:70]!r}'
    patched = original.replace(anchor, replacement, 1)
    assert patched != original, 'NC replacement was a no-op'
    copy_path = target_file + '.nc_copy.js'
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        is_i18n = (target_file == _I18N_SRC)
        output = _run_i18n(_PROJECT_SRC, i18n_src=copy_path if is_i18n else None) \
            if is_i18n else _run_i18n(copy_path)
        for m in must_fail:
            assert ('FAIL ' + m) in output, f'NC: expected {m} to FAIL:\n{output}'
        for m in must_still_pass:
            assert ('PASS ' + m) in output, \
                f'NC must be surgical — {m} should still PASS:\n{output}'
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(target_file, encoding='utf-8') as f:
        assert f.read() == original, f'shipped {os.path.basename(target_file)} must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_empty_state_uses_t():
    """Revert the empty-state to a hardcoded English literal → the zh empty
    checks FAIL, while the static-attr localization (separate concern) passes."""
    _nc_i18n(
        _PROJECT_SRC,
        anchor='const msg = q ? _t("pm.recentNoMatch") : _t("pm.recentEmpty");',
        replacement='const msg = q ? "No matching projects" : "No recent projects";',
        must_fail=['nomatch_zh', 'empty_zh'],
        must_still_pass=['label_zh', 'placeholder_zh'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_i18n_keys_defined():
    """Blank the zh value of pm.recentSearchPlaceholder → its placeholder check
    FAILs (renders empty), while the empty-state (separate key) still passes."""
    _nc_i18n(
        _I18N_SRC,
        anchor='"pm.recentSearchPlaceholder": "搜索最近…",',
        replacement='"pm.recentSearchPlaceholder": "",',
        must_fail=['placeholder_zh'],
        must_still_pass=['nomatch_zh', 'empty_zh'],
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
