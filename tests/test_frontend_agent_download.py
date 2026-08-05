"""tests/test_frontend_agent_download.py — Local Control's two-component
download matrix (A3, docs/DESKTOP_AGENT_DIST_DESIGN.md §4.7), 2026-08-05
re-pinned for the ZERO-CONFIG bundle flow (owner decree: pairing codes
retired — the credential rides the per-download ZIP, never the user's
keyboard).

The remote branch of ``_lcRenderDesktop`` must offer the agent BUNDLE
(exe + baked attach.json) as the PRIMARY action in a numbered TWO-step
flow, with the full desktop app collapsed into a one-line <details>
secondary and the connect line demoted to an advanced details — and must
degrade to the full-installer rendering when no agent artifact exists
(stale-while-build, never a dead end). Pinned:

  1. agent artifact + agent_bundle_ready ⇒ ①② steps, the BUNDLE button
     (href=/api/v1/desktop/agent-bundle) first, the auto-connect copy,
     NO ③ and NO pair button anywhere, full link inside a COLLAPSED
     details, connect line in its own details, escape hatch once;
  1b. artifact present but bundle NOT ready (stale exe predating the
     attach flow) ⇒ rebuilding note + the bare exe as the repair path,
     never a dead button;
  2. no agent_downloads ⇒ full-installer fallback with the connect-line
     details, no bundle button, no pair vocabulary;
  3. local_source ⇒ BOTH installs role-labeled, the bundle button inside
     the AGENT role card, connect line only inside the advanced details,
     exactly one lc-step heading;
  4. an unchanged poll beat PRESERVES user interaction state (the minted
     connect line, an expanded section) — the 3s repaint must not blow
     the DOM away; a changed payload still re-renders;
  6. public host ⇒ the bundle button STILL renders (it carries its own
     route candidates) but the connect line VANISHES (its address half
     is a measured SSO dead end); NO surface instructs a manual ssh
     tunnel or a pairing code;
  7. a loopback-bound server surfaces the operator-facing bind warning;
  8. NEUTER ×2: severing the agent branch fails the remote checks;
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

// ── 1. remote + artifact + bundle ready ⇒ bundle PRIMARY, 2 steps ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  agent_bundle_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html1 = document.getElementById('lcDesktopSetup').innerHTML;
check('remote_bundle_button_primary', html1.includes('lcAgentBundleBtn'));
check('remote_bundle_href', html1.includes('/api/v1/desktop/agent-bundle'));
check('remote_bundle_note', html1.includes('解压 ZIP'));
check('remote_step1_numbered', html1.includes('① 下载并安装受控端'));
check('remote_step2_auto', html1.includes('② 装完启动即可'));
check('remote_no_third_step', !html1.includes('③'));
check('remote_no_pair_button', !html1.includes('lcPairBtn'));
check('remote_full_secondary_toggle', html1.includes('下载完整桌面版'));
check('remote_full_collapsed',
  html1.includes('<details class="lc-details"><summary>') &&
  !html1.includes('<details class="lc-details" open'));
check('remote_full_link_present', html1.includes('Tofu-Setup-0.16.0-win64.exe'));
check('remote_bundle_before_full',
  html1.indexOf('lcAgentBundleBtn') < html1.indexOf('Tofu-Setup'));
check('remote_mint_demoted_to_details', html1.includes('lcMintBtn') &&
  html1.includes('高级：连接行'));
check('remote_escape_hatch_once',
  (html1.match(/查看全部下载/g) || []).length === 1);
check('remote_no_pairing_vocabulary', !html1.includes('配对码'));

// ── 1b. artifact present but bundle NOT ready ⇒ honest degraded path ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  agent_bundle_ready: false,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html1b = document.getElementById('lcDesktopSetup').innerHTML;
check('stale_rebuilding_note', html1b.includes('后台重建'));
check('stale_bare_exe_kept', html1b.includes('TofuAgent-Setup-0.16.0-win64.exe'));
check('stale_no_bundle_button', !html1b.includes('lcAgentBundleBtn'));
check('stale_connect_line_kept', html1b.includes('lcMintBtn'));

// ── 2. remote WITHOUT agent artifact ⇒ full fallback, no dead end ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  downloads: [FULL], agent_downloads: [] }, null);
const html2 = document.getElementById('lcDesktopSetup').innerHTML;
check('fallback_full_link_primary', html2.includes('Tofu-Setup-0.16.0-win64.exe'));
check('fallback_no_bundle_button', !html2.includes('lcAgentBundleBtn'));
check('fallback_no_pair_button', !html2.includes('lcPairBtn'));
check('fallback_connect_line_in_details', html2.includes('lcMintBtn') &&
  html2.includes('高级：连接行'));
check('fallback_no_pairing_copy', !html2.includes('配对码'));

// ── 3. local_source ⇒ BOTH installs role-labeled, bundle in agent card ──
const LOCAL_SRC = { connected: false, setup_state: 'local_source',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'http://127.0.0.1:15000/',
  agent_bundle_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] };
_lcRenderDesktop(LOCAL_SRC, null);
const html3 = document.getElementById('lcDesktopSetup').innerHTML;
check('local_source_full_link', html3.includes('Tofu-Setup-0.16.0-win64.exe'));
check('local_source_bundle_button', html3.includes('lcAgentBundleBtn'));
check('local_source_bundle_in_agent_role',
  html3.indexOf('lcAgentBundleBtn') < html3.indexOf('完整桌面版'));
check('local_source_no_pair_button', !html3.includes('lcPairBtn'));
check('local_source_connect_line_in_details',
  html3.includes('lcMintBtn') && html3.includes('高级：连接行'));
check('local_source_primary_accent', html3.includes('lc-role-primary'));
check('local_source_role_notes',
  html3.includes('另一台电脑访问') && html3.includes('服务器本机'));
check('local_source_one_step',
  (html3.match(/lc-step/g) || []).length === 1);

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

// ── 6. public host ⇒ bundle stays (carries its own routes), line dies ──
const PROXIED = { connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://5665bc99-vscode-zw05.mlp.internal.example.com/',
  server_url_reachability: 'public',
  agent_bundle_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] };
_lcRenderDesktop(PROXIED, null);
const html6 = document.getElementById('lcDesktopSetup').innerHTML;
check('public_host_keeps_bundle', html6.includes('lcAgentBundleBtn'));
check('public_host_hides_connect_line', !html6.includes('lcMintBtn'));
check('public_host_no_pair_button', !html6.includes('lcPairBtn'));
check('public_host_never_teaches_manual_tunnel',
  !/隧道地址|ssh 隧道|ssh-tunnel/.test(html6));
// private/loopback hosts keep the demoted connect-line fallback.
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private',
  agent_bundle_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html6p = document.getElementById('lcDesktopSetup').innerHTML;
check('private_host_keeps_connect_line_in_details',
  html6p.includes('lcMintBtn') && html6p.includes('高级：连接行'));
// credentials-issued-but-nothing-arrived: points at the agent's own
// link-state line, never at a manual tunnel or a re-pair.
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private', bridge_tokens_issued: 2,
  agent_bundle_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html6b = document.getElementById('lcDesktopSetup').innerHTML;
check('awaiting_hint_when_tokens_but_no_agent',
  html6b.includes('首次连入'));
check('awaiting_hint_points_at_link_line', html6b.includes('链路'));
check('awaiting_hint_no_pairing_code', !html6b.includes('配对码'));
// …but never cry wolf once connected, or before any token exists.
_lcRenderDesktop({ connected: true, setup_state: 'connected',
  bridge_tokens_issued: 2,
  agent_bundle_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
check('no_awaiting_hint_when_connected',
  !document.getElementById('lcDesktopSetup').innerHTML.includes('首次连入'));
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private', bridge_tokens_issued: 0,
  agent_bundle_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
check('no_awaiting_hint_without_tokens',
  !document.getElementById('lcDesktopSetup').innerHTML.includes('首次连入'));

// ── 7. loopback bind ⇒ the operator-facing warning surfaces ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private', server_bind: 'loopback',
  agent_bundle_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html7 = document.getElementById('lcDesktopSetup').innerHTML;
check('loopback_bind_warns', html7.includes('BIND_HOST=127.0.0.1'));
// …and a healthy bind never cries wolf.
const html7b = html6p;
check('healthy_bind_no_warning', !html7b.includes('BIND_HOST=127.0.0.1'));

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
             if ln.startswith('FAIL remote_bundle')]
    assert len(fails) >= 3, (
        'the agent-branch neuter should fail the primary-bundle checks — '
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
