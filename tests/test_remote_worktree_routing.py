"""tests/test_remote_worktree_routing.py — RWA P3:工具投影 + 执行路由.

docs/REMOTE_WORKTREE_DESIGN.md §5 P3 + 拍板 3A(同名路由)+ 约束③第三条:
  * **同名策略**:远程会话沿用 write_file 等工具名 —— schema 逐字节不变,
    仅 description 追加本地执行提示(``with_remote_hint``);
    OFF→ON 一次性 latch-clear(project_ready/multiroot 同范式);
  * **路由**:``_handle_project_tool`` 读 ``cfg['project_remote']``
    (总闸 ``TOFU_REMOTE_WORKTREE``)翻译为 ``project_<fn>`` 命令按
    agent_id 寻址入队;服务器 FS 门(ReadGate/FreshGate/abs_path_guard)
    不适用 —— agent 侧自守(P1 约束⑤);
  * **批准门洞闭合**:``ToolSpec('desktop')`` 补 provides + write_tools,
    desktop 写/执行工具进串行写分区(原来既进并行池又绕 Manual 门);
  * 未映射工具(apply_diffs / insert_content / create_project /
    read_files 批量 / inspect_image)报诚实错,绝不静默落服务器路径。

Run:  pytest tests/test_remote_worktree_routing.py -m unit -v
"""

from __future__ import annotations

import pytest

from lib.tools.registry._build import _build_project_or_code_exec
from lib.tools.registry._spec import ToolContext

_REMOTE_CFG = {'project_remote': {'agent_id': 'agent-A', 'root': 'myapp'}}


def _ctx(cfg=None, conv_id='', project_enabled=True):
    return ToolContext(
        cfg=cfg if cfg is not None else dict(_REMOTE_CFG),
        task_id='task-1', project_path='/srv/app',
        project_enabled=project_enabled, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False, swarm_enabled=False, conv_id=conv_id)


# ═══════════════════════════════════════════════════════════
#  约束③第三条:desktop write_tools 补声明(关批准门洞)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDesktopWriteToolsDeclaration:
    def test_spec_declares_provides_and_write_partition(self):
        from lib.tools import all_specs
        spec = next(s for s in all_specs() if s.key == 'desktop')
        from lib.desktop_tools import DESKTOP_TOOL_NAMES
        assert spec.provides == frozenset(DESKTOP_TOOL_NAMES)
        assert spec.write_tools == frozenset({
            'desktop_write_file', 'desktop_move_file', 'desktop_run_command',
            'desktop_open_app', 'desktop_open_file'})

    def test_dispatch_partition_includes_desktop_writes(self):
        from lib.tasks_pkg.tool_dispatch._flags import _task_partitions
        write, _idem = _task_partitions({})
        assert {'desktop_write_file', 'desktop_move_file',
                'desktop_run_command', 'desktop_open_app',
                'desktop_open_file'} <= set(write)


# ═══════════════════════════════════════════════════════════
#  绑定契约(总闸 + cfg)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBindingContract:
    def test_binding_helper(self, monkeypatch):
        from lib.desktop.remote import remote_worktree_binding
        monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
        assert remote_worktree_binding(
            {'project_remote': {'agent_id': 'a', 'root': 'r'}}) == {
            'agent_id': 'a', 'root': 'r'}
        assert remote_worktree_binding(
            {'project_remote': {'agent_id': 'a'}}) is None
        assert remote_worktree_binding({}) is None
        monkeypatch.delenv('TOFU_REMOTE_WORKTREE', raising=False)
        assert remote_worktree_binding(
            {'project_remote': {'agent_id': 'a', 'root': 'r'}}) is None


# ═══════════════════════════════════════════════════════════
#  投影:同名 schema + 本地执行提示
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestProjection:
    def test_remote_hint_same_names_same_params(self, monkeypatch):
        monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
        from lib.tools import PROJECT_TOOLS
        tools = _build_project_or_code_exec(_ctx())
        base = {t['function']['name']: t for t in PROJECT_TOOLS}
        hinted = {t['function']['name']: t for t in tools}
        # 拍板 3A:同名同 schema,仅 description 变化
        assert set(hinted) == set(base)
        for name, t in hinted.items():
            assert t['function']['parameters'] == \
                base[name]['function']['parameters']
        desc = hinted['write_file']['function']['description']
        assert 'local machine' in desc.lower() or '本地' in desc

    def test_master_switch_off_no_hint(self, monkeypatch):
        monkeypatch.delenv('TOFU_REMOTE_WORKTREE', raising=False)
        from lib.tools import PROJECT_TOOLS
        tools = _build_project_or_code_exec(_ctx())  # cfg 带绑定但总闸关
        by = {t['function']['name']: t for t in tools}
        base = {t['function']['name']: t for t in PROJECT_TOOLS}
        assert by['write_file']['function']['description'] == \
            base['write_file']['function']['description']

    def test_remote_state_is_evaluated_live(self, monkeypatch):
        monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
        conv = 'conv-remote-latch'
        assert _ctx(conv_id=conv).project_remote is True
        monkeypatch.delenv('TOFU_REMOTE_WORKTREE')
        assert _ctx(conv_id=conv).project_remote is False


