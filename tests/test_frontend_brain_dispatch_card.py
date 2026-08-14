"""tests/test_frontend_brain_dispatch_card.py — the brain-dispatch kickoff
renders as a PROVENANCE CARD, not a wall of English (owner ask 2026-08-04).

The defect: a Project-Brain kickoff bubble showed only the raw engine
instructions — the human could not see WHICH epic it was, WHO posted it (a
bare conv id at best), or HOW/WHY the brain routed it here. The backend now
stamps `_brainEpic` (creator conv + title, dispatch seam, routing reason) on
the persisted turn; `renderMessage` renders `_renderBrainDispatchCard(msg)`
and collapses the raw instructions behind a <details>.

This harness evals the REAL shipped escape_html.js + safe_html.js +
branding.js + chat_render.js and drives `renderMessage(msg)` asserting:
  * the card carries the epic title (clickable → openProjectBrain), the
    creator's TITLE as a loadConversation link, method + reason labels;
  * creator == the open conversation → "本对话" plain text, NO link;
  * the answered flag renders its chip;
  * the raw kickoff text is inside <details class="brain-kickoff-raw">;
  * LEGACY kickoffs (no _brainEpic) render NO card (provenance can't be
    fabricated) but the raw wall still collapses — the wall is the same wall;
  * XSS: epic/creator titles are escaped;
  * the header still attributes the turn to the brain (not "You").

SOURCE-LEVEL NEUTER (mutated copy; shipped files untouched): stub
`_renderBrainDispatchCard` to return '' → every card assertion goes red,
proving the consumption is load-bearing.
"""

from __future__ import annotations

