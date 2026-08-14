"""tests/test_frontend_initiator_attribution.py — auto-initiated turns must be
visually ATTRIBUTABLE, never wear the plain human "You" / bare "Agent" identity.

WHY
---
Many backend paths inject a turn WITHOUT a human typing (Project Brain dispatch,
proactive scheduler, timer continuation, swarm auto-continuation). The backend
now stamps `_initiator` (lib/conversations/turn_initiation.py); the frontend
resolves it through the shared INITIATOR_REGISTRY (static/js/settings/branding.js)
and `renderMessage` (chat_render.js) consumes it. BEFORE the fix a `_proactive` /
`_timer` / `_brainDispatch` user-lane turn fell straight through to the onigiri
"You" avatar + label, and a `_swarmAutoContinue` assistant turn showed the plain
"Agent" — indistinguishable from a human-initiated turn.

This harness evals the REAL shipped escape_html.js + safe_html.js + branding.js +
chat_render.js and drives `renderMessage(msg)` for each auto-initiator, asserting:
  * user-lane proactive/timer/brain → the registry label (NOT "You") + a non-
    onigiri avatar;
  * swarm assistant turn → the "Auto-continued" label (NOT "Agent") + a badge;
  * legacy-boolean-only messages resolve identically (one-directional migration);
  * NO REGRESSION: a plain human turn is still onigiri + "You".

SOURCE-LEVEL NEUTER (mutated copy; shipped files untouched): stub
`_initiatorPresentation` to always return null → every auto-initiator falls back
to "You"/"Agent" (the bug reproduces), proving the consumption is load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
BRANDING = os.path.join(JS_DIR, 'settings', 'branding.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# The consumption arms must be present in shipped source or the test is stale.
_USER_ARM = "(_userInit && _userInit.lane === 'user')"
_ASSISTANT_ARM = "if (_ip && _ip.lane === 'assistant')"


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// EN labels for the initiator keys (mirrors i18n.js); echo other keys back so
// _tOr's `t(k) !== k` guard treats real keys as present.
const I18N = {
  'initiator.proactive': 'Proactive Agent',
  'initiator.timer': 'Timer',
  'initiator.brain': 'Project Brain',
  'initiator.swarm': 'Auto-continued',
};
global.t = (k) => (k in I18N ? I18N[k] : k);

// BASE_PATH so branding.js's _ICON_BASE builds; sentinel mascots so the
// non-fallback avatar path is exercised for the human/agent baselines.
global.BASE_PATH = '';
global._USER_AVATAR_SVG = '<img alt="You" data-avatar="onigiri">';
global._TOFU_WORKER_SVG = '<img alt="Worker" data-avatar="worker">';
global._TOFU_PLANNER_SVG = '<img alt="Planner" data-avatar="planner">';
global._TOFU_CRITIC_SVG = '<img alt="Critic" data-avatar="critic">';

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
// No `segments` on the test messages → the interleaved timeline no-ops and the
// render stays on the simple grouped path (we only assert header identity).
global._renderSegmentedTimeline = () => '';
global.renderToolRoundsGrouped = () => '';
global.renderThinkingBlock = () => '';
// Assistant-lane body helpers (we assert only header identity + badge, not body).
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
  (0, eval)(fs.readFileSync(process.argv[2].replace('escape_html.js', 'translation_model.js'), 'utf8'));  // core/translation_model.js (chat_render dep)
  (0, eval)(fs.readFileSync(process.argv[2].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8'));  // ui/translation_indicator.js (chat_render dep)
  (0, eval)(chatRenderSrc);                             // chat_render.js
}

function roleLabel(html) {
  const m = html.match(/<span class="message-role">([^<]*)<\/span>/);
  return m ? m[1] : null;
}
function avatarKind(html) {
  const m = html.match(/<div class="message-avatar">(.*?)<\/div>/s);
  if (!m) return null;
  const inner = m[1];
  if (inner.indexOf('data-avatar="onigiri"') !== -1) return 'onigiri';
  if (inner.indexOf('data-avatar="worker"') !== -1) return 'worker';
  if (inner.indexOf('<svg') !== -1) return 'glyph';
  return 'other';
}

(async () => {
  const CHAT = fs.readFileSync(process.argv[4], 'utf8');
  const BRAND = fs.readFileSync(process.argv[5], 'utf8');
  loadAll(CHAT, BRAND);
  if (typeof renderMessage !== 'function') {
    console.log('FAIL fn_exposed renderMessage missing'); process.exit(0);
  }
  check('fn_exposed', true);
  // INITIATOR_REGISTRY is a top-level `const` — not a hoisted global under the
  // (0,eval) function scope — so assert the registry indirectly via the
  // function-declared (hoisted) helpers that read it.
  check('resolver_exposed', typeof _resolveInitiator === 'function');
  check('presentation_exposed', typeof _initiatorPresentation === 'function');
  check('registry_via_presentation',
        (_initiatorPresentation({ _initiator: 'proactive' }) || {}).label === 'Proactive Agent');

  // ══ user-lane auto-initiators (via _initiator) → registry label + glyph ══
  const USER_CASES = [
    ['proactive', 'Proactive Agent'],
    ['timer', 'Timer'],
    ['brain', 'Project Brain'],
  ];
  for (const [init, label] of USER_CASES) {
    const html = renderMessage({ role: 'user', content: 'auto', _initiator: init });
    check(init + '_label_is_' + label.replace(/\s/g,''), roleLabel(html) === label);
    check(init + '_label_not_You', roleLabel(html) !== 'You');
    check(init + '_avatar_glyph', avatarKind(html) === 'glyph');
    check(init + '_avatar_not_onigiri', avatarKind(html) !== 'onigiri');
  }

  // ══ legacy-boolean fallback resolves identically (one-directional migration) ══
  {
    const html = renderMessage({ role: 'user', content: 'legacy', _brainDispatch: true });
    check('legacy_brain_label', roleLabel(html) === 'Project Brain');
    check('legacy_brain_not_You', roleLabel(html) !== 'You');
  }
  {
    const html = renderMessage({ role: 'user', content: 'legacy', _proactive: true });
    check('legacy_proactive_label', roleLabel(html) === 'Proactive Agent');
  }

  // ══ swarm assistant-lane turn → "Auto-continued" label + badge (NOT "Agent") ══
  {
    const html = renderMessage({ role: 'assistant', content: 'drained updates',
                                 _initiator: 'swarm' });
    check('swarm_label_is_autocontinued', roleLabel(html) === 'Auto-continued');
    check('swarm_label_not_Agent', roleLabel(html) !== 'Agent');
    check('swarm_badge_present', html.indexOf('init-badge') !== -1
                                 && html.indexOf('init-swarm') !== -1);
  }
  {
    const html = renderMessage({ role: 'assistant', content: 'x', _swarmAutoContinue: true });
    check('legacy_swarm_label', roleLabel(html) === 'Auto-continued');
  }

  // ══ NO REGRESSION: plain human user → onigiri + "You" ══
  {
    const html = renderMessage({ role: 'user', content: 'i am human' });
    check('human_label_is_You', roleLabel(html) === 'You');
    check('human_avatar_onigiri', avatarKind(html) === 'onigiri');
  }
  // ══ NO REGRESSION: plain assistant → "Agent" + worker avatar ══
  {
    const html = renderMessage({ role: 'assistant', content: 'normal reply' });
    check('agent_label_is_Agent', roleLabel(html) === 'Agent');
    check('agent_avatar_worker', avatarKind(html) === 'worker');
  }

  // ══ NEUTER: _initiatorPresentation → null everywhere ⇒ bug reproduces ══
  {
    let neutered = BRAND.replace(
      'function _initiatorPresentation(msg) {',
      'function _initiatorPresentation(msg) { return null;');
    check('neuter_applied', neutered !== BRAND);
    loadAll(CHAT, neutered);
    const pHtml = renderMessage({ role: 'user', content: 'auto', _initiator: 'proactive' });
    check('neuter_proactive_falls_back_to_You', roleLabel(pHtml) === 'You');
    const sHtml = renderMessage({ role: 'assistant', content: 'x', _initiator: 'swarm' });
    check('neuter_swarm_falls_back_to_Agent', roleLabel(sHtml) === 'Agent');
    loadAll(CHAT, BRAND);  // restore
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_initiator_attribution():
    chat_src = open(CHAT_RENDER, encoding='utf-8').read()
    brand_src = open(BRANDING, encoding='utf-8').read()
    assert _USER_ARM in chat_src, 'user-lane initiator arm missing — test stale'
    assert _ASSISTANT_ARM in chat_src, 'assistant-lane initiator arm missing — test stale'
    assert '_initiatorPresentation' in brand_src, 'registry helper missing — test stale'

    harness = os.path.join(HERE, '_initiator_attribution_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ESCAPE_HTML, SAFE_HTML, CHAT_RENDER, BRANDING],
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
    assert not fails, 'initiator-attribution failures:\n' + output
    assert output.count('PASS') >= 24, f'expected >=24 PASS lines, got:\n{output}'