# ═══════════════════════════════════════════════════════════
#  路由:handler → 桥(按 agent_id 寻址)
# ═══════════════════════════════════════════════════════════

@pytest.fixture()
def routed(monkeypatch):
    """Capture bridge sends + finalized metas from the project handler."""
    import lib.desktop
    import lib.tasks_pkg.handlers.project as hp
    monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
    box = {'send': [], 'meta': []}

    def fake_send(cmd_type, params=None, timeout=30, target_agent_id=None,
                  user_id='', cmd_id=None):
        box['send'].append({'cmd_type': cmd_type, 'params': params,
                            'timeout': timeout,
                            'target_agent_id': target_agent_id})
        return {'content': 'done', 'path': (params or {}).get('path')}, None

    monkeypatch.setattr(lib.desktop, 'send_desktop_command', fake_send)
    monkeypatch.setattr(hp, '_finalize_tool_round',
                        lambda task, rn, round_entry, metas:
                        box['meta'].extend(metas))
    return box


def _call(fn_name, fn_args, cfg=None):
    import lib.tasks_pkg.handlers.project as hp
    task = {'id': 'task-remote-1', 'convId': 'conv-remote-1'}
    return hp._handle_project_tool(
        task, {}, fn_name, 'tc-1', fn_args, 1, {},
        cfg if cfg is not None else dict(_REMOTE_CFG),
        project_path='', project_enabled=False)


@pytest.mark.unit
class TestRouting:
    def test_write_file_routes_to_agent_root(self, routed):
        _call('write_file', {'description': 'd', 'path': 'src/x.py',
                             'content': 'print(1)'})
        call = routed['send'][0]
        assert call['cmd_type'] == 'project_write_file'
        assert call['target_agent_id'] == 'agent-A'
        assert call['params']['root'] == 'myapp'
        assert call['params']['path'] == 'src/x.py'
        assert call['params']['content'] == 'print(1)'
        assert routed['meta'][0]['remoteRoot'] == 'myapp'

    @pytest.mark.parametrize('fn_name,cmd_type,args', [
        ('apply_diff', 'project_apply_diff',
         {'description': 'd', 'path': 'a.py', 'search': 'x', 'replace': 'y'}),
        ('list_dir', 'project_list_dir', {'path': '.'}),
        ('grep_search', 'project_grep_search', {'pattern': 'foo'}),
        ('find_files', 'project_find_files', {'pattern': '*.py'}),
        ('read_files', 'project_read_files', {'path': 'a.py'}),
    ])
    def test_all_mapped_tools_route(self, routed, fn_name, cmd_type, args):
        _call(fn_name, args)
        assert routed['send'][0]['cmd_type'] == cmd_type
        assert routed['send'][0]['params']['root'] == 'myapp'

    def test_run_command_bridge_timeout_tracks_cmd(self, routed):
        _call('run_command', {'command': 'npm test', 'timeout': 600})
        call = routed['send'][0]
        assert call['cmd_type'] == 'project_run_command'
        assert call['timeout'] == 630.0

    def test_unsupported_tool_honest_error(self, routed):
        _tc, content, _ = _call('apply_diffs', {'edits': []})
        assert 'not supported' in content and not routed['send']

    def test_batch_reads_honest_error(self, routed):
        _tc, content, _ = _call('read_files',
                                {'reads': [{'path': 'a'}, {'path': 'b'}]})
        assert 'not supported' in content and not routed['send']

    def test_bridge_error_surfaced(self, monkeypatch, routed):
        import lib.desktop
        monkeypatch.setattr(
            lib.desktop, 'send_desktop_command',
            lambda *a, **k: (None, "target desktop agent 'agent-A' is not "
                                   'online (0 registered agent(s) online)'))
        _tc, content, _ = _call('list_dir', {'path': '.'})
        assert 'not online' in content

    def test_master_switch_off_byte_identical(self, monkeypatch, routed):
        monkeypatch.delenv('TOFU_REMOTE_WORKTREE', raising=False)
        # 总闸关 + cfg 带绑定 → 不路由,落回服务器路径(字节不变)
        _tc, content, _ = _call('grep_search', {'pattern': 'x'})
        assert not routed['send']
        assert content == 'Error: No project path.'

    def test_server_fs_gates_bypassed_on_remote(self, monkeypatch, routed):
        import lib.tasks_pkg.handlers.project as hp

        def _boom(*a, **k):
            raise AssertionError(
                'server-side FS gate must not run for a remote worktree')

        monkeypatch.setattr(hp, 'check_write_freshness', _boom)
        monkeypatch.setattr(hp, 'check_read_before_edit', _boom)
        _call('write_file', {'description': 'd', 'path': 'x', 'content': 'y'})
        assert routed['send']  # 路由成功 = 服务器门未拦(agent 自守)

    def test_neuter_routing_falls_back_to_server(self, monkeypatch, routed):
        """NEUTER:摘掉路由绑定 → write_file 不再进桥 = 路由承重."""
        import lib.tasks_pkg.handlers.project as hp
        monkeypatch.setattr(hp, 'remote_worktree_binding', lambda _cfg: None)
        _tc, content, _ = _call('write_file',
                                {'description': 'd', 'path': 'x', 'content': 'y'})
        assert not routed['send']
        assert content == 'Error: No project path.'