import os
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
BRANDING = runtime_section_path('settings/branding.js')
CHAT_RENDER = runtime_section_path('ui/chat_render.js')
TRANSLATION_MODEL = runtime_section_path('core/translation_model.js')
TRANSLATION_INDICATOR = runtime_section_path('ui/translation_indicator.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// zh labels for the card keys (mirrors i18n.js); echo unknown keys back so the
// `t(k) !== k` presence guard treats real keys as present.
const I18N = {
  'initiator.brain': '项目大脑',
  'brain.dispatchTitle': '项目大脑派发',
  'brain.fromLabel': '来自',
  'brain.methodLabel': '方式',
  'brain.reasonLabel': '原因',
  'brain.method.heartbeat': '心跳巡视自动拉起',
  'brain.method.dependency_done': '依赖 Epic 完成后触发',
  'brain.method.posted': 'Epic 发布时立即派发',
  'brain.method.conv_idle': '本对话空闲时立即派发',
  'brain.method.answered': '人工答复后立即重派',
  'brain.reason.creator': '本对话是该 Epic 的创建者',
  'brain.reason.migrated': '原目标会话空闲卡死，自动迁移到本对话',
  'brain.reason.fallback': '创建者未知，派给刚完成依赖的会话',
  'brain.answeredChip': '含人工答复',
  'brain.rawKickoff': '派发指令原文',
  'brain.thisConv': '本对话',
  'brain.untitledConv': '未命名会话',
  'brain.openBoard': '在项目大脑面板中查看',
};
let LANG = 'zh';
global.t = (k) => {
  if (!(k in I18N)) return k;
  if (LANG === 'en') {
    const EN = {
      'brain.dispatchTitle': 'Brain dispatch',
      'brain.reason.migrated': 'Migrated here — the original target was idle-stranded',
      'brain.thisConv': 'this conversation',
      'brain.rawKickoff': 'Raw dispatch instructions',
    };
    return EN[k] || I18N[k];
  }
  return I18N[k];
};

global.BASE_PATH = '';
global._USER_AVATAR_SVG = '<img alt="You" data-avatar="onigiri">';
global._TOFU_WORKER_SVG = '<img alt="Worker" data-avatar="worker">';
global._TOFU_PLANNER_SVG = '<img alt="Planner" data-avatar="planner">';
global._TOFU_CRITIC_SVG = '<img alt="Critic" data-avatar="critic">';
global.activeConvId = 'convVIEWING';
global.loadConversation = () => {};

// Render helpers touched by the render path.
global._fmtAbsoluteDateTime = () => '';
global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
global.renderMarkdown = (s) => String(s == null ? '' : s);
global.renderMcpLoginHintHtml = () => '';
global.renderTurnProvenanceHtml = () => '';
global.renderFileChangesBar = () => '';
global.renderErrorEnvelope = () => '';
global.renderBranchZone = () => '';
global.renderTurnCtxNote = () => '';
global.getActiveConv = () => null;
global.activeStreams = new Set();
global.getToolRoundsFromMsg = () => [];
global._renderSegmentedTimeline = () => '';
global.renderToolRoundsGrouped = () => '';
global.renderThinkingBlock = () => '';
global.renderFinishInfo = () => '';
global.renderPreferenceLearnedHtml = () => '';
global._apRunReportAffordance = () => '';
global._buildApSummaryPanel = () => '';
global.renderToolRounds = () => '';
global._msgFingerprint = () => '';
global.formatClockTime = () => '';
global._fmtRelTime = () => '';

function loadAll(chatRenderSrc, brandingSrc) {
  (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // escape_html.js
  (0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // safe_html.js
  (0, eval)(brandingSrc);                               // branding.js (registry)
  (0, eval)(fs.readFileSync(process.argv[6], 'utf8'));
  (0, eval)(fs.readFileSync(process.argv[7], 'utf8'));
  (0, eval)(chatRenderSrc);                             // chat_render.js
}

const META = {
  epicId: 'pt_53065dbe86bb4286',
  epicTitle: 'MCP 插件工具 per-tool 开关 + schema 瘦身',
  originatorConv: 'convCREATOR',
  originatorTitle: '浏览器工具面 v2 认知减负',
  method: 'heartbeat',
  route: 'creator',
  answered: false,
};
function brainMsg(extra) {
  return Object.assign({
    role: 'user',
    content: '[Project Brain — autonomous dispatch] You are picking up an open project epic …',
    _brainDispatch: true,
    _boardTaskId: META.epicId,
    _brainEpic: META,
  }, extra || {});
}

(async () => {
  const CHAT = fs.readFileSync(process.argv[4], 'utf8');
  const BRAND = fs.readFileSync(process.argv[5], 'utf8');
  loadAll(CHAT, BRAND);
  if (typeof renderMessage !== 'function') {
    console.log('FAIL fn_exposed renderMessage missing'); process.exit(0);
  }
  check('fn_exposed', true);
  check('card_fn_exposed', typeof _renderBrainDispatchCard === 'function');

  // ══ Full card: title / creator link / method / reason / collapse ══
  {
    const html = renderMessage(brainMsg());
    check('card_present', html.indexOf('brain-dispatch-card') !== -1);
    check('card_epic_title', html.indexOf('MCP 插件工具 per-tool 开关 + schema 瘦身') !== -1);
    check('card_epic_opens_brain', html.indexOf('openProjectBrain') !== -1);
    check('card_creator_link', html.indexOf("loadConversation('convCREATOR')") !== -1);
    check('card_creator_title', html.indexOf('浏览器工具面 v2 认知减负') !== -1);
    check('card_no_raw_id_visible', html.indexOf('>convCREATOR<') === -1);
    check('card_method_zh', html.indexOf('心跳巡视自动拉起') !== -1);
    check('card_reason_zh', html.indexOf('本对话是该 Epic 的创建者') !== -1);
    check('card_epic_id_shown', html.indexOf('pt_53065dbe86bb4286') !== -1);
    check('raw_collapsed', html.indexOf('<details class="brain-kickoff-raw">') !== -1);
    check('raw_label_zh', html.indexOf('派发指令原文') !== -1);
    check('no_answered_chip', html.indexOf('bdc-answered') === -1);
    // Header still attributes the turn to the brain, not "You".
    check('header_brain_label', html.indexOf('项目大脑') !== -1);
  }

  // ══ Creator IS the open conversation → 本对话 plain text, NO link ══
  {
    const html = renderMessage(brainMsg({
      _brainEpic: Object.assign({}, META, { originatorConv: 'convVIEWING' }),
    }));
    check('self_this_conv', html.indexOf('本对话') !== -1);
    check('self_no_link', html.indexOf("loadConversation('convVIEWING')") === -1);
  }

  // ══ Migrated route + answered chip + dependency_done method ══
  {
    const html = renderMessage(brainMsg({
      _brainEpic: Object.assign({}, META, {
        method: 'answered', route: 'migrated', answered: true }),
    }));
    check('migrated_reason', html.indexOf('自动迁移到本对话') !== -1);
    check('answered_method', html.indexOf('人工答复后立即重派') !== -1);
    check('answered_chip', html.indexOf('bdc-answered') !== -1
                           && html.indexOf('含人工答复') !== -1);
  }

  // ══ Unknown method token → raw token fallback, no crash ══
  {
    const html = renderMessage(brainMsg({
      _brainEpic: Object.assign({}, META, { method: 'pigeon' }),
    }));
    check('unknown_method_fallback', html.indexOf('>pigeon<') !== -1
                                     || html.indexOf('pigeon') !== -1);
  }

  // ══ LEGACY kickoff (no _brainEpic) → NO card (can't fabricate
  //    provenance) but the raw wall STILL collapses — the wall is identical
  //    whether or not the record exists. ══
  {
    const html = renderMessage({
      role: 'user', content: 'legacy kickoff text', _brainDispatch: true,
    });
    check('legacy_no_card', html.indexOf('brain-dispatch-card') === -1);
    check('legacy_collapsed', html.indexOf('brain-kickoff-raw') !== -1);
    check('legacy_content_shown', html.indexOf('legacy kickoff text') !== -1);
    // … but the header still attributes it (existing initiator arm).
    check('legacy_header_brain', html.indexOf('项目大脑') !== -1);
  }

  // ══ XSS: titles are escaped ══
  {
    const html = renderMessage(brainMsg({
      _brainEpic: Object.assign({}, META, {
        epicTitle: '<img src=x onerror=alert(1)>',
        originatorTitle: '<script>alert(2)</script>',
      }),
    }));
    check('xss_epic_escaped', html.indexOf('<img src=x onerror=alert(1)>') === -1);
    check('xss_epic_entity', html.indexOf('&lt;img src=x') !== -1);
    check('xss_origin_escaped', html.indexOf('<script>alert(2)</script>') === -1);
  }

  // ══ Empty originator title → untitled fallback ══
  {
    const html = renderMessage(brainMsg({
      _brainEpic: Object.assign({}, META, { originatorTitle: '' }),
    }));
    check('untitled_fallback', html.indexOf('未命名会话') !== -1);
  }

  // ══ EN locale: method/reason resolve to English ══
  {
    LANG = 'en';
    const html = renderMessage(brainMsg({
      _brainEpic: Object.assign({}, META, { route: 'migrated' }),
    }));
    check('en_dispatch_title', html.indexOf('Brain dispatch') !== -1);
    check('en_migrated_reason', html.indexOf('idle-stranded') !== -1);
    check('en_raw_label', html.indexOf('Raw dispatch instructions') !== -1);
    LANG = 'zh';
  }

  // ══ NEUTER: _renderBrainDispatchCard → '' ⇒ the card vanishes ══
  {
    const neutered = CHAT.replace(
      'function _renderBrainDispatchCard(msg) {',
      'function _renderBrainDispatchCard(msg) { return \'\';');
    check('neuter_applied', neutered !== CHAT);
    loadAll(neutered, BRAND);
    const html = renderMessage(brainMsg());
    check('neuter_card_gone', html.indexOf('brain-dispatch-card') === -1);
    loadAll(CHAT, BRAND);  // restore
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_brain_dispatch_card():
    chat_src = open(CHAT_RENDER, encoding='utf-8').read()
    assert 'function _renderBrainDispatchCard(msg) {' in chat_src, \
        'card renderer missing — test stale'
    assert 'msg._brainDispatch' in chat_src, 'mount predicate missing — test stale'
    assert 'brain-kickoff-raw' in chat_src, 'collapse mount missing — test stale'

    harness = os.path.join(HERE, '_brain_dispatch_card_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ESCAPE_HTML, SAFE_HTML, CHAT_RENDER, BRANDING,
             TRANSLATION_MODEL, TRANSLATION_INDICATOR],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'brain-dispatch-card failures:\n' + output
    assert output.count('PASS') >= 28, f'expected >=28 PASS lines, got:\n{output}'
