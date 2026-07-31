"""jsdom regression: the autopilot VU SETTLED bubble is a VERBATIM projection
of the ONE backend-authoritative record (``ev.vuMessage``) — never a
frontend-reconstructed value that falls back to the local stream buffer.

WHY
---
Directive (2026-07-02, see .tofu/memories/separation-of-concerns-directive.md):
a chat-inner bubble that has SETTLED must render from a single backend record,
verbatim. The prior `autopilot_vu_done` code reconstructed the settled message:

    entry.msg.content = finalMsg.content || (buf && buf.content) || entry.msg.content || "";

That `|| buf.content` chain makes the frontend-accumulated buffer a SECOND
source of truth. It bites precisely when the backend record's `content` is
FALSY — i.e. a LEGITIMATELY EMPTY VU "keep going" reply (backend commits
content='' and emits DONE; it bails to VU_CANCEL, not DONE, only when the VU
produced *nothing to persist*). With the fallback, an empty authoritative reply
was silently overwritten by whatever stale text had accumulated in the live
buffer — a stuck/wrong bubble that cannot be diagnosed from one place.

This suite drives the REAL shipped `_handleAutopilotVuEvent` (streaming_render.js)
under jsdom through the real settle path: vu_start → a content delta (fills the
buffer with STALE text + a stale tool round) → vu_done carrying an authoritative
record whose content is EMPTY and whose toolRounds is []. It asserts the settled
message equals the backend record VERBATIM (empty content, zero rounds), not the
stale buffer.

NC is a genuine BYTE-REVERT: the test reconstructs the old `|| buf.content`
fallback lines into a temp copy of streaming_render.js and proves the same
assertions FAIL against it — so reintroducing the fallback can't silently
regress. Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SRC = os.path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js')

# The exact lines the fix introduced (settled = verbatim projection).
_FIX_CONTENT_LINE = '      entry.msg.content = finalMsg.content || "";'
_FIX_ROUNDS_LINE = (
    '      entry.msg.toolRounds = Array.isArray(finalMsg.toolRounds) '
    '? finalMsg.toolRounds : [];'
)

# The old fallback block that reintroduces the dual source of truth. The NC
# rewrites the fix lines back into this — a faithful byte-revert of the edit.
_OLD_CONTENT_BLOCK = (
    '      const buf = (typeof streamBufs !== "undefined") ? streamBufs.get(convId) : null;\n'
    '      entry.msg.content = finalMsg.content || (buf && buf.content) || entry.msg.content || "";'
)
_OLD_ROUNDS_BLOCK = (
    '      if (Array.isArray(finalMsg.toolRounds) && finalMsg.toolRounds.length) {\n'
    '        entry.msg.toolRounds = finalMsg.toolRounds;\n'
    '      } else if (buf && Array.isArray(buf.toolRounds) && buf.toolRounds.length) {\n'
    '        entry.msg.toolRounds = buf.toolRounds;\n'
    '      }'
)


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# The harness takes the path to the streaming_render.js variant to eval as the
# 3rd argv (so the NC can point it at a byte-reverted temp copy).
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const RENDER_SRC = process.argv[3];   // path to the streaming_render.js variant
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

const _I18N = { 'autopilot.warming': 'Autopilot…' };
win.t = global.t = (k) => (k in _I18N ? _I18N[k] : k);
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.formatClockTime = global.formatClockTime = () => '12:00';
win.raw = global.raw = (s) => ({ _raw: String(s), toString() { return String(s); } });
win.safeHtml = global.safeHtml = function (strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => {
    out += s;
    if (i < vals.length) {
      const v = vals[i];
      out += (v && v._raw !== undefined) ? v._raw : win.escapeHtml(v);
    }
  });
  return { toString() { return out; } };
};
win.renderMarkdown = global.renderMarkdown = (s) => String(s == null ? '' : s);

win.activeConvId = global.activeConvId = 'C1';
win.conversations = global.conversations = [{ id: 'C1', messages: [] }];

// Faithful synchronous streaming substrate (mirrors health_stream_timer.js
// twStart/twUpdate/twStop minus rAF batching).
const streamBufs = new Map();
win.streamBufs = global.streamBufs = streamBufs;
win.twStart = global.twStart = (cid) => {
  streamBufs.set(cid, { content: '', thinking: '', toolRounds: [], phase: null });
};
// twUpdate is a no-op here: this suite asserts the SETTLED in-memory message
// state (conv.messages via _findVuMsgById), which is produced entirely inside
// _handleAutopilotVuEvent — NOT the DOM. Skipping the real updateStreamingUI
// (and thus streaming_ui.js + its tool-round renderer deps) keeps the harness
// focused on the verbatim-vs-buffer logic under test. The buffer + vuMsg still
// accumulate the stale transient state (that's what the NC must bite).
win.twUpdate = global.twUpdate = (_cid) => {};
win.twStop = global.twStop = (cid) => { streamBufs.delete(cid); };

const _noop = () => {};
const _noopStr = () => '';
for (const [name, fn] of [
  ['scrollToBottom', _noop], ['isNearBottom', () => false],
  ['buildTurnNav', _noop], ['saveConversations', _noop],
  ['convAutoTranslate', () => false], ['_isRoundSwarm', () => false],
  ['_stampFreshness', _noop], ['renderMcpLoginHintHtml', _noopStr],
  ['renderTurnProvenanceHtml', _noopStr], ['renderPreferenceLearnedHtml', _noopStr],
  ['_buildSwarmInboxChipsHTML', _noopStr], ['_fcFingerprint', () => 0],
  ['_renderFileChangesHtml', _noopStr],
  ['_extractFileChangesFromRoundsAsync', () => Promise.resolve([])],
  ['normalizeErrorEnvelope', (x) => x],
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = fn; }
}
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<svg id="critic-avatar"></svg>';
/* The reworked _streamingBubbleHTML resolves avatars via Icon() and
 * _beginVuStreaming creates the bubble via ConvView.startStreaming (step-4:
 * no fallbacks). This suite asserts the IN-MEMORY settled state, so a no-op
 * seam stub + an Icon stub suffice — the DOM is never asserted here. */
win.Icon = global.Icon = (n) => '<svg data-icon="' + n + '"></svg>';
win.ConvView = global.ConvView = { startStreaming: () => {}, finalizeStreaming: () => {} };

// Load ONLY the (possibly byte-reverted) streaming_render.js. streaming_ui.js
// is deliberately NOT loaded — twUpdate is stubbed to a no-op above, so the DOM
// tool-round renderer never runs; the settled-state assertions read the
// in-memory message that _handleAutopilotVuEvent mutates.
eval(fs.readFileSync(RENDER_SRC, 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _handleAutopilotVuEvent !== 'function') {
  console.log('FAIL fn_exposed _handleAutopilotVuEvent missing'); process.exit(0);
}
check('fn_exposed', true);

const conv = conversations.find(c => c.id === 'C1');
const VU = 'vu-verbatim-1';

// 1) vu_start → stand up the bubble.
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_start', vuMsgId: VU });

// 2) A content delta + a tool_start accumulate STALE state into the live
//    buffer AND the in-memory vuMsg (the transient in-flight paint — allowed).
_handleAutopilotVuEvent('C1', {
  type: 'autopilot_vu_event', vuMsgId: VU,
  inner: { type: 'delta', content: 'STALE-BUFFER-LEFTOVER-TEXT' },
});
_handleAutopilotVuEvent('C1', {
  type: 'autopilot_vu_event', vuMsgId: VU,
  inner: { type: 'tool_start', roundNum: 1, toolName: 'web_search', query: 'stale' },
});

// Sanity: the transient buffer really did accumulate the stale state (so the
// verbatim assertions below are meaningful, not vacuous).
const midEntry = _findVuMsgById(conv, VU);
check('transient_buffer_accumulated_stale',
  !!midEntry && midEntry.msg.content === 'STALE-BUFFER-LEFTOVER-TEXT'
  && (midEntry.msg.toolRounds || []).length === 1);

// 3) vu_done carrying the AUTHORITATIVE backend record: a LEGITIMATELY EMPTY
//    "keep going" VU reply (content='' — backend committed this to the DB and
//    emitted DONE; it would only VU_CANCEL if there were nothing to persist).
//    The settled bubble MUST project this verbatim: empty content, zero rounds.
_handleAutopilotVuEvent('C1', {
  type: 'autopilot_vu_done', vuMsgId: VU,
  vuMessage: { role: 'user', content: '', toolRounds: [], _isVirtualUser: true, _msgId: VU },
});

const finalEntry = _findVuMsgById(conv, VU);
check('settled_entry_exists', !!finalEntry);
// ★ The biting assertions: settled state == backend record VERBATIM, NOT buffer.
check('settled_content_is_empty_verbatim',
  !!finalEntry && finalEntry.msg.content === '');
check('settled_content_not_stale_buffer',
  !!finalEntry && finalEntry.msg.content !== 'STALE-BUFFER-LEFTOVER-TEXT');
check('settled_toolrounds_empty_verbatim',
  !!finalEntry && Array.isArray(finalEntry.msg.toolRounds)
  && finalEntry.msg.toolRounds.length === 0);
check('settled_streamingvu_cleared',
  !!finalEntry && finalEntry.msg._streamingVu === undefined);

console.log(out.join('\n'));
"""


