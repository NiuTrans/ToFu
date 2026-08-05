"""tests/test_frontend_oauth_egress_line.py — OAuth 卡片出口状态行守卫（S4）。

WHY
---
The egress line is the ONLY place a user can tell WHY their subscription
fails (server blocked / agent capability off / no agent). A regression that
renders the wrong state (or hides the line) sends the user down the wrong
debugging path — the "keep failing" class this epic exists to kill.

Drives the REAL static/js/settings/oauth.js `_renderEgressLine` in node with
a fake DOM element and spies, asserting per state: text key, css class,
pin-selector presence, and that a missing egress field hides the line.
NEUTER: dropping the capability-off branch must surface as a red pin.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
OAUTH_JS = os.path.join(ROOT, 'static', 'js', 'settings', 'oauth.js')
NODE = shutil.which('node')

_HARNESS = r"""
const fs = require('fs');
globalThis.window = globalThis;
globalThis.addEventListener = function(){};
delete globalThis.BroadcastChannel;
globalThis.t = (k, vars) => vars ? k + '|' + (vars.name || '') : k;
globalThis.escapeHtml = (s) => String(s);
globalThis.showAlert = () => {};
globalThis.debugLog = () => {};
const el = { innerHTML: '', style: { display: 'none' }, className: '' };
globalThis.document = { getElementById: (id) =>
  (id === 'oauthClaudeEgress' ? el : (id === 'oauthClaudeEgressPin' ? globalThis._pinEl : null)) };
eval(fs.readFileSync(process.argv[1], 'utf8'));
// The unknown-state render below arms the re-poll timer — make it instant so
// the node process drains immediately (modal is null here → chain drops).
_EGRESS_REPOLL_MS = 1;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function render(egress) {
  el.innerHTML = ''; el.style.display = 'none'; el.className = '';
  globalThis._pinEl = null;
  _renderEgressLine('claude', egress);
  return el;
}

// direct
let e = render({ state: 'direct' });
check('direct_visible', e.style.display !== 'none');
check('direct_key', e.innerHTML.indexOf('settings.egressDirect') !== -1);

// agent (2 online → pin selector)
globalThis.Api = { oauth: {
  egressAgentGet: () => Promise.resolve({ pinned: 'a2' }),
  egressAgentSet: (v) => { globalThis._posted = v; return Promise.resolve({ ok: true }); },
}};
globalThis._pinEl = { value: '', onchange: null };
e = render({ state: 'agent', agents: [{ agent_id: 'a1', name: 'mac' }, { agent_id: 'a2', name: 'win' }] });
check('agent_key_with_name', e.innerHTML.indexOf('settings.egressViaAgent|mac') !== -1);
check('pin_select_rendered', e.innerHTML.indexOf('oauthClaudeEgressPin') !== -1);

// agent_no_capability — the default trap state
e = render({ state: 'agent_no_capability' });
check('nocap_warn_class', e.className.indexOf('oauth-egress-warn') !== -1);
check('nocap_guidance_key', e.innerHTML.indexOf('settings.egressAgentNoCap') !== -1);

// unavailable
e = render({ state: 'unavailable' });
check('unavail_bad_class', e.className.indexOf('oauth-egress-bad') !== -1);
check('unavail_key', e.innerHTML.indexOf('settings.egressUnavailable') !== -1);

// unknown (probing)
e = render({ state: 'unknown' });
check('unknown_pending_key', e.innerHTML.indexOf('settings.egressProbing') !== -1);

// missing field hides
e = render(undefined);
check('missing_hidden', e.style.display === 'none' && e.innerHTML === '');

// pin change posts
if (globalThis._pinEl) {
  // The rendered select exists in innerHTML; simulate a change via the wired element.
  // (document.getElementById returns _pinEl for the pin id.)
}
process.stdout.write(out.join('\n'));
"""


def _run(src: str | None = None) -> str:
    if NODE is None:
        pytest.skip('node is required to execute oauth.js')
    path = OAUTH_JS
    tmp = None
    if src is not None:
        import tempfile
        tmp = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                          encoding='utf-8')
        tmp.write(src)
        tmp.close()
        path = tmp.name
    try:
        proc = subprocess.run([NODE, '-e', _HARNESS, path],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'harness failed: {proc.stderr[:800]}'
        return proc.stdout
    finally:
        if tmp is not None:
            os.unlink(tmp.name)


def test_egress_line_states():
    out = _run()
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'egress-line failures:\n' + out
    assert out.count('PASS') >= 10, out


_HARNESS_REPOLL = r"""
const fs = require('fs');
globalThis.window = globalThis;
globalThis.addEventListener = function(){};
delete globalThis.BroadcastChannel;
globalThis.t = (k, vars) => vars ? k + '|' + (vars.name || '') : k;
globalThis.escapeHtml = (s) => String(s);
globalThis.showAlert = () => {};
globalThis.debugLog = () => {};
function mkEl() { return { innerHTML: '', style: { display: 'none' }, className: '' }; }
const elC = mkEl(), elX = mkEl();
globalThis._modalOpen = true;
const modal = { classList: { contains: (c) => c === 'open' && globalThis._modalOpen } };
globalThis.document = { getElementById: (id) =>
  id === 'oauthClaudeEgress' ? elC :
  id === 'oauthCodexEgress' ? elX :
  id === 'settingsModal' ? modal : null };
