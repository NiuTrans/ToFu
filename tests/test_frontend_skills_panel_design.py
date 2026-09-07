#!/usr/bin/env python3
"""Guards for the Skills settings-tab redesign (tofu re-skin, 2026-08-04).

The panel's skills-specific CSS was a dark-theme leftover: a #1a1a1a scope
pill, a purple-accented drop zone, a green installed wash, and a #333 footer
hairline — all alien inside the cream tofu settings chrome (owner: "界面太丑,
对齐/风格/重点突出都不行"). The redesign re-skins every skills-* rule to the
tofu tokens (--s-ink / --s-gold / --s-cream / --s-white) and restructures the
header to mirror the MCP store header (title+badges | scope | action | search).

Three layers pinned here:

  1. jsdom render pins — the catalog card renderer emits the featured stamp
     (the catalog sorts featured-first; the reason is now VISIBLE), the
     official/warn/note blocks, and the installed card's gold-language
     classes + action set.
  2. Dark-leftover ratchet — no skill-* rule body in settings.css may
     reference the retired dark tokens again (the exact regression class).
  3. Panel-structure pins — header control order + the new-memory button
     demoted from a fake scope tab to a real .btn-secondary.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
     tests/test_frontend_skills_panel_design.py
"""

from __future__ import annotations

import os
import re

import pytest

from tests._jsdom import run_harness
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SETTINGS_CSS = os.path.join(ROOT, 'static', 'settings.css')
PANEL_HTML = os.path.join(ROOT, 'static', 'settings_panels', 'skills.html')
SKILLS_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'skills', 'panel.ts')

# ── 1. jsdom render pins ─────────────────────────────────────────────

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
    + '<div id="skillsTotalCount"></div>'
    + '<div id="skillsCatalogCount"></div>'
    + '<div id="skillsCategoryBar"></div>'
    + '<div id="skillsCatalogGrid"></div>'
    + '</body>',
  targets: [process.argv[2]],
  globals: {
    t: function (key, vars) {
      var dict = {
        'skills.featured': '推荐',
        'skills.official': '官方',
        'skills.by': '作者：' + (vars && vars.author),
        'skills.reqBins': '需要 ' + (vars && vars.bins),
        'skills.reqEnv': '需要环境变量 ' + (vars && vars.env),
        'skills.repo': '仓库',
        'skills.installBtn': '安装',
        'skills.installedTag': '✓ 已安装',
        'skills.viewFiles': '查看文件',
        'skills.uninstallBtn': '卸载',
      };
      return dict[key] || 'T[' + key + ']';
    },
    Icon: function () { return '<svg class="icon-stub"></svg>'; },
    Api: { skills: {
      catalog: async () => ({ catalog: window._testCatalog }),
      list: async () => ({ skills: [] }),
      envStatus: async () => ({ env: [] }),
      catalogSearch: async () => ({ catalog: [], online: {} }),
    }},
  },
});

