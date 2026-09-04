#!/usr/bin/env python3
"""tests/test_frontend_remote_picker.py — RWA P4b-2a:项目选择器「远程设备」分组.

拍板 6A(docs/REMOTE_WORKTREE_DESIGN.md §5 P4):
  * 伪路径短路:conv.projectPath = ``remote:<agent>:<root>`` 的会话
    **绝不**调 Api.project.setPaths(服务器 fs 上没有这个路径,
    调了就是 400/误清),项目栏渲染合成态(active,路径徽章);
  * 目录浏览弹窗顶部「远程设备」分组:在线 agent 的共享根可一键
    ``mpAddBrowsedPath('remote:…')``;离线 agent 灰显且不可加;
  * 装配钉:index.html 容器 / browseDirectory 触发渲染 / _isRemotePath。

Run isolated: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_frontend_remote_picker.py
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, ROOT, node_deps_available, run_harness, skip_or_fail
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Epic-E sub-7 split (2026-08-01): the STATE seam (_restoreConvProject /
# _isRemotePath / _applyRemoteProjectState) lives in project_state.js (core);
# the PANEL renderer (_renderRemoteDevicesSection / browseDirectory) stays in
# project.js (deferred). The jsdom harness evals BOTH, in bundle order.
_PROJECT_STATE_JS = os.path.join(JS_DIR, 'project_state.js')
_PROJECT_JS = os.path.join(JS_DIR, 'project.js')
_PROJECT_BROWSE_JS = native_module_path(
    '.native/project-browse-coordinator-remote-picker.js',
    os.path.join(
        PROJECT_ROOT, 'frontend', 'src', 'core',
        'project-browse-coordinator.ts',
    ),
)


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════
#  1. 装配钉
# ══════════════════════════════════════════════════════════════════════

def test_browse_modal_has_remote_section_container():
    html = _read(os.path.join(PROJECT_ROOT, 'index.html'))
    assert 'id="remoteDevicesSection"' in html, (
        '目录浏览弹窗缺 remoteDevicesSection 容器')


def test_project_js_has_remote_seams():
    state_src = _read(_PROJECT_STATE_JS)
    assert 'function _isRemotePath(' in state_src, 'project_state.js 缺 _isRemotePath'
    assert '_applyRemoteProjectState' in state_src, '缺 bar 合成态函数'
    panel_src = _read(_PROJECT_JS)
    assert '_renderRemoteDevicesSection' in panel_src, '缺远程分组渲染函数'
    # browseDirectory 必须触发分组渲染(弹窗每次打开/换目录都新鲜)
    assert '_renderRemoteDevicesSection()' in panel_src


def test_i18n_remote_group_key():
    for locale in ('en.json', 'zh.json'):
        src = _read(os.path.join(
            PROJECT_ROOT, 'frontend', 'src', 'i18n', 'locales', locale))
        assert '"devices.remoteGroup"' in src, (
            f'{locale} 缺 devices.remoteGroup')


# ══════════════════════════════════════════════════════════════════════
#  2. 行为(jsdom 真驱)
# ══════════════════════════════════════════════════════════════════════

_PICKER_BODY = r"""
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);

const calls = { setPaths: [], devices: [] };
const devicesFixture = {
  agents: [
    { agent_id: 'aaaa1111', name: 'macbook', platform: 'darwin', online: true,
      share_roots: [{ name: 'myapp', path: '/code/myapp' },
                    { name: 'lib', path: '/code/lib' }] },
    { agent_id: 'bbbb2222', name: 'winbox', platform: 'win32', online: false,
      share_roots: [{ name: 'legacy', path: 'C:/code/legacy' }] },
  ],
  tokens: [],
};

const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<div id="projectBar"></div><div id="projectBadge"></div>' +
    '<div id="projectBarStats"></div><div id="projectBarFolders"></div>' +
    '<div id="remoteDevicesSection"></div>' +
    '</body>',
  targets: [process.argv[5], process.argv[2], process.argv[4]],
  globals: {
    activeConvId: 'c1',
    conversations: [{ id: 'c1' }],
    debugLog: () => {},
    reconcileConversationCatalogMetadata: () => {},
    persistConversationSettings: () => {},
    getActiveConv: () => null,
    _stopScanPoll: () => {},
    _applyProjectData: () => {},
    onProjectCleared: () => {},
    saveRecentProject: () => {},
    // project.js 的 projectState 经 setup globals 双挂(win+node global)——
    // eval'd 代码里的裸读写查的是 node global,挂 window 上是另一对象。
    projectState: { active: false, path: '', extraRoots: [] },
    Api: {
      project: {
        setPaths: async (paths, ro) => { calls.setPaths.push(paths);
          return { ok: true, json: async () => ({}) }; },
      },
      desktop: {
        devices: async () => { calls.devices.push(1); return devicesFixture; },
      },
    },
  },
});

