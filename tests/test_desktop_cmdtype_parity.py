"""tests/test_desktop_cmdtype_parity.py — desktop 工具 wire 契约守卫(永久).

wire 契约(docs/modules/remote_execution.md):命令 ``type`` = **完整**
工具名,agent 命令表(``lib/desktop_agent/_dispatch.COMMANDS``)键 = wire
type,逐字相等;任何剥前缀/加前缀/别名映射都会让 agent 回 ``Unknown
command``,Studio 桌面工具全灭。

  * 测试一:agent 命令表覆盖全部 LLM schema 名 —— 两半各自的一致性。
  * 测试二:task-loop 入队的 wire type 必须命中 agent 命令表。
"""

import pytest

from lib.desktop_agent._dispatch import COMMANDS as AGENT_COMMANDS
from lib.desktop_tools import DESKTOP_TOOL_NAMES


def _capture_enqueued_cmd_type(monkeypatch, fn_name):
    """经 task-loop 路径(_run_desktop)入队一条命令,回传 wire 上的 cmd_type."""
    import lib.desktop
    from lib.tasks_pkg.handlers.misc import _agents

    captured = {}

    def fake_send_desktop_command(cmd_type, params=None, timeout=30,
                                  target_agent_id=None, user_id=''):
        captured['cmd_type'] = cmd_type
        captured['params'] = params
        return {'ok': True}, None

    # _run_desktop 在函数体内才 from lib.desktop import send_desktop_command,
    # 因此 patch lib.desktop 模块属性即可拦截。
    monkeypatch.setattr(lib.desktop, 'send_desktop_command',
                        fake_send_desktop_command)
    _agents._run_desktop(fn_name, {})
    return captured


@pytest.mark.unit
def test_agent_command_table_covers_all_schema_names():
    """每一个暴露给 LLM 的 desktop_* schema,agent 都有对应命令处理器."""
    missing = sorted(n for n in DESKTOP_TOOL_NAMES if n not in AGENT_COMMANDS)
    assert not missing, (
        f'agent COMMANDS 缺少 schema 暴露的工具: {missing} '
        '(desktop_move_file 刻意不暴露,见 lib/desktop_tools.py 注释)')


@pytest.mark.unit
def test_enqueued_wire_type_matches_agent_command_table(monkeypatch):
    """task-loop 入队的 cmd_type 必须是 agent 命令表的合法键(完整工具名)."""
    for fn_name in sorted(DESKTOP_TOOL_NAMES):
        captured = _capture_enqueued_cmd_type(monkeypatch, fn_name)
        enqueued = captured.get('cmd_type')
        assert enqueued in AGENT_COMMANDS, (
            f'wire 错配: {fn_name} 被入队为 {enqueued!r} —— '
            f'agent 只会回 Unknown command(完整名才命中 COMMANDS)')
