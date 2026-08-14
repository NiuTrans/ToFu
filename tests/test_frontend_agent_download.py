"""Local Control's one-file controlled-end download matrix.

The remote branch offers one personalized EXE as its primary action. Server
address and credential live inside it, so there is no archive, sidecar,
numbered setup flow, pairing control, copied token, or native paste step. The
full desktop app remains a collapsed secondary choice. A stale/missing agent
artifact shows an honest preparation state and never exposes the bare EXE.

The suite also pins the local-source role layout, public/proxied hosts, the
loopback-bind warning, awaiting state, and poll-render state preservation.
Two neuters prove both the installer branch and the signature gate are live.

Loads the REAL shipped local-control.js under jsdom; skips when
node+jsdom are absent (same convention as test_frontend_cmd_collapse.py).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
LOCAL_CONTROL = runtime_section_path('local-control.js')


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
  src = src.replace('if (d && d.agent_installer_ready) {', 'if (false) {');
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

// ── 1. remote + ready artifact ⇒ one-file installer PRIMARY ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  agent_installer_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html1 = document.getElementById('lcDesktopSetup').innerHTML;
check('remote_installer_button_primary', html1.includes('lcAgentInstallerBtn'));
check('remote_installer_href', html1.includes('/api/v1/desktop/agent-installer'));
check('remote_direct_run_note', html1.includes('下载后直接运行即可'));
check('remote_no_zip', !/ZIP|解压|两个文件/.test(html1));
check('remote_no_numbered_setup', !/[①②③]/.test(html1));
check('remote_no_pair_button', !html1.includes('lcPairBtn'));
check('remote_full_secondary_toggle', html1.includes('下载完整桌面版'));
check('remote_full_collapsed',
  html1.includes('<details class="lc-details"><summary>') &&
  !html1.includes('<details class="lc-details" open'));
check('remote_full_link_present', html1.includes('Tofu-Setup-0.16.0-win64.exe'));
check('remote_installer_before_full',
  html1.indexOf('lcAgentInstallerBtn') < html1.indexOf('Tofu-Setup'));
check('remote_no_connect_line', !html1.includes('lcMintBtn') &&
  !html1.includes('连接行'));
check('remote_escape_hatch_once',
  (html1.match(/查看全部下载/g) || []).length === 1);
check('remote_no_pairing_vocabulary', !html1.includes('配对码'));

// ── 1b. stale installer ⇒ honest preparation state ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  agent_installer_ready: false,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html1b = document.getElementById('lcDesktopSetup').innerHTML;
check('stale_rebuilding_note', html1b.includes('后台准备'));
check('stale_no_bare_exe', !html1b.includes('TofuAgent-Setup-0.16.0-win64.exe'));
check('stale_no_installer_button', !html1b.includes('lcAgentInstallerBtn'));
check('stale_no_manual_pairing', !html1b.includes('lcMintBtn'));

// ── 2. remote WITHOUT agent artifact ⇒ full fallback, no dead end ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://tofu.example.com/',
  downloads: [FULL], agent_downloads: [] }, null);
const html2 = document.getElementById('lcDesktopSetup').innerHTML;
check('fallback_preparing_primary', html2.includes('后台准备'));
check('fallback_full_link_secondary', html2.includes('Tofu-Setup-0.16.0-win64.exe'));
check('fallback_no_installer_button', !html2.includes('lcAgentInstallerBtn'));
check('fallback_no_pair_button', !html2.includes('lcPairBtn'));
check('fallback_no_connect_line', !html2.includes('lcMintBtn'));
check('fallback_no_pairing_copy', !html2.includes('配对码'));

// ── 3. local_source ⇒ BOTH installs role-labeled, installer in agent card ──
const LOCAL_SRC = { connected: false, setup_state: 'local_source',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'http://127.0.0.1:15000/',
  agent_installer_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] };
_lcRenderDesktop(LOCAL_SRC, null);
const html3 = document.getElementById('lcDesktopSetup').innerHTML;
check('local_source_full_link', html3.includes('Tofu-Setup-0.16.0-win64.exe'));
check('local_source_installer_button', html3.includes('lcAgentInstallerBtn'));
check('local_source_installer_in_agent_role',
  html3.indexOf('lcAgentInstallerBtn') < html3.indexOf('完整桌面版'));
check('local_source_no_pair_button', !html3.includes('lcPairBtn'));
check('local_source_no_connect_line', !html3.includes('lcMintBtn'));
check('local_source_primary_accent', html3.includes('lc-role-primary'));
check('local_source_role_notes',
  html3.includes('另一台电脑访问') && html3.includes('服务器本机'));
check('local_source_one_step',
  (html3.match(/lc-step/g) || []).length === 1);

// ── 4. unchanged poll beat PRESERVES user interaction state ──
// (Every 3s repaint used to rewrite innerHTML and collapse open details.)
const box = document.querySelector('#lcDesktopSetup details');
box.open = true;
_lcRenderDesktop(LOCAL_SRC, null);   // identical payload — a poll beat
const box2 = document.querySelector('#lcDesktopSetup details');
check('rerender_preserves_open_details', box2 === box && box2.open === true);
// …but a CHANGED payload still re-renders (the poll's whole point).
_lcRenderDesktop({ connected: true, setup_state: 'connected',
  downloads: [FULL], agent_downloads: [AGENT] }, null);
check('state_change_still_rerenders',
  document.getElementById('lcDesktopSetup').innerHTML === '');

// ── 6. every host class keeps connection details internal ──
const PROXIED = { connected: false, setup_state: 'remote',
  download_url: 'https://github.com/x/y/releases/latest',
  server_url: 'https://5665bc99-vscode-zw05.mlp.internal.example.com/',
  server_url_reachability: 'public',
  agent_installer_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] };
_lcRenderDesktop(PROXIED, null);
const html6 = document.getElementById('lcDesktopSetup').innerHTML;
check('public_host_keeps_installer', html6.includes('lcAgentInstallerBtn'));
check('public_host_hides_connect_line', !html6.includes('lcMintBtn'));
check('public_host_no_pair_button', !html6.includes('lcPairBtn'));
check('public_host_never_teaches_manual_tunnel',
  !/隧道地址|ssh 隧道|ssh-tunnel/.test(html6));
// Private/loopback hosts also keep connection details internal.
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private',
  agent_installer_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html6p = document.getElementById('lcDesktopSetup').innerHTML;
check('private_host_has_no_connect_line', !html6p.includes('lcMintBtn'));
// Installer downloaded but nothing arrived: a simple automatic wait state.
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private', bridge_tokens_issued: 2,
  agent_installer_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
const html6b = document.getElementById('lcDesktopSetup').innerHTML;
check('awaiting_hint_when_tokens_but_no_agent',
  html6b.includes('首次连入'));
check('awaiting_hint_is_simple', html6b.includes('自动寻找服务器'));
check('awaiting_hint_no_pairing_code', !html6b.includes('配对码'));
// …but never cry wolf once connected, or before any token exists.
_lcRenderDesktop({ connected: true, setup_state: 'connected',
  bridge_tokens_issued: 2,
  agent_installer_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
check('no_awaiting_hint_when_connected',
  !document.getElementById('lcDesktopSetup').innerHTML.includes('首次连入'));
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private', bridge_tokens_issued: 0,
  agent_installer_ready: true,
  downloads: [FULL], agent_downloads: [AGENT] }, null);
check('no_awaiting_hint_without_tokens',
  !document.getElementById('lcDesktopSetup').innerHTML.includes('首次连入'));

// ── 7. loopback bind ⇒ the operator-facing warning surfaces ──
_lcRenderDesktop({ connected: false, setup_state: 'remote',
  download_url: '', server_url: 'http://192.168.1.10:15000/',
  server_url_reachability: 'private', server_bind: 'loopback',
  agent_installer_ready: true,
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
             LOCAL_CONTROL,                                  # argv[2]
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
             if ln.startswith(('FAIL remote_installer',
                               'FAIL remote_direct_run'))]
    assert len(fails) >= 3, (
        'the agent-branch neuter should fail the primary-installer checks — '
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
