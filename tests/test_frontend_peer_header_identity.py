"""tests/test_frontend_peer_header_identity.py — regression for peer/operator
KIND_PEER_MSG turns wearing the HUMAN header identity (onigiri avatar + "You").

WHY
---
A message injected by a sibling conversation (project_message) or the project
operator (project_intervene / brain peer-nudge) arrives as a real `role:"user"`
turn — the backend stamps `_peerMessage` / `_fromConv` / `_peerHuman` in
`message_queue.dispatch_next_queued`. In `renderMessage` (chat_render.js) the
avatar + header label USED TO fall through to the plain-user branch:

    userAvatar = ... : _USER_AVATAR_SVG            // onigiri mascot
    userLabel  = ... : "You"

so the header identity was byte-identical to something the human typed — the
in-bubble `.peer-msg-banner` was the ONLY distinction, which is too weak (the
primary signal, avatar+label, was lying). The fix extends the SAME
userAvatar/userLabel branch that already special-cases _isEndpointReview /
_isVirtualUser: a `_peerMessage` turn now gets the people-glyph (mirroring the
project_peer_status tool icon) + a role label — "Operator" (peer.operatorLabel)
when `_peerHuman`, else "Peer" (peer.senderLabel). The specific sender
(conv id / operator) stays in the in-bubble banner.

This harness evals the REAL shipped escape_html.js + safe_html.js +
chat_render.js and drives `renderMessage(msg)` (idx omitted → no action-button
/ fingerprint path, so only the render helpers we stub are needed) with each
message shape.

SOURCE-LEVEL DOUBLE-NEUTER (on a MUTATED copy; shipped file untouched):
  • Remove the `: msg._peerMessage ? _peerAvatar` avatar arm AND the
    `: msg._peerMessage ? (... peer.*Label ...)` label arm → a peer turn once
    again renders the onigiri + "You" (the bug reproduces), proving the branch
    is load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# The two arms we neuter — must be present in the shipped source or the test is
# stale. Kept as module constants so the python side can assert presence too.
_AVATAR_ARM = ': msg._peerMessage\n    ? _peerAvatar'
_LABEL_ARM = ': msg._peerMessage\n    ? (msg._peerHuman ? _tOr("peer.operatorLabel", "Operator") : _tOr("peer.senderLabel", "Peer"))'


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Real i18n labels the fix references (zh UI would use these). We stub t()
//    to return the EN strings for the two peer keys, and echo the key back
//    otherwise (so the _tOr helper's `t(k) !== k` guard treats them as present). ──
const I18N = {
  'peer.senderLabel': 'Peer',
  'peer.operatorLabel': 'Operator',
};
global.t = (k) => (k in I18N ? I18N[k] : k);

// ── Distinct sentinel avatars so we can tell which arm won without shipping
//    the real branding <img> tags. The fix uses `typeof _USER_AVATAR_SVG` /
//    `typeof _TOFU_CRITIC_SVG` guards, so defining them here exercises the
//    real (non-fallback) avatar path. ──
global._USER_AVATAR_SVG = '<img alt="You" data-avatar="onigiri">';
global._TOFU_CRITIC_SVG = '<img alt="Critic" data-avatar="critic">';
global._TOFU_WORKER_SVG = '<img alt="Worker" data-avatar="worker">';
global._TOFU_PLANNER_SVG = '<img alt="Planner" data-avatar="planner">';

// ── Render helpers the isUser+no-idx path touches. ──
global._fmtAbsoluteDateTime = () => '';
global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
global.renderMarkdown = (s) => String(s == null ? '' : s);
// Helpers referenced only inside branches our path never enters, but must
// exist as names so eval of the whole file doesn't ReferenceError at call.
global.renderMcpLoginHintHtml = () => '';
global.renderTurnProvenanceHtml = () => '';
global.renderFileChangesBar = () => '';
global.renderErrorEnvelope = () => '';
global.renderBranchZone = () => '';
global.renderTurnCtxNote = () => '';
global.getActiveConv = () => null;
global.activeStreams = new Set();
global.getToolRoundsFromMsg = () => [];

function loadAll(chatRenderSrc) {
  // escape_html + safe_html are REAL; chat_render is the (possibly neutered) arg.
  (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // escape_html.js
  (0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // safe_html.js
  (0, eval)(chatRenderSrc);                             // chat_render.js
}

// Extract header identity from rendered HTML.
function roleLabel(html) {
  const m = html.match(/<span class="message-role">([^<]*)<\/span>/);
  return m ? m[1] : null;
}
function avatarKind(html) {
  const m = html.match(/<div class="message-avatar">(.*?)<\/div>/s);
  if (!m) return null;
  const inner = m[1];
  if (inner.indexOf('data-avatar="onigiri"') !== -1) return 'onigiri';
  if (inner.indexOf('data-avatar="critic"') !== -1) return 'critic';
  // The people-glyph the fix reuses (project_peer_status icon) has 2 <circle>-less
  // person paths + a circle; detect its signature stroke-linejoin markup.
  if (inner.indexOf('M23 21v-2a4 4 0 0 0-3-3.87') !== -1) return 'people';
  return 'other';
}

(async () => {
  const CHAT = fs.readFileSync(process.argv[4], 'utf8');
  loadAll(CHAT);
  if (typeof renderMessage !== 'function') {
    console.log('FAIL fn_exposed renderMessage missing'); process.exit(0);
  }
  check('fn_exposed', true);

  // ══ 1. Peer-conversation message → people glyph + "Peer" (NOT onigiri/"You") ══
  {
    const html = renderMessage({ role: 'user', content: 'hi from peer',
                                 _peerMessage: true, _fromConv: 'mr78tlad' });
    check('peer_label_is_Peer', roleLabel(html) === 'Peer');
    check('peer_avatar_is_people', avatarKind(html) === 'people');
    check('peer_label_not_You', roleLabel(html) !== 'You');
    check('peer_avatar_not_onigiri', avatarKind(html) !== 'onigiri');
    // In-bubble banner still carries the specific sender.
    check('peer_banner_kept', html.indexOf('peer-msg-banner') !== -1
                              && html.indexOf('mr78tlad') !== -1);
  }

  // ══ 2. Operator nudge (_peerHuman) → people glyph + "Operator" ══
  {
    const html = renderMessage({ role: 'user', content: 'operator says',
                                 _peerMessage: true, _peerHuman: true,
                                 _fromConv: 'mr5wo337' });
    check('operator_label_is_Operator', roleLabel(html) === 'Operator');
    check('operator_avatar_is_people', avatarKind(html) === 'people');
    check('operator_banner_operator_variant',
          html.indexOf('peer-msg-banner-operator') !== -1);
  }

  // ══ 3. NO REGRESSION: a plain human message → onigiri + "You" ══
  {
    const html = renderMessage({ role: 'user', content: 'i am the human' });
    check('human_label_is_You', roleLabel(html) === 'You');
    check('human_avatar_is_onigiri', avatarKind(html) === 'onigiri');
  }

  // ══ 4. NO REGRESSION: a critic (endpoint review) still → critic avatar/label ══
  {
    const html = renderMessage({ role: 'user', content: 'verdict',
                                 _isEndpointReview: true });
    check('critic_label_is_Critic', roleLabel(html) === 'Critic');
    check('critic_avatar_is_critic', avatarKind(html) === 'critic');
  }

  // ══ 5. DOUBLE-NEUTER: strip BOTH peer arms → the bug reproduces ══
  {
    const AVATAR_ARM = ': msg._peerMessage\n    ? _peerAvatar';
    const LABEL_ARM = ': msg._peerMessage\n    ? (msg._peerHuman ? _tOr("peer.operatorLabel", "Operator") : _tOr("peer.senderLabel", "Peer"))';
    let neutered = CHAT.replace(AVATAR_ARM, '\n    /* NEUTERED peer avatar arm */').replace(LABEL_ARM, '\n    /* NEUTERED peer label arm */');
    check('neuter_avatar_applied', neutered.indexOf(AVATAR_ARM) === -1 && neutered !== CHAT);
    check('neuter_label_applied', neutered.indexOf(LABEL_ARM) === -1);
    loadAll(neutered);
    const html = renderMessage({ role: 'user', content: 'hi from peer',
                                 _peerMessage: true, _fromConv: 'mr78tlad' });
    // With both arms gone, a peer turn falls back to human identity.
    check('neuter_falls_back_to_You', roleLabel(html) === 'You');
    check('neuter_falls_back_to_onigiri', avatarKind(html) === 'onigiri');
    loadAll(CHAT);  // restore the real fn
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_peer_header_identity():
    chat_src = open(CHAT_RENDER, encoding='utf-8').read()
    # Sanity: the two arms we neuter must be present, else the test is stale.
    assert _AVATAR_ARM in chat_src, 'peer avatar arm missing from chat_render.js — test stale'
    assert _LABEL_ARM in chat_src, 'peer label arm missing from chat_render.js — test stale'

    harness = os.path.join(HERE, '_peer_header_identity_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ESCAPE_HTML, SAFE_HTML, CHAT_RENDER],
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
    assert not fails, 'peer-header-identity failures:\n' + output
    assert output.count('PASS') >= 16, f'expected >=16 PASS lines, got:\n{output}'
