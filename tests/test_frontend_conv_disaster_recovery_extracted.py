"""Wire-parity guards for pt_3879f00e sub-part 2 slice 9 — extract the
console-invokable disaster-recovery trio (forceRecoverFromServer,
auditConversations, recoverAll) from static/js/core/conversations.js
into a dedicated leaf static/js/core/conv_disaster_recovery.js.

The three functions form a CLOSED cluster: they are documented as "run
from browser console", their only callers outside the leaf are inside
the leaf itself (recoverAll → auditConversations → forceRecoverFromServer),
and grep across the whole ``static/`` tree confirms zero cross-file
callers. Moving them to their own file:

  * shrinks the boot-critical core/conversations.js by ~85L that never
    fires on open;
  * lets the RULES they embody (adopt-only-if-server-longer,
    preserve-pinning, cross-invocation through window scope) be driven
    directly rather than via the full 1600L conversations.js harness;
  * keeps the trio available on window scope via bundle-concat, so
    console users still type ``forceRecoverFromServer("...")`` and it
    Just Works — cross-invocation is proven below with a JSDOM harness.

Failing-first: these guards were written BEFORE the extraction; each is
RED until the leaf lands, the manifest is updated, and conversations.js
delegates.
"""

from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONV_JS = ROOT / 'static' / 'js' / 'core' / 'conversations.js'
LEAF_JS = ROOT / 'static' / 'js' / 'core' / 'conv_disaster_recovery.js'
INDEX_HTML = ROOT / 'index.html'


# ---------------------------------------------------------------------------
# 1. Leaf module exists and defines all three functions at top-level
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_defines_trio_at_top_level():
    assert LEAF_JS.exists(), (
        f'{LEAF_JS} must exist — it houses the extracted disaster-recovery '
        'trio from conversations.js')
    src = LEAF_JS.read_text()
    import re
    for sym in ('forceRecoverFromServer', 'auditConversations', 'recoverAll'):
        m = re.search(
            r'^async\s+function\s+' + re.escape(sym) + r'\s*\(',
            src, re.MULTILINE,
        )
        assert m, (
            f'{sym} must be a top-level `async function` in the leaf so '
            'bundle-concat exposes it via the shared window scope')


def test_leaf_carries_pivotal_body_lines():
    """The extracted bodies must preserve their load-bearing behaviour.
    Each assertion targets a distinct RULE that a stealth stub could
    break; NEUTER-detection for silent regressions."""
    src = LEAF_JS.read_text()

    # forceRecoverFromServer: only adopts when server has MORE messages
    assert 'serverMsgs.length > localMsgs.length' in src, (
        'forceRecoverFromServer must only adopt when server has MORE '
        'messages than local — never overwrite when local is longer')

    # forceRecoverFromServer: preserves pinning across the settings apply
    assert 'keepPinned = conv.pinned' in src, (
        'forceRecoverFromServer must snapshot conv.pinned BEFORE calling '
        '_applySettingsToConv (which would blow it away) and restore after')
    assert 'conv.pinned = keepPinned' in src, (
        'forceRecoverFromServer must RESTORE conv.pinned after the '
        'settings-apply step to keep the sidebar pin sticky')

    # forceRecoverFromServer: adopts server rev when present (numeric)
    assert "typeof data.rev === 'number'" in src, (
        'forceRecoverFromServer must adopt a numeric server rev so the '
        'next PUT carries the correct baseRev')

    # auditConversations: iterates ALL conversations
    assert 'for (const conv of conversations)' in src, (
        'auditConversations must iterate over ALL conversations to look '
        'for local-vs-server count divergence')
    assert 'serverCount > localCount' in src, (
        'auditConversations flags divergence only when server > local — '
        'never treats local-longer as recoverable data loss')

    # recoverAll: chains audit → force-recover, with a rate-limit sleep
    assert 'await auditConversations()' in src, (
        'recoverAll must fan out from auditConversations() rather than '
        'reproducing a second scan')
    assert 'await forceRecoverFromServer(' in src, (
        'recoverAll must delegate the single-conv recover to '
        'forceRecoverFromServer — not reimplement it')
    assert 'setTimeout(r, 200)' in src, (
        'recoverAll must pace itself between conversations to avoid '
        'hammering the server')


