"""tests/test_frontend_agent_download.py — Local Control's two-component
download matrix (A3, docs/DESKTOP_AGENT_DIST_DESIGN.md §4.7).

The remote branch of ``_lcRenderDesktop`` must offer the AGENT installer
as the PRIMARY action (受控端·轻量 — the machine's role in that branch is
to be controlled) inside a NUMBERED three-step flow, with the full desktop
app collapsed into a one-line <details> secondary — and must degrade to
the historical full-installer rendering when no agent artifact exists yet
(stale-while-build, never a dead end). Pinned:

  1. agent artifact present ⇒ numbered steps (①②③), agent link first
     (受控端·轻量 + 服务器直连 + size), the PAIRING button as primary
     attach action (2026-08-04 decree) with the connect line demoted to
     a collapsed details, full link inside a COLLAPSED details, escape
     hatch once;
  1b. preseeded artifact + open bridge ⇒ ZERO-TOUCH: no mint button, the
     auto-connect copy instead; a required bridge token forces the
     3-step flow back even with a preseed;
  2. no agent_downloads ⇒ full-installer fallback with the SAME pairing
     block, no agent vocabulary (never an empty primary slot);
  3. local_source ⇒ BOTH installs visible, role-labeled, agent block
     FIRST with the pairing button, connect line only inside the
     advanced details, exactly one lc-step heading;
  4. an unchanged poll beat PRESERVES user interaction state (the minted
     connect line, an expanded section) — the 3s repaint must not blow
     the DOM away; a changed payload still re-renders;
  6. public host ⇒ the connect line VANISHES (its address half is a
     measured SSO dead end) and pairing carries everything; NO surface
     instructs a manual ssh tunnel (2026-08-04 owner decree, superseding
     the 2026-08-03 proxy warning);
  7. NEUTER ×2: severing the agent branch fails the remote checks;
     severing the signature gate fails the preservation check.

Loads the REAL shipped local-control.js under jsdom; skips when
node+jsdom are absent (same convention as test_frontend_cmd_collapse.py).
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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const MODE = process.argv[4] || 'normal';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>'
  + '<div id="lcDesktopStatus"><span class="browser-status-dot"></span>'
  + '<span class="lc-status-text"></span></div>'
  + '<div id="lcDesktopSwitch"></div><div id="lcDesktopAbout"></div>'
  + '<div id="lcPermNote"></div><div id="lcDesktopSetup"></div>'
  + '<div id="localControlBadge"></div><div id="localControlToggle"></div>'
  + '</body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k) => k;   // fall back to the literal fallback strings
global.browserEnabled = false; global.desktopEnabled = false;

let src = fs.readFileSync(process.argv[2], 'utf8');
if (MODE === 'neuter') {
  const before = src;
  src = src.replace('if (agentPicks.length) {', 'if (false) {');
  if (src === before) { console.log('NEUTER_NOMUT'); process.exit(2); }
}
if (MODE === 'neuter-gate') {
  const before = src;
  src = src.replace('if (sig === _lcDesktopSigLast) return;',
                    'if (false) {}');
  if (src === before) { console.log('NEUTER_GATE_NOMUT'); process.exit(2); }
}
eval(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const FULL = { os: 'windows', arch: 'x86_64', label: 'Windows installer',
  filename: 'Tofu-Setup-0.16.0-win64.exe',
  url: 'https://h/api/v1/desktop/download/Tofu-Setup-0.16.0-win64.exe',
  hosted: 'server', size: 152000000, source: 'built', kind: 'full' };
const AGENT = { os: 'windows', arch: 'x86_64',
  label: 'Windows agent installer',
  filename: 'TofuAgent-Setup-0.16.0-win64.exe',
  url: 'https://h/api/v1/desktop/download/TofuAgent-Setup-0.16.0-win64.exe',
  hosted: 'server', size: 53000000, source: 'built', kind: 'agent' };

// ── 1. remote + agent artifact ⇒ agent PRIMARY in a numbered flow ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html1 = document.getElementById('lcDesktopSetup').innerHTML;
check('remote_agent_link_present', html1.includes('TofuAgent-Setup-0.16.0-win64.exe'));
check('remote_agent_label', html1.includes('受控端·轻量'));
check('remote_step1_numbered', html1.includes('① 下载并安装受控端'));
check('remote_steps_numbered', html1.includes('②') && html1.includes('③'));
check('remote_full_secondary_toggle', html1.includes('下载完整桌面版'));
check('remote_full_collapsed',
  html1.includes('<details class="lc-details"><summary>') &&
  !html1.includes('<details class="lc-details" open'));
check('remote_full_link_present', html1.includes('Tofu-Setup-0.16.0-win64.exe'));
check('remote_agent_before_full',
  html1.indexOf('TofuAgent-Setup') < html1.indexOf('Tofu-Setup'));
check('remote_pair_button_primary', html1.includes('lcPairBtn'));
check('remote_mint_button', html1.includes('lcMintBtn'));
check('remote_mint_label', html1.includes('生成连接行'));
check('remote_mint_demoted_to_details', html1.includes('高级：连接行')); 
check('remote_hosted_chip_twice',
  (html1.match(/服务器直连/g) || []).length === 2);
check('remote_escape_hatch_once',
  (html1.match(/查看全部下载/g) || []).length === 1);
check('remote_agent_size_shown', html1.includes('50.5 MB') || html1.includes('50.4 MB'));

// ── 1b. preseeded artifact + open bridge ⇒ ZERO-TOUCH (2 steps) ──
const AGENT_PRE = Object.assign({}, AGENT,
  { preseed_url: 'https://tofu.example.com' });
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  bridge_token_required: false,
  downloads: [FULL], agent_downloads: [AGENT_PRE] }, null);
const html1b = document.getElementById('lcDesktopSetup').innerHTML;
check('autoconnect_hides_mint', !html1b.includes('lcMintBtn'));
check('autoconnect_copy', html1b.includes('会自动连上'));
// …but a REQUIRED bridge token forces the mint-and-paste flow back even
// with a usable preseed (the agent still needs a credential).
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  bridge_token_required: true,
  downloads: [FULL], agent_downloads: [AGENT_PRE] }, null);
const html1c = document.getElementById('lcDesktopSetup').innerHTML;
check('token_required_keeps_mint', html1c.includes('lcMintBtn'));
check('token_required_no_autoconnect', !html1c.includes('会自动连上'));

// ── 2. remote WITHOUT agent artifact ⇒ historical fallback, no dead end ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  downloads: [FULL], agent_downloads: [] }, null);
const html2 = document.getElementById('lcDesktopSetup').innerHTML;
check('fallback_full_link_primary', html2.includes('Tofu-Setup-0.16.0-win64.exe'));
check('fallback_no_agent_step', !html2.includes('① 下载并安装受控端'));
check('fallback_historical_text', html2.includes('安装桌面版'));
check('fallback_pair_kept', html2.includes('lcPairBtn'));
check('fallback_mint_in_details', html2.includes('lcMintBtn') && html2.includes('高级：连接行'));

// ── 3. local_source ⇒ BOTH installs visible, role-labeled, no collapse ──
const LOCAL_SRC = { connected: false, setup_state: 'local_source',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'http://127.0.0.1:15000/',
  downloads: [FULL], agent_downloads: [AGENT] };
_lcRenderDesktop(LOCAL_SRC, null);
const html3 = document.getElementById('lcDesktopSetup').innerHTML;
check('local_source_full_link', html3.includes('Tofu-Setup-0.16.0-win64.exe'));
check('local_source_agent_link', html3.includes('TofuAgent-Setup-0.16.0-win64.exe'));
check('local_source_pair_button', html3.includes('lcPairBtn'));
check('local_source_connect_line_in_details',
  html3.includes('lcMintBtn') && html3.includes('高级：连接行'));
check('local_source_agent_first',
  html3.indexOf('TofuAgent-Setup') < html3.indexOf('Tofu-Setup'));
check('local_source_primary_accent', html3.includes('lc-role-primary'));
check('local_source_role_notes',
  html3.includes('另一台电脑访问') && html3.includes('服务器本机'));
check('local_source_one_step',
  (html3.match(/lc-step/g) || []).length === 1);
check('local_source_no_agent_step', !html3.includes('① 下载并安装受控端'));

// ── 4. unchanged poll beat PRESERVES user interaction state ──
// (The 2026-08-03 auto-collapse: every 3s repaint rewrote innerHTML,
// collapsing an opened details and vanishing a minted connect line.)
const box = document.getElementById('lcTokenBox');
box.style.display = 'block';
box.textContent = 'http://127.0.0.1:15000 k_test';
_lcRenderDesktop(LOCAL_SRC, null);   // identical payload — a poll beat
const box2 = document.getElementById('lcTokenBox');
check('rerender_preserves_token_box',
  !!box2 && box2.textContent === 'http://127.0.0.1:15000 k_test' &&
  box2.style.display === 'block');
// …but a CHANGED payload still re-renders (the poll's whole point).
_lcRenderDesktop({ connected: true, setup_state: 'connected',
  downloads: [FULL], agent_downloads: [AGENT] }, null);
check('state_change_still_rerenders',
  document.getElementById('lcDesktopSetup').innerHTML === '');

// ── 6. public host ⇒ connect line VANISHES, pairing carries all ──
// (2026-08-04 owner decree, superseding the 2026-08-03 proxy warning:
// that warning sent the user to reopen this panel via an ssh-tunnel
// address — a MANUAL tunnel instruction, now forbidden on every surface.
// The pairing code is address-free — the agent discovers the route
// itself — so a public host leans on pairing entirely and stops offering
// the measured-dead-end line.)
const PROXIED = { connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://5665bc99-vscode-zw05.mlp.internal.example.com/',
  server_url_reachability: 'public',
  downloads: [FULL], agent_downloads: [AGENT] };
_lcRenderDesktop(PROXIED, null);
const html6 = document.getElementById('lcDesktopSetup').innerHTML;
check('public_host_keeps_pairing', html6.includes('lcPairBtn'));
check('public_host_hides_connect_line', !html6.includes('lcMintBtn'));
check('public_host_never_teaches_manual_tunnel',
  !/隧道地址|ssh 隧道|ssh-tunnel/.test(html6));
// private/loopback hosts keep the demoted connect-line fallback.
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private',
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html6p = document.getElementById('lcDesktopSetup').innerHTML;
check('private_host_keeps_connect_line_in_details',
  html6p.includes('lcMintBtn') && html6p.includes('高级：连接行'));
// paired-but-nothing-arrived: self-heal semantics, never manual ssh.
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private', bridge_tokens_issued: 2,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html6b = document.getElementById('lcDesktopSetup').innerHTML;
check('awaiting_hint_when_tokens_but_no_agent',
  html6b.includes('等待受控端连入'));
check('awaiting_hint_is_self_heal_not_manual_ssh',
  html6b.includes('自动') && !/隧道地址|换.*隧道/.test(html6b));
// …but never cry wolf once connected, or before any token exists.
_lcRenderDesktop({ connected: true, setup_state: 'connected',
  bridge_tokens_issued: 2,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
check('no_awaiting_hint_when_connected',
  !document.getElementById('lcDesktopSetup').innerHTML.includes('等待受控端连入'));
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private', bridge_tokens_issued: 0,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
check('no_awaiting_hint_without_tokens',
  !document.getElementById('lcDesktopSetup').innerHTML.includes('等待受控端连入'));
// absent fields (an older server) render NO awaiting hint at all.
_lcRenderDesktop(LOCAL_SRC, null);
check('absent_fields_render_no_awaiting_hint',
  !document.getElementById('lcDesktopSetup').innerHTML.includes('等待受控端连入')); 

console.log(out.join('\n'));
process.exit(0);
"""


def _run_harness(mode: str = 'normal') -> str:
    harness = os.path.join(HERE, '_agent_download_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'local-control.js'),       # argv[2]
             ROOT,                                           # argv[3]
             mode],                                          # argv[4]
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_agent_download_matrix():
    output = _run_harness('normal')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'agent download matrix failures:\n' + output
    assert output.count('PASS') >= 36, f'expected >=36 PASS lines, got:\n' \
                                       f'{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_severing_the_agent_branch_is_caught():
    output = _run_harness('neuter')
    fails = [ln for ln in output.splitlines()
             if ln.startswith('FAIL remote_agent')]
    assert len(fails) >= 3, (
        'the agent-branch neuter should fail the primary-agent checks — '
        'the suite cannot tell the matrix from the fallback:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_severing_the_signature_gate_is_caught():
    """Without the signature gate every 3s poll rewrites the setup DOM —
    the auto-collapse the owner measured. Sever it and the preservation
    check must go red."""
    output = _run_harness('neuter-gate')
    fails = [ln for ln in output.splitlines()
             if ln.startswith('FAIL rerender_preserves')]
    assert fails, (
        'the gate neuter should fail the preservation check — without the '
        'gate the poll blows the DOM away again:\n' + output)
