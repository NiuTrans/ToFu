"""tests/test_frontend_oauth_egress_agent_btn.py — the 'unavailable' egress
verdict must carry the ONE way out of it.

WHY
---
The egress line (S4) tells the user WHY a subscription cannot reach its
provider. For the ``unavailable`` state ("server blocked AND no desktop
agent") the diagnosis is also a dead end: the fix lives in a completely
different surface (the Local Control modal, which owns the backend-chosen
download links + bridge-token connect line). A user staring at the OAuth
card has no reason to know that surface exists.

The button added by this guard deep-links the verdict to that installer, and
while the modal is open the OAuth status is re-polled so the line flips to
"via agent" the moment the agent connects — otherwise the user installs the
agent, comes back, and still sees "unavailable" until they think to reload.

Drives the REAL static/js/settings/oauth.js in jsdom and asserts:
  1. unavailable → the button is rendered (and wired);
  2. the other four states render NO button (the verdict is not a menu);
  3. click → openLocalControlModal() fires + a 3 s poll is registered;
  4. poll tick with the modal open re-reads the status;
  5. poll tick with the modal closed clears the interval (no leaked timer)
     and does one final refresh;
NEUTER: dropping the button markup turns pin 1 red while 2 stays green.
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
OAUTH_JS = ROOT / "static" / "js" / "settings" / "oauth.js"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


@functools.lru_cache(maxsize=1)
def _has_jsdom() -> bool:
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"], cwd=ROOT,
                       capture_output=True, check=True)
        return True
    except Exception:
        return False


DOM_HTML = """
<div id="oauthClaudeEgress"></div>
<div id="localControlModal" class="modal-overlay"></div>
"""

HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>{html}</body>`);
    global.document = dom.window.document;
    global.window = dom.window;
    global.addEventListener = () => {{}};
    delete global.BroadcastChannel;
    global.t = (k, vars) => vars ? k + '|' + JSON.stringify(vars) : k;
    global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
    global.showAlert = () => {{}};
    global.debugLog = () => {{}};
    global._rec = {{ lcOpened: 0, statusCalls: 0 }};
    global.openLocalControlModal = () => {{ global._rec.lcOpened++; }};
    global.Api = {{ oauth: {{
      status: () => {{ global._rec.statusCalls++; return Promise.resolve({{}}); }},
      egressAgentGet: () => Promise.resolve({{}}),
      egressAgentSet: () => Promise.resolve({{ ok: true }}),
    }} }};
    // Capture intervals instead of running them on a wall clock.
    const intervals = [];
    global.setInterval = (cb, ms) => {{ intervals.push({{ cb, ms, cleared: false }}); return intervals.length; }};
    global.clearInterval = (id) => {{ if (intervals[id - 1]) intervals[id - 1].cleared = true; }};

    {shipped}

    (async () => {{
      const out = {{}};
      const el = document.getElementById('oauthClaudeEgress');
      const render = (egress) => {{
        el.innerHTML = ''; el.style.display = 'none'; el.className = '';
        _renderEgressLine('claude', egress);
        return el;
      }};

      // (1) unavailable → button rendered, carrying the guidance keys.
      render({{ state: 'unavailable' }});
      out.unavailHasBtn = el.innerHTML.includes('oauthClaudeEgressAgentBtn');
      out.unavailBtnKey = el.innerHTML.includes('settings.egressGetAgent');
      out.unavailVerdictKept = el.innerHTML.includes('settings.egressUnavailable');
      out.unavailSubKey = el.innerHTML.includes('settings.egressUnavailSub');
      out.unavailIsCallout = el.innerHTML.includes('oauth-egress-callout');

      // (2) the other four states render NO button.
      out.directNoBtn = !render({{ state: 'direct' }}).innerHTML.includes('EgressAgentBtn');
      out.agentNoBtn = !render({{ state: 'agent', agents: [{{ agent_id: 'a1', name: 'mac' }}] }})
        .innerHTML.includes('EgressAgentBtn');
      out.nocapNoBtn = !render({{ state: 'agent_no_capability' }}).innerHTML.includes('EgressAgentBtn');
      out.unknownNoBtn = !render({{ state: 'unknown' }}).innerHTML.includes('EgressAgentBtn');

      // (3) click → installer opens + a 3 s self-heal poll is registered.
      render({{ state: 'unavailable' }});
      const btn = document.getElementById('oauthClaudeEgressAgentBtn');
      out.btnWired = !!(btn && typeof btn.onclick === 'function');
      if (btn && btn.onclick) btn.onclick();
      out.clickOpened = global._rec.lcOpened;
      out.pollMs = intervals.length === 1 ? intervals[0].ms : -1;

      // (4)+(5) poll ticks. NEUTER tolerance: with the button cut, no
      // poll is ever registered — that IS the guard biting, so the ticks
      // must not crash the harness before the pins can be reported.
      if (intervals[0]) {{
        document.getElementById('localControlModal').classList.add('open');
        intervals[0].cb();
        await new Promise(r => setTimeout(r, 0));
        out.statusAfterOpenTick = global._rec.statusCalls;
        document.getElementById('localControlModal').classList.remove('open');
        intervals[0].cb();
        await new Promise(r => setTimeout(r, 0));
        out.statusAfterCloseTick = global._rec.statusCalls;
        out.pollCleared = intervals[0].cleared;
      }} else {{
        out.statusAfterOpenTick = 0;
        out.statusAfterCloseTick = 0;
        out.pollCleared = false;
      }}

      console.log(JSON.stringify(out));
    }})().catch(e => {{ console.error(e); process.exit(1); }});
""")