# ---------------------------------------------------------------------------
# 2. conversations.js no longer declares the trio inline
# ---------------------------------------------------------------------------
def test_conversations_js_no_longer_declares_trio_inline():
    src = CONV_JS.read_text()
    import re
    for sym in ('forceRecoverFromServer', 'auditConversations', 'recoverAll'):
        m = re.search(
            r'^async\s+function\s+' + re.escape(sym) + r'\s*\(',
            src, re.MULTILINE,
        )
        assert m is None, (
            f'{sym} must live in core/conv_disaster_recovery.js, not '
            'inline in conversations.js')


def test_conversations_js_has_no_stray_calls_to_trio():
    """The trio is console-invokable ONLY — conversations.js MUST NOT
    call these functions from live code paths. If a slice ever adds a
    live caller, that path needs auditing (recoverAll sleeps 200ms per
    conversation — a UI codepath must never chain through it)."""
    src = CONV_JS.read_text()
    import re
    # Strip block + line comments so we count REAL call sites, not doc
    # header mentions like the "Conversation persistence: …" list at top.
    stripped = re.sub(r'/\*[\s\S]*?\*/', '', src)
    stripped = re.sub(r'//[^\n]*', '', stripped)
    for sym in ('forceRecoverFromServer', 'auditConversations', 'recoverAll'):
        # A CALL is `sym(` — a docstring or comma-list is `sym,` or `sym\n`
        assert f'{sym}(' not in stripped, (
            f'conversations.js must NOT call {sym}() from live code — it '
            'is console-invokable only. Found a call site outside comments; '
            'audit that path.')


# ---------------------------------------------------------------------------
# 3. Bundle manifest lists the leaf BEFORE conversations.js
# ---------------------------------------------------------------------------
def test_bundler_lists_leaf_before_conversations_js():
    """Load order: leaf must precede conversations.js so window-level
    forceRecoverFromServer/auditConversations/recoverAll are available
    for console use once the bundle is loaded. Also must sit AFTER
    conv_apply_settings.js because forceRecoverFromServer calls
    ``_applySettingsToConv``."""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lib.js_bundler import _BUNDLE_FILES
    assert 'core/conv_disaster_recovery.js' in _BUNDLE_FILES, (
        'core/conv_disaster_recovery.js missing from _BUNDLE_FILES')
    idx_leaf = _BUNDLE_FILES.index('core/conv_disaster_recovery.js')
    idx_conv = _BUNDLE_FILES.index('core/conversations.js')
    idx_settings = _BUNDLE_FILES.index('core/conv_apply_settings.js')
    assert idx_leaf < idx_conv, (
        f'core/conv_disaster_recovery.js (idx {idx_leaf}) must precede '
        f'core/conversations.js (idx {idx_conv})')
    assert idx_settings < idx_leaf, (
        f'core/conv_apply_settings.js (idx {idx_settings}) must precede '
        f'core/conv_disaster_recovery.js (idx {idx_leaf}) — the trio '
        'depends on _applySettingsToConv being on window scope at call time')


# ---------------------------------------------------------------------------
# 4. Dev-fallback <script> tag exists in index.html
# ---------------------------------------------------------------------------
def test_index_html_has_devfallback_script_tag_for_leaf():
    """Per the peer note about slice 4 (silent-absence risk when
    bundling fails and the fallback path emits individual <script> tags):
    every _BUNDLE_FILES entry MUST have a matching <script> in index.html
    or the fallback silently drops the leaf."""
    src = INDEX_HTML.read_text()
    assert 'core/conv_disaster_recovery.js' in src, (
        'index.html must have a <script defer src="static/js/core/'
        'conv_disaster_recovery.js"> tag for the dev fallback path')
    idx_leaf = src.index('core/conv_disaster_recovery.js')
    idx_conv = src.index('core/conversations.js')
    assert idx_leaf < idx_conv, (
        'core/conv_disaster_recovery.js <script> must appear BEFORE '
        'core/conversations.js in index.html for correct load order on '
        'the dev-fallback path')