eval(fs.readFileSync(process.argv[1], 'utf8'));
_EGRESS_REPOLL_MS = 1;   // instant cadence for the test

let statusCalls = 0;
let statusMode = 'unavailable';
globalThis.Api = { oauth: {
  status: () => {
    statusCalls++;
    const mk = () => ({ authenticated: false, status: 'not_started',
                        egress: { state: statusMode, verdict: 'geo_blocked', agents: [] } });
    return Promise.resolve({ claude: mk(), codex: mk() });
  },
  egressAgentGet: () => Promise.resolve({ pinned: '' }),
  egressAgentSet: () => Promise.resolve({ ok: true }),
}};

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  // A — cold-cache paint flips to the verdict once the background probe lands,
  //         and the chain STOPS once resolved.
  _renderEgressLine('claude', { state: 'unknown' });
  check('a_pending_shown', elC.innerHTML.indexOf('settings.egressProbing') !== -1);
  await sleep(30);
  check('a_flipped_unavailable', elC.innerHTML.indexOf('settings.egressUnavailable') !== -1);
  check('a_codex_flipped_too', elX.innerHTML.indexOf('settings.egressUnavailable') !== -1);
  check('a_fetched_once', statusCalls === 1);
  await sleep(20);
  check('a_chain_stopped', statusCalls === 1);
  check('a_budget_freed', _egressRepollAttempts === 0);

  // C — modal closed mid-chain: the timer fires but never re-fetches.
  globalThis._modalOpen = false;
  _renderEgressLine('claude', { state: 'unknown' });
  await sleep(20);
  check('c_modal_closed_drops', statusCalls === 1);
  check('c_budget_reset', _egressRepollAttempts === 0);

  // B — verdict never lands: the chain is BOUNDED at _EGRESS_REPOLL_MAX.
  globalThis._modalOpen = true;
  statusMode = 'unknown';
  _renderEgressLine('claude', { state: 'unknown' });
  await sleep(60);
  check('b_capped_at_max', statusCalls - 1 === _EGRESS_REPOLL_MAX);
  const afterCap = statusCalls;
  await sleep(20);
  check('b_stays_dead', statusCalls === afterCap);

  process.stdout.write(out.join('\n'));
})();
"""


def _run_repoll(src: str | None = None) -> str:
    if NODE is None:
        pytest.skip('node is required to execute oauth.js')
    path = OAUTH_JS
    tmp = None
    if src is not None:
        import tempfile
        tmp = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                          encoding='utf-8')
        tmp.write(src)
        tmp.close()
        path = tmp.name
    try:
        proc = subprocess.run([NODE, '-e', _HARNESS_REPOLL, path],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'harness failed: {proc.stderr[:800]}'
        return proc.stdout
    finally:
        if tmp is not None:
            os.unlink(tmp.name)


def test_unknown_state_repolls_until_verdict():
    """THE bug class: without the re-poll, 出口检测中 is a TERMINAL label —
    the verdict lands in the server cache ~1s after first paint but the open
    panel never re-fetches it (probe TTL 300s ⇒ every settings-open is cold)."""
    out = _run_repoll()
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'egress re-poll failures:\n' + out
    assert out.count('PASS') >= 9, out


def test_NEUTER_unknown_branch_repoll_removed():
    """Drop the _scheduleEgressRepoll() call → scenario A must go red: the
    probing label becomes terminal again (the exact regression guarded)."""
    src = open(OAUTH_JS, encoding='utf-8').read()
    needle = "      _scheduleEgressRepoll();"
    assert needle in src, 'NEUTER anchor missing — test stale'
    neutered = src.replace(needle, '')
    out = _run_repoll(src=neutered)
    assert 'FAIL a_flipped_unavailable' in out, out
    assert 'PASS a_pending_shown' in out, 'unrelated pins must stay green'


def test_NEUTER_capability_off_branch_removed():
    """Kill the agent_no_capability branch → its two pins must go red (the
    default trap state becomes indistinguishable from unavailable)."""
    src = open(OAUTH_JS, encoding='utf-8').read()
    needle = "    case 'agent_no_capability':"
    assert needle in src, 'NEUTER anchor missing — test stale'
    start = src.index(needle)
    end = src.index('break;', start) + len('break;')
    neutered = src[:start] + src[end:]
    out = _run(src=neutered)
    assert 'FAIL nocap_warn_class' in out, out
    assert 'FAIL nocap_guidance_key' in out, out
    assert 'PASS direct_key' in out, 'unrelated pins must stay green'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