def _run(neuter=None) -> dict:
    body = OAUTH_JS.read_text(encoding="utf-8")
    if neuter:
        rewritten = neuter(body)
        assert rewritten != body, (
            "NEUTER substitution did not apply — the anchor text is gone, so "
            "this run proves nothing about whether the guard bites")
        body = rewritten
    script = HARNESS.format(shipped=body, html=DOM_HTML.replace("`", "\\`"))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr[:1500]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_unavailable_renders_the_way_out():
    out = _run()
    assert out["unavailHasBtn"] and out["unavailBtnKey"], (
        "the 'unavailable' verdict renders no path to the desktop-agent "
        "installer — the user is told the diagnosis but not the cure")
    assert out["unavailVerdictKept"], (
        "the verdict text was displaced by the button — both must render")
    assert out["unavailSubKey"] and out["unavailIsCallout"], (
        "the unavailable verdict lost its action-first callout (title + "
        "install-the-agent sub-line) — the user is told WHAT to do, not "
        "just the diagnosis")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_other_states_render_no_button():
    out = _run()
    for k in ("directNoBtn", "agentNoBtn", "nocapNoBtn", "unknownNoBtn"):
        assert out[k], (
            f"{k}: a state that is not 'unavailable' grew the installer "
            f"button — the line must show exactly ONE next action, and only "
            f"where it is the way out")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_click_opens_installer_and_starts_the_self_heal_poll():
    out = _run()
    assert out["btnWired"], "the rendered button is dead markup (no onclick)"
    assert out["clickOpened"] == 1, (
        "clicking did not open the Local Control modal — the deep-link to "
        "the install surface is broken")
    assert out["pollMs"] == 3000, (
        f"self-heal poll registered at {out['pollMs']}ms, expected 3000")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_poll_self_heals_and_never_leaks():
    out = _run()
    assert out["statusAfterOpenTick"] >= 1, (
        "a poll tick with the modal open did not re-read the status — the "
        "line would stay 'unavailable' until a manual reload")
    assert out["statusAfterCloseTick"] > out["statusAfterOpenTick"], (
        "closing the modal skipped the final refresh")
    assert out["pollCleared"], (
        "the interval survived the modal closing — a leaked 3 s timer that "
        "re-reads the status forever")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_button_markup_removed():
    """Drop the button from the unavailable branch → pin 1 must go red
    while the sibling states stay clean (the guard bites HERE, not on
    unrelated states)."""
    out = _run(lambda s: s.replace(
        "               '<button type=\"button\" class=\"btn-small oauth-egress-agent-btn\" id=\"oauth' + capProvider + 'EgressAgentBtn\"' +\n"
        "               ' title=\"' + escapeHtml(t('settings.egressGetAgentTitle')) + '\">' +\n"
        "               escapeHtml(t('settings.egressGetAgent')) + '</button>' +\n"
        "             '</div>';\n",
        "             '</div>';\n"))
    assert not out["unavailHasBtn"], "NEUTER did not remove the button"
    assert out["directNoBtn"] and out["agentNoBtn"], (
        "unrelated states must stay button-free")


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