def _run(render_src: str) -> str:
    harness = os.path.join(HERE, '_autopilot_vu_verbatim_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, render_src],
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


def _status(output: str, name: str) -> str | None:
    for ln in output.splitlines():
        if ln.endswith(' ' + name):
            return ln.split(' ', 1)[0]
    return None


def _build_reverted_copy() -> str:
    """Produce a temp streaming_render.js with the fix byte-reverted to the old
    ``|| buf.content`` fallback. Returns the temp file path."""
    src = open(SRC, encoding='utf-8').read()
    assert _FIX_CONTENT_LINE in src, (
        'fix content line not found — did the source change? Update this test.')
    assert _FIX_ROUNDS_LINE in src, (
        'fix toolRounds line not found — did the source change? Update this test.')
    reverted = src.replace(_FIX_CONTENT_LINE, _OLD_CONTENT_BLOCK)
    reverted = reverted.replace(_FIX_ROUNDS_LINE, _OLD_ROUNDS_BLOCK)
    assert reverted != src and '|| (buf && buf.content) ||' in reverted
    dst = os.path.join(HERE, '_streaming_render_reverted.js')
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(reverted)
    return dst


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_vu_settled_render_is_verbatim_backend_record():
    """The shipped code renders the settled VU bubble verbatim from ev.vuMessage
    — an empty authoritative reply stays empty; stale buffer text is discarded."""
    output = _run(SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'VU-verbatim failures:\n' + output
    # fn_exposed + transient_buffer_accumulated_stale + settled_entry_exists
    # + 4 biting/verbatim assertions = 7
    assert output.count('PASS') >= 7, f'expected >=7 PASS, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_byte_revert_fallback_fails():
    """Byte-revert NC: reintroducing the `|| buf.content` fallback makes the
    empty-authoritative-reply project the STALE buffer instead — the verbatim
    assertions MUST fail. Proves the principle can't silently regress."""
    dst = _build_reverted_copy()
    try:
        output = _run(dst)
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
    # The transient sanity check still passes (buffer accumulation is unchanged),
    # proving the harness is sound…
    assert _status(output, 'transient_buffer_accumulated_stale') == 'PASS', \
        'NC harness precondition broke:\n' + output
    # …but the settled-state verbatim assertions MUST fail under the fallback.
    assert _status(output, 'settled_content_is_empty_verbatim') == 'FAIL', \
        'NC should overwrite empty backend content with stale buffer:\n' + output
    assert _status(output, 'settled_content_not_stale_buffer') == 'FAIL', \
        'NC should show the stale buffer text:\n' + output
    assert _status(output, 'settled_toolrounds_empty_verbatim') == 'FAIL', \
        'NC should keep the stale buffer tool round:\n' + output