// ── A. 伪路径会话恢复:setPaths 绝不被调,bar 合成态 ──
const conv = { id: 'c1', projectPath: 'remote:aaaa1111:myapp',
               projectPaths: ['remote:aaaa1111:myapp'] };
await _restoreConvProject(conv);
for (let _i = 0; _i < 6; _i++) await Promise.resolve();
check('remote_restore_never_calls_setPaths', calls.setPaths.length === 0);
check('remote_restore_conv_untouched',
      conv.projectPath === 'remote:aaaa1111:myapp');

// ── B. bar 徽章(restore 自己调 _updateProjectUI;projectState 是
//    script-let,外部读不到,可观测面就是渲染出的 DOM)──
const badgeHtml = document.getElementById('projectBarFolders').innerHTML;
check('remote_restore_bar_rendered', badgeHtml.length > 0);
check('remote_restore_badge_title_keeps_pseudo',
      badgeHtml.includes('remote:aaaa1111:myapp'));
check('bar_badge_shows_agent_root', badgeHtml.includes('aaaa1111:myapp'));
check('bar_badge_not_raw_remote_prefix',
      !badgeHtml.includes('>remote:aaaa1111'));

// ── C. 远程分组渲染:在线可加、离线灰显 ──
let added = [];
window._mpFolders = [];
window._mpRenderTags = () => {};
window._browseState = { path: '~' };
window.browseDirectory = async () => {};
global.mpAddBrowsedPath = window.mpAddBrowsedPath = (p) => { added.push(p); };
await _renderRemoteDevicesSection();
for (let _i = 0; _i < 6; _i++) await Promise.resolve();
const sec = document.getElementById('remoteDevicesSection');
const html = sec.innerHTML;
check('section_fetched_devices', calls.devices.length >= 1);
check('section_shows_online_agent', html.includes('macbook'));
check('section_shows_offline_agent', html.includes('winbox'));
const addBtns = sec.querySelectorAll('.remote-root-add');
check('online_roots_have_add_buttons', addBtns.length === 2);
const offlineRow = sec.querySelector('.remote-agent-offline');
check('offline_agent_greyed', !!offlineRow);
check('offline_has_no_add',
      !offlineRow.innerHTML.includes('remote-root-add'));

// 点击第一个 add → mpAddBrowsedPath 收到伪路径
addBtns[0].click();
check('add_emits_pseudo_path', added[0] === 'remote:aaaa1111:myapp');

report();
process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_remote_picker_behaviour_jsdom():
    run_harness(
        target_js=_PROJECT_STATE_JS,
        body_js=_PICKER_BODY,
        extra_targets=[_PROJECT_JS, _PROJECT_BROWSE_JS],
        min_pass=13,
        label='remote picker',
    )


def test_NEUTER_strip_shortcircuit_calls_setPaths():
    """NEUTER:摘掉 _restoreConvProject 的伪路径短路 → setPaths 拿着
    'remote:…' 直奔服务器(400/误清的真实 bug 形态)= 短路承重。"""
    # 手写 node 子进程(绕过 run_harness 因为要断言 FAIL 输出),所以共享 dep
    # guard 也必须手写一遍 —— 无 jsdom 的 lane(公共 CI test-unit)要响亮地
    # SKIP,而不是拿空 stdout 断言失败。
    if not node_deps_available():
        skip_or_fail('node + jsdom dev-deps not installed (run `npm install`)')
    import subprocess
    import tempfile
    src = _read(_PROJECT_STATE_JS)
    anchor = 'if (_isRemotePath(savedPath)) {'
    assert anchor in src, 'neuter 锚点不在 —— 短路形态变了?'
    neutered = src.replace(anchor, 'if (false) {', 1)
    body = _PICKER_BODY.replace(
        "check('remote_restore_never_calls_setPaths', calls.setPaths.length === 0);",
        "check('neuter_setPaths_called_with_pseudo', calls.setPaths.length === 1"
        " && calls.setPaths[0][0] === 'remote:aaaa1111:myapp');")
    tmp = []
    try:
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(_PROJECT_STATE_JS),
            delete=False, encoding='utf-8') as fh:
            npath = fh.name
            fh.write(neutered)
        tmp.append(npath)
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(os.path.abspath(__file__)),
            delete=False, encoding='utf-8') as hf:
            harness = hf.name
            hf.write(body)
        tmp.append(harness)
        proc = subprocess.run(
            ['node', harness, npath, ROOT, _PROJECT_JS, _PROJECT_BROWSE_JS],
            capture_output=True, text=True, timeout=60,
            env={**os.environ,
                 'JSDOM_HARNESS': os.path.join(
                     os.path.dirname(os.path.abspath(__file__)),
                     '_jsdom_harness.js')})
        out = (proc.stdout or '').strip()
        assert proc.returncode == 0, (
            f'NEUTER harness crashed:\n{proc.stderr}\n{out}')
        assert 'PASS neuter_setPaths_called_with_pseudo' in out, (
            f'NEUTER 未咬:摘掉短路后 setPaths 仍未被伪路径调用?\n{out}')
    finally:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass
