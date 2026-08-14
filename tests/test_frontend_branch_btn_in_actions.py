"""tests/test_frontend_branch_btn_in_actions.py — guard for unifying the
分支 (branch-add) affordance into the bottom `.message-actions` bar.

WHY
---
The branch-add button used to be a standalone dashed pill (`.branch-add-btn`)
rendered by `renderBranchZone` in its own `.branch-zone`. It is now a unified
`.msg-action-btn` (class `msg-branch-btn`) inside `.message-actions`, sharing
the hover-reveal + styling with Copy/Edit/Translate/…

CHECKS
  A. A non-user (assistant) message → `.message-actions` contains a
     `.msg-branch-btn` whose onclick calls `promptNewBranch(<idx>)`.
  B. A user message → NO `.msg-branch-btn` (branch is an assistant-lane action).
  C. `renderBranchZone` NEVER emits the old `branch-add-btn` class (dead).
  D. styles.css no longer defines a `.branch-add-btn` rule.
  E. The action-bar labels + tooltips are i18n-DRIVEN: with a `t()` that
     returns localized (zh) strings, the Copy button renders the localized
     label/title, NOT the hardcoded English "Copy".
  F. (static) i18n.js defines the `msgAction.*` keys with BOTH zh + en.

NEUTER
  • nc_keep_zone_addbtn: restore the add button in renderBranchZone → C FAILS.
  • nc_hardcode_copy: hardcode the Copy label back to English → E FAILS.
"""

from __future__ import annotations

import os
import json
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

ESCAPE_HTML = runtime_section_path('core/escape_html.js')
SAFE_HTML = runtime_section_path('core/safe_html.js')
CHAT_RENDER = runtime_section_path('ui/chat_render.js')
BRANCH_JS = runtime_section_path('branch.js')
TRANSLATION_MODEL = runtime_section_path('core/translation_model.js')
TRANSLATION_INDICATOR = runtime_section_path('ui/translation_indicator.js')
STYLES = os.path.join(ROOT, 'static', 'styles.css')
ZH_I18N = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales', 'zh.json')
EN_I18N = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales', 'en.json')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[6];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = () => 0;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const _conv = { id: 'c-br', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c-br';
win.getActiveConv = global.getActiveConv = () => _conv;

// Localized (zh) stub for the keys under test — lets check E prove the
// labels are i18n-driven (a hardcoded English label would NOT match these).
const _I18N = {
  'branch.add': '分支',
  'msgAction.copy': '复制', 'msgAction.copyTitle': '复制',
  'msgAction.edit': '编辑', 'msgAction.editTitle': '编辑',
  'msgAction.export': '导出', 'msgAction.exportTitle': '导出为手机屏幕图片',
};
win.t = global.t = (k) => (k in _I18N ? _I18N[k] : k);
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '<div class="ptool-panel">TOOLS</div>';
// No `segments` on the test messages → force the grouped fallback via the
// segment-timeline renderer returning ''.
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';

const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_prefetchConvCosts','_prefetchConvFileChanges',
  '_stampFreshness','buildTurnNav','calcCostCny',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(fs.readFileSync(process.argv[8], 'utf8'));
(0, eval)(fs.readFileSync(process.argv[9], 'utf8'));

// ── branch.js (real / neutered) — defines renderBranchZone ──
let branchSrc = fs.readFileSync(process.argv[5], 'utf8');
const NC = process.argv[7] || '';
if (NC === 'nc_keep_zone_addbtn') {
  branchSrc = branchSrc.replace(
    'if (!pills.length && !panelHtml) {\n    return "";\n  }',
    'if (!pills.length && !panelHtml) {\n    return `<div class="branch-zone"><button class="branch-add-btn" onclick="promptNewBranch(${msgIdx})">${escapeHtml(t("branch.add"))}</button></div>`;\n  }');
}
const _branchApplied = (NC !== 'nc_keep_zone_addbtn') || (branchSrc !== fs.readFileSync(process.argv[5], 'utf8'));
(0, eval)(branchSrc);

// ── chat_render.js (real / neutered) ──
let chatSrc = fs.readFileSync(process.argv[2], 'utf8');
if (NC === 'nc_hardcode_copy') {
  chatSrc = chatSrc.replace(
    "${escapeHtml(_mt('msgAction.copy', 'Copy'))}</button>`;",
    "Copy</button>`;");
}
const _chatApplied = (NC !== 'nc_hardcode_copy') || (chatSrc !== fs.readFileSync(process.argv[2], 'utf8'));
check('nc_pattern_applied', _branchApplied && _chatApplied);
(0, eval)(chatSrc);

if (typeof renderMessage !== 'function' || typeof renderBranchZone !== 'function') {
  console.log('FAIL fn_exposed renderMessage/renderBranchZone missing'); process.exit(0);
}
check('fn_exposed', true);

function parse(html) {
  const d = win.document.createElement('div');
  d.innerHTML = html; return d;
}

// ══ A. assistant message → branch btn inside .message-actions ══
{
  const el = parse(renderMessage({ role: 'assistant', content: 'hi' }, 0));
  const actions = el.querySelector('.message-actions');
  const br = actions && actions.querySelector('.msg-branch-btn');
  const isActionBtn = br && br.classList.contains('msg-action-btn');
  const action = br ? (br.getAttribute('data-tofu-action') || '') : '';
  check('A_branch_btn_in_actions', !!br && !!isActionBtn && action.indexOf('promptNewBranch(_msgElIndex(this))') >= 0);
}

// ══ B. user message → NO branch btn ══
{
  const el = parse(renderMessage({ role: 'user', content: 'hello' }, 1));
  check('B_user_has_no_branch_btn', !el.querySelector('.msg-branch-btn'));
}

// ══ C. renderBranchZone never emits the dead branch-add-btn class ══
{
  const emptyZone = renderBranchZone({ role: 'assistant', content: 'x', branches: [] }, 0, new Set());
  check('C_zone_no_addbtn', String(emptyZone).indexOf('branch-add-btn') < 0);
}

// ══ E. action-bar Copy label + title are i18n-driven (localized, not hardcoded) ══
{
  const el = parse(renderMessage({ role: 'assistant', content: 'hi' }, 0));
  const copyBtn = el.querySelector('.copy-msg-btn');
  const label = copyBtn ? (copyBtn.textContent || '').trim() : '';
  const title = copyBtn ? (copyBtn.getAttribute('title') || '') : '';
  // With the zh stub, a properly-i18n'd button shows '复制' and NEVER 'Copy'.
  check('E_copy_label_localized', !!copyBtn && label === '复制' && label.indexOf('Copy') < 0);
  check('E_copy_title_localized', title === '复制');
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_branch_btn_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, CHAT_RENDER, ESCAPE_HTML, SAFE_HTML, BRANCH_JS,
             ROOT, nc, TRANSLATION_MODEL, TRANSLATION_INDICATOR],
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


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_branch_btn_lives_in_message_actions():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'branch-button unification failures:\n' + output
    for want in ('PASS A_branch_btn_in_actions', 'PASS B_user_has_no_branch_btn',
                 'PASS C_zone_no_addbtn', 'PASS E_copy_label_localized',
                 'PASS E_copy_title_localized'):
        assert want in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_keep_zone_addbtn_regression_is_caught():
    output = _run('nc_keep_zone_addbtn')
    assert 'PASS nc_pattern_applied' in output, f'NC did not apply:\n{output}'
    assert 'FAIL C_zone_no_addbtn' in output, (
        'Restoring the branch-zone add button did NOT fail C:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_hardcode_copy_regression_is_caught():
    output = _run('nc_hardcode_copy')
    assert 'PASS nc_pattern_applied' in output, f'NC did not apply:\n{output}'
    assert 'FAIL E_copy_label_localized' in output, (
        'Hardcoding the Copy label back to English did NOT fail E:\n' + output)


def test_msgaction_i18n_keys_have_zh_and_en():
    """Static guard: every msgAction.* key referenced by chat_render.js is
    defined in i18n.js with BOTH a zh and an en value."""
    with open(ZH_I18N, encoding='utf-8') as f:
        zh = json.load(f)
    with open(EN_I18N, encoding='utf-8') as f:
        en = json.load(f)
    with open(CHAT_RENDER, encoding='utf-8') as f:
        chat_src = f.read()
    keys = set(re.findall(r"_mt\('(msgAction\.[a-zA-Z]+)'", chat_src))
    assert keys, 'no msgAction.* keys found in chat_render.js — did the wiring change?'
    for key in sorted(keys):
        assert key in zh, f'zh.json is missing a definition for {key!r}'
        assert key in en, f'en.json is missing a definition for {key!r}'


def test_styles_no_longer_defines_branch_add_btn():
    with open(STYLES, encoding='utf-8') as f:
        css = f.read()
    assert not re.search(r'(?<![.\w-])\.branch-add-btn[\s{,:]', css), (
        'styles.css still defines a `.branch-add-btn` rule — should be removed '
        'now that the button is a `.msg-branch-btn` in `.message-actions`.')


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_branch_btn_lives_in_message_actions()
        test_nc_keep_zone_addbtn_regression_is_caught()
        test_nc_hardcode_copy_regression_is_caught()
    test_msgaction_i18n_keys_have_zh_and_en()
    test_styles_no_longer_defines_branch_add_btn()
    print('PASS test_frontend_branch_btn_in_actions')