(async () => {
  try {
    // ── Card A: featured + note + requirements + install CTA ──
    window._testCatalog = [{
      id: 'flyai', name: '飞猪 FlyAI（出行旅游）', author: 'Alibaba Fliggy',
      description: '阿里飞猪官方出行 skill',
      install_note: '八个搜索命令零配置即可用',
      requires: { bins: ['node'], env: ['FLYAI_API_KEY'] },
      homepage: 'https://example.com/repo',
      featured: true, installed: false,
    }];
    await window._populateSkillsTab();
    const a = document.getElementById('skillsCatalogGrid').innerHTML;
    check('featured_stamp', a.indexOf('skill-badge-featured') !== -1 && a.indexOf('推荐') !== -1);
    check('warn_bins', a.indexOf('skill-badge-warn') !== -1 && a.indexOf('需要 node') !== -1);
    check('warn_env', a.indexOf('需要环境变量 FLYAI_API_KEY') !== -1);
    check('note_rendered', a.indexOf('mcp-app-note') !== -1 && a.indexOf('八个搜索命令零配置即可用') !== -1);
    check('install_cta_primary', a.indexOf('btn btn-primary btn-xs') !== -1
      && a.indexOf('data-skills-action="install"') !== -1);
    check('repo_link', a.indexOf('mcp-app-repo') !== -1 && a.indexOf('https://example.com/repo') !== -1);
    check('no_installed_tag_when_absent', a.indexOf('skill-installed-tag') === -1);
    check('footer_axis', a.indexOf('skill-card-footer') !== -1 && a.indexOf('skill-card-actions') !== -1);

    // ── Card B: anthropic author → official badge; installed → gold language ──
    window._testCatalog = [{
      id: 'xlsx', name: 'Excel (xlsx)', author: 'Anthropic',
      description: 'Read and write Excel workbooks',
      featured: false, installed: true, installed_memory_id: 'xlsx-pkg',
    }];
    await window._populateSkillsTab();
    const b = document.getElementById('skillsCatalogGrid').innerHTML;
    check('official_badge', b.indexOf('skill-badge-official') !== -1 && b.indexOf('官方') !== -1);
    check('no_featured_when_false', b.indexOf('skill-badge-featured') === -1);
    check('installed_card_class', b.indexOf('skill-card is-installed') !== -1);
    check('installed_tag', b.indexOf('skill-installed-tag') !== -1 && b.indexOf('✓ 已安装') !== -1);
    check('installed_actions', b.indexOf('data-skills-action="files"') !== -1
      && b.indexOf('data-skills-action="uninstall"') !== -1
      && b.indexOf('data-skill-id="xlsx-pkg"') !== -1);
    check('installed_no_primary_cta', b.indexOf('btn-primary') === -1);
    report();
  } catch (e) {
    check('harness_threw: ' + (e && e.message), false);
    report();
  }
})();
'''


def test_skills_card_render_design_pins():
    with compiled_typescript(
        SKILLS_TS, expose_feature_registry_to_window=True,
    ) as built:
        run_harness(
            target_js=built,
            body_js=_BODY,
            min_pass=14,
            label='skills-panel-design',
        )


# ── 2. Dark-leftover ratchet on the skills CSS ───────────────────────

# Tokens of the retired dark skin that must never reappear inside a skill
# rule body (the whole point of the redesign).
_DARK_TOKENS = (
    '--bg-tertiary', '--bg-secondary', '--text-secondary', '--text-primary',
    '--border-color', 'rgba(108,100,230', 'rgba(34,197,94', 'rgba(234,179,8',
    'rgba(239,68,68', 'rgba(255,255,255', '#1a1a1a', '#2a2a2a', '#161616',
    '#1e1e1e', '#6c64e6', '#b38b5d', '#d97706', '#d4a518', '#15803d',
)

_RULE_RE = re.compile(r'([^{}]+)\{([^{}]*)\}')


def _skill_rules(css: str) -> list[tuple[str, str]]:
    """(selector, body) pairs for skill-panel rules.

    ``skill`` matches the .skills-*/.skill-* classes; ``.is-installed`` is a
    skills-only card state (the retired green wash lived there) that carries
    no 'skill' substring and must be swept by the same ratchet."""
    out = []
    for sel, body in _RULE_RE.findall(css):
        low = sel.lower()
        if 'skill' in low or '.is-installed' in low:
            out.append((sel.strip(), body))
    return out


def test_no_dark_leftovers_in_skills_css():
    with open(SETTINGS_CSS, encoding='utf-8') as fh:
        css = fh.read()
    rules = _skill_rules(css)
    assert len(rules) >= 25, (
        f'only {len(rules)} skill rules scanned — the ratchet would pass '
        'vacuously if the section went missing'
    )
    offenders = []
    for sel, body in rules:
        for tok in _DARK_TOKENS:
            if tok in body:
                offenders.append(f'{sel} → {tok}')
    assert not offenders, (
        'skill rules still reference the retired dark skin (the redesign '
        'moved every skills-* rule to the tofu tokens):\n  '
        + '\n  '.join(offenders)
    )


def test_skills_css_tofu_anchors_present():
    """Positive pins: the load-bearing tofu accents exist (guards against a
    'delete everything' way of going green)."""
    with open(SETTINGS_CSS, encoding='utf-8') as fh:
        css = fh.read()
    rules = dict()
    for sel, body in _skill_rules(css):
        rules.setdefault(sel, body)

    def _body(sel_part: str) -> str:
        for sel, body in rules.items():
            if sel_part in sel:
                return body
        raise AssertionError(f'no skill rule matching {sel_part!r}')

    assert 'var(--s-gold)' in _body('.skills-scope-tab.active'), (
        'the active scope tab lost its gold offset shadow'
    )
    assert 'var(--s-ink)' in _body('.skills-scope-tab.active'), (
        'the active scope tab is no longer the ink block'
    )
    assert 'var(--s-gold)' in _body('.mcp-app-card.is-installed'), (
        'the installed card lost the gold connected-language shadow'
    )
    assert '--s-gold-glow' in _body('.skills-drop-zone.is-dragging'), (
        'the drag-over state lost its gold fill'
    )
    assert 'var(--s-ink)' in _body('.skill-installed-tag') and \
        'var(--s-gold)' in _body('.skill-installed-tag'), (
        'the installed tag is no longer the ink/gold stamp'
    )


# ── 3. Panel-structure pins ──────────────────────────────────────────

def test_skills_panel_header_structure():
    with open(PANEL_HTML, encoding='utf-8') as fh:
        html = fh.read()

    # Header control order: title block → scope tabs → search. The new-memory
    # button was removed from the skills header (memory creation lives in the
    # Memory tab); it must not return as a fake scope tab.
    i_title = html.find('mcp-store-header-title')
    i_scope = html.find('skills-scope-tabs')
    i_search = html.find('id="skillsSearch"')
    assert -1 < i_title < i_scope < i_search, (
        'skills.html header control order drifted (title | scope | search — '
        'the same order as the MCP store header)'
    )

    # The count badges live inside the title block.
    title_block = html[i_title:i_scope]
    assert 'id="skillsTotalCount"' in title_block and \
        'id="skillsCatalogCount"' in title_block, (
        'the count badges left the title block'
    )

    # The removed new-memory button must not come back as a scope-tab masquerading
    # as a view switch (it used to sit between scope tabs and search).
    assert 'openMemoryCreateForm' not in html, (
        'skills.html re-added the removed new-memory button'
    )

    # The intro paragraph carries the dedicated class (no inline styling).
    assert 'settings-desc skills-intro' in html, (
        'the intro paragraph lost its skills-intro class'
    )

    # The test-pinned static onclicks survive the restructure.
    assert "_skillsSetScope('catalog')" in html and '_skillsFilter(' in html, (
        'skills.html lost the static onclicks the deferred-loader stubs cover'
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
