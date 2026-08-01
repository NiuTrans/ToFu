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
