"""tests/test_knowledge_search_panel.py — guards for the KB search-results panel.

INCIDENT (2026-08-14, real deployment screenshot)
-------------------------------------------------
Two defects shipped in the workbench redesign, both visible in ONE screenshot:

1. BROKEN THUMBNAILS. ``_knowledgeRenderSearch`` placed the server-returned
   ROOT-RELATIVE asset URLs (``/api/v1/knowledge/assets/<id>``) verbatim into
   ``<img src>`` / ``<a href>``. Every ``fetch()`` goes through ``apiUrl()``,
   which prefixes the deployment base (``BASE_PATH`` from
   ``location.pathname``), but DOM attributes do not — under a path-prefix
   gateway (the owner's VS Code ``/proxy/<port>/`` tunnel, or any reverse
   proxy subpath) the browser resolved them against the origin ROOT, the
   gateway refused them, and every thumbnail rendered as a broken image whose
   alt text exploded over the card. The origin never even logged the request,
   so it looked like a backend asset bug while the backend was innocent.

2. RESULT CARDS PAINTING THROUGH THE FOOTER. ``.kb-search-results`` carried
   ``flex: 1`` (= ``flex: 1 1 0%``) inside the scrollable column
   ``.kb-search-panel``. With three long excerpts the box shrank below its
   content height; the overflowing cards painted over ``.kb-panel-foot``
   (which has no background), producing text-on-text soup.

CHECKS
  A. (jsdom) ``_knowledgeAssetUrl`` prefixes the deployment base when the page
     lives under a path prefix (``/proxy/15000/``), leaves URLs unchanged at
     the root, passes absolute/empty values through, and prefers the
     ``apiUrl()`` seam when it is in scope.
  B. (source) the render wraps BOTH ``asset.url`` and ``asset.thumbnail_url``
     in ``_knowledgeAssetUrl(...)`` — the wiring, not just the helper.
  C. (source) ``.kb-search-results`` must not reintroduce the shrinkable
     one-token ``flex: 1`` — the footer-overlap regression.

NEUTERS
  • nc_passthrough — helper returns the URL untouched → A FAILS.
  • nc_raw_render  — render emits the raw server URL → B FAILS.
  • nc_shrink      — ``flex: 1`` replaces ``flex: 1 0 auto`` → C FAILS.
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
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

KNOWLEDGE = runtime_section_path('knowledge.js')
STYLES = os.path.join(ROOT, 'static', 'styles.css')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SECTION = process.argv[2];
const ROOT = process.argv[3];
const NC = process.argv[4] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const THUMB = '/api/v1/knowledge/assets/abc123?thumbnail=1';
const FULL = '/api/v1/knowledge/assets/abc123';
const ABS = 'https://cdn.example.test/x.png';

let src = fs.readFileSync(SECTION, 'utf8');
if (NC === 'nc_passthrough') {
  const anchor = "  var value = String(url || '');\n  if (value.charAt(0) !== '/') return value;";
  src = src.replace(anchor, "  var value = String(url || '');\n  return value;");
}
check('nc_pattern_applied', NC !== 'nc_passthrough' || src.indexOf('nc_passthrough') < 0 &&
  src.indexOf("if (value.charAt(0) !== '/') return value;") < 0);

function runCase(label, pageUrl, withApiUrl) {
  const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: pageUrl });
  global.window = dom.window;
  global.document = dom.window.document;
  if (withApiUrl) { global.apiUrl = (p) => '/BASE' + p; }
  else { delete global.apiUrl; }
  (0, eval)(src);
  return {
    thumb: _knowledgeAssetUrl(THUMB),
    full: _knowledgeAssetUrl(FULL),
    absolute: _knowledgeAssetUrl(ABS),
    empty: _knowledgeAssetUrl(''),
  };
}

// A. page under a path-prefix gateway, no apiUrl in scope → fallback base.
const proxy = runCase('proxy', 'http://localhost/proxy/15000/', false);
check('A_proxy_thumb_prefixed', proxy.thumb === '/proxy/15000' + THUMB);
check('A_proxy_full_prefixed', proxy.full === '/proxy/15000' + FULL);
check('A_proxy_absolute_passthrough', proxy.absolute === ABS);
check('A_proxy_empty_passthrough', proxy.empty === '');

// B. page at the deployment root → URLs stay root-relative (no '//' bug).
const root = runCase('root', 'http://localhost/', false);
check('A_root_thumb_unchanged', root.thumb === THUMB);
check('A_root_full_unchanged', root.full === FULL);

// C. the apiUrl() seam, when in scope, wins over the pathname fallback.
const seam = runCase('seam', 'http://localhost/proxy/15000/', true);
check('A_seam_thumb_uses_apiUrl', seam.thumb === '/BASE' + THUMB);
check('A_seam_full_uses_apiUrl', seam.full === '/BASE' + FULL);

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_kb_asset_url_harness_{nc or "main"}.js')
    with open(harness, 'w') as handle:
        handle.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, KNOWLEDGE, ROOT, nc],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


def _section() -> str:
    with open(KNOWLEDGE, encoding='utf-8') as handle:
        return handle.read()


# ── A. behavior under a path-prefix gateway ───────────────────────────────

@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_asset_urls_follow_the_deployment_base():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'knowledge asset URL resolution failures:\n' + output
    for want in ('PASS A_proxy_thumb_prefixed', 'PASS A_proxy_full_prefixed',
                 'PASS A_proxy_absolute_passthrough', 'PASS A_root_thumb_unchanged',
                 'PASS A_seam_thumb_uses_apiUrl'):
        assert want in output, output


# ── B. the render actually uses the resolver (both attributes) ───────────

def test_render_wraps_both_asset_urls():
    src = _section()
    for raw in ('asset.thumbnail_url', 'asset.url'):
        wrapped = f'_knowledgeEsc(_knowledgeAssetUrl({raw}))'
        assert wrapped in src, (
            f'_knowledgeRenderSearch must resolve {raw} through '
            '_knowledgeAssetUrl(...) before it lands in the DOM — a bare '
            'server URL breaks every thumbnail under a path-prefix gateway')


# ── C. the footer-overlap layout fix stays ────────────────────────────────

def _results_rule(css: str) -> str:
    match = re.search(r'\.kb-search-results\s*\{([^}]*)\}', css)
    assert match, '.kb-search-results rule not found in styles.css'
    return match.group(1)


def test_search_results_box_never_shrinks_below_its_content():
    body = _results_rule(open(STYLES, encoding='utf-8').read())
    flex = re.search(r'flex\s*:\s*([^;]+);', body)
    assert flex, '.kb-search-results carries no flex declaration'
    assert re.fullmatch(r'1\s+0\s+auto', flex.group(1).strip()), (
        f".kb-search-results flex must be '1 0 auto' (grow into spare space, "
        f"never shrink below content height) — got {flex.group(1)!r}; the "
        "one-token 'flex: 1' let result cards paint through the panel footer")


# ── Neuters ───────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_passthrough_helper_is_caught():
    output = _run('nc_passthrough')
    assert 'PASS nc_pattern_applied' in output, f'NC did not apply:\n{output}'
    assert 'FAIL A_proxy_thumb_prefixed' in output, (
        'A pass-through _knowledgeAssetUrl did NOT fail the proxy checks:\n'
        + output)


def test_NC_unwrapped_render_is_caught():
    src = _section()
    poisoned = src.replace(
        '_knowledgeEsc(_knowledgeAssetUrl(asset.thumbnail_url))',
        '_knowledgeEsc(asset.thumbnail_url)')
    assert poisoned != src, 'neuter did not apply — re-anchor it'
    assert '_knowledgeEsc(_knowledgeAssetUrl(asset.thumbnail_url))' not in poisoned, (
        'NEUTER must leak: an unwrapped thumbnail_url must fail guard B')


def test_NC_shrinkable_flex_is_caught():
    css = open(STYLES, encoding='utf-8').read()
    poisoned = re.sub(
        r'(\.kb-search-results\s*\{[^}]*?)flex\s*:\s*1\s+0\s+auto\s*;',
        r'\1flex: 1;', css)
    assert poisoned != css, 'neuter did not apply — re-anchor it'
    body = _results_rule(poisoned)
    flex = re.search(r'flex\s*:\s*([^;]+);', body)
    assert not re.fullmatch(r'1\s+0\s+auto', flex.group(1).strip()), (
        "NEUTER must leak: shrinking back to 'flex: 1' must fail guard C")


if __name__ == '__main__':
    if _node_deps_available():
        test_asset_urls_follow_the_deployment_base()
        test_NC_passthrough_helper_is_caught()
    else:
        print('SKIP node+jsdom — behavioral guards')
    test_render_wraps_both_asset_urls()
    test_search_results_box_never_shrinks_below_its_content()
    test_NC_unwrapped_render_is_caught()
    test_NC_shrinkable_flex_is_caught()
    print('PASS test_knowledge_search_panel')