# ---------------------------------------------------------------------------
# 5. Semantic exercise via node — cross-invocation still works
# ---------------------------------------------------------------------------
def test_trio_cross_invocation_through_window_via_node():
    """Drive the leaf through node to prove:
    (a) recoverAll can still call auditConversations and
        forceRecoverFromServer via bare-name resolution (as it would in
        the browser once the bundle exposes all three on window scope);
    (b) forceRecoverFromServer's adopt-when-longer rule holds;
    (c) auditConversations' server-vs-local count diff is reported."""
    import shutil
    import subprocess
    if shutil.which('node') is None:
        import pytest
        pytest.skip('node not on PATH — skipping semantic exercise')
    leaf = LEAF_JS.read_text()
    # Provide bundle-scope stubs for the free names the trio reaches:
    # activeConvId, conversations, debugLog, Api, saveConversations,
    # _applySettingsToConv, _restoreConvToolState, window.ConvView.
    harness = r'''
let activeConvId = 'conv-A';
const conversations = [
  { id: 'conv-A', title: 'A', messages: [{_msgId: 'a1'}], pinned: true, pinnedAt: 1234 },
  { id: 'conv-B', title: 'B', messages: [{_msgId: 'b1'}, {_msgId: 'b2'}], pinned: false },
];
const debugLog = () => {};
let applySettingsCalls = 0;
const _applySettingsToConv = () => { applySettingsCalls++; };
const _restoreConvToolState = () => {};
const saveConversations = () => {};
const Api = {
  conversations: {
    get: async (id) => {
      if (id === 'conv-A') {
        return {
          messages: [{_msgId: 'a1'}, {_msgId: 'a2'}, {_msgId: 'a3'}],
          title: 'A (server)',
          rev: 42,
          settings: {model: 'x'},
        };
      }
      if (id === 'conv-B') {
        // Server has same count as local — no rescue
        return { messages: [{_msgId: 'b1'}, {_msgId: 'b2'}], rev: 7 };
      }
      return null;
    },
  },
};
let replaceAllCalls = 0;
globalThis.window = { ConvView: { replaceAll: () => { replaceAllCalls++; } } };
const assert = require('assert');
''' + leaf + r'''

(async () => {
  // (a) forceRecoverFromServer adopts server's longer list
  const before = conversations[0].messages.length;
  const res = await forceRecoverFromServer('conv-A');
  assert.strictEqual(res.messages.length, 3, 'server-longer must be adopted');
  assert.strictEqual(res.title, 'A (server)', 'title must be adopted');
  assert.strictEqual(res._serverRev, 42, 'numeric rev must be adopted');
  assert.strictEqual(res.pinned, true, 'pinning must be preserved');
  assert.strictEqual(res.pinnedAt, 1234, 'pinnedAt must be preserved');
  assert.ok(applySettingsCalls >= 1, '_applySettingsToConv must fire');
  assert.strictEqual(replaceAllCalls, 1, 'ConvView.replaceAll must fire for active conv');

  // (b) forceRecoverFromServer does NOT overwrite when server is not longer
  const bBefore = conversations[1].messages.length;
  activeConvId = 'conv-B';
  await forceRecoverFromServer('conv-B');
  assert.strictEqual(conversations[1].messages.length, bBefore,
    'same-count reply must not overwrite messages');

  // (c) auditConversations reports the divergence — reset conv-A first
  conversations[0].messages = [{_msgId: 'a1'}];
  const issues = await auditConversations();
  const aIssue = issues.find(i => i.id === 'conv-A');
  assert.ok(aIssue, 'auditConversations must flag conv-A as recoverable');
  assert.strictEqual(aIssue.diff, 2, 'diff must be server - local');

  // (d) recoverAll chains audit → force-recover; window-scope bare-name
  //     resolution must work for its inner call to forceRecoverFromServer
  //     and auditConversations.
  replaceAllCalls = 0;
  await recoverAll();
  assert.strictEqual(conversations[0].messages.length, 3,
    'recoverAll must have restored conv-A');

  console.log('OK');
})().catch(e => { console.error(e); process.exit(1); });
'''
    result = subprocess.run(
        ['node', '-e', harness],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        f'node harness failed:\nstdout={result.stdout}\nstderr={result.stderr}')
    assert 'OK' in result.stdout, (
        f'node harness did not print OK:\nstdout={result.stdout}')
