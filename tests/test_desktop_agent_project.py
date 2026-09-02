"""tests/test_desktop_agent_project.py — RWA P1:agent 项目命令集 + 安全网.

docs/modules/remote_execution.md:
  * ``project_*`` 七命令(list/read/write/apply_diff/grep/find/run_command),
    wire type = 完整命令名(约束①,键与 wire 逐字相等);
  * 路径校验**下沉 agent 侧**(约束⑤):root 名 + root 相对路径,
    realpath  containment,'..' / 绝对路径 / 符号链接逃逸全拒;
  * snapshot-before-write:``<root>/.tofu/file-history/<md5>/<epoch>``(约束③);
  * freshness 门:外部改动后写入被拒,**重读刷新令牌**后放行(约束③);
  * 写族归 allow_write、run_command 归 allow_exec(权限分层不变);
  * ``.tofu`` 快照目录对 list/grep/find 不可见。

Run:  pytest tests/test_desktop_agent_project.py -m unit -v
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

import lib.desktop_agent._project as pj
from lib.desktop_agent import _dispatch as disp


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    """Declare one share root 'app' with seed files; per-test freshness store."""
    root = tmp_path / 'app'
    (root / 'src').mkdir(parents=True)
    (root / 'src' / 'main.py').write_text('print("hello")\n', encoding='utf-8')
    (root / 'README.md').write_text('# app\nhello world\n', encoding='utf-8')
    cfg = tmp_path / 'cfg.json'
    cfg.write_text(json.dumps(
        {'share_roots': [{'name': 'app', 'path': str(root)}]}), encoding='utf-8')
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(cfg))
    pj._freshness.clear()
    yield {'root': root, 'cfg': cfg, 'tmp': tmp_path}
    pj._freshness.clear()


def _write_cfg(cfg_path, roots):
    cfg_path.write_text(json.dumps({'share_roots': roots}), encoding='utf-8')


# ═══════════════════════════════════════════════════════════
#  路径校验(约束⑤)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPathValidation:
    def test_no_roots_declared_refuses_everything(self, proj):
        _write_cfg(proj['cfg'], [])
        out = pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        assert 'error' in out and 'share_roots' in out['error']

    def test_unknown_root_name_refused(self, proj):
        out = pj.cmd_project_read_files({'root': 'nope', 'path': 'README.md'})
        assert 'error' in out and 'nope' in out['error']

    def test_dotdot_escape_refused(self, proj):
        out = pj.cmd_project_read_files({'root': 'app', 'path': '../cfg.json'})
        assert 'error' in out

    def test_absolute_path_refused(self, proj):
        out = pj.cmd_project_read_files(
            {'root': 'app', 'path': str(proj['root'] / 'README.md')})
        assert 'error' in out and 'root-relative' in out['error']

    def test_sibling_prefix_attack_refused(self, proj):
        # root=/tmp/.../app; /tmp/.../app2 must NOT count as inside
        (proj['tmp'] / 'app2').mkdir()
        (proj['tmp'] / 'app2' / 'x.txt').write_text('x', encoding='utf-8')
        out = pj.cmd_project_read_files({'root': 'app', 'path': '../app2/x.txt'})
        assert 'error' in out

    def test_symlink_escape_refused(self, proj):
        outside = proj['tmp'] / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('top secret', encoding='utf-8')
        os.symlink(str(outside), str(proj['root'] / 'link'))
        out = pj.cmd_project_read_files(
            {'root': 'app', 'path': 'link/secret.txt'})
        assert 'error' in out

    def test_is_within_case_insensitive_mode(self):
        assert pj._is_within('/A/B', '/a/b/c', case_insensitive=True) is True
        assert pj._is_within('/A/B', '/a/bc', case_insensitive=True) is False

    def test_is_within_drive_mismatch_returns_false(self, monkeypatch):
        monkeypatch.setattr(pj.os.path, 'commonpath',
                            lambda _paths: (_ for _ in ()).throw(ValueError('drives')))
        assert pj._is_within('C:/a', 'D:/b') is False


# ═══════════════════════════════════════════════════════════
#  list / read(读即盖章)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestReadSide:
    def test_list_dir_structured_and_hides_tofu(self, proj):
        pj.cmd_project_write_file({'root': 'app', 'path': 'new.txt',
                                   'content': 'v1'})  # creates .tofu snapshots dir on later writes
        out = pj.cmd_project_list_dir({'root': 'app', 'path': '.'})
        names = [e['name'] for e in out['entries']]
        assert 'README.md' in names and 'src' in names and 'new.txt' in names
        assert '.tofu' not in names

    def test_shell_compatible_list_keeps_ignored_hidden_and_symlink(self, proj):
        (proj['root'] / 'node_modules').mkdir()
        (proj['root'] / '.hidden.txt').write_text('hidden', encoding='utf-8')
        (proj['root'] / 'readme-link').symlink_to(
            proj['root'] / 'README.md')
        out = pj.cmd_project_list_dir({
            'root': 'app',
            'path': '.',
            'show_hidden': True,
            'shell_compatible': True,
        })
        by_name = {entry['name']: entry for entry in out['entries']}
        assert 'node_modules' in by_name
        assert '.hidden.txt' in by_name
        assert by_name['readme-link']['type'] == 'symlink'

    def test_read_text_stamps_freshness(self, proj):
        out = pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        assert out['content'].startswith('# app')
        assert out['truncated'] is False
        assert str(proj['root'] / 'README.md') in pj._freshness

    def test_read_image_returns_base64(self, proj):
        (proj['root'] / 'pic.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 32)
        out = pj.cmd_project_read_files({'root': 'app', 'path': 'pic.png'})
        assert 'base64' in out and out['media'] == '.png'
        assert str(proj['root'] / 'pic.png') in pj._freshness

    def test_read_missing_file_errors(self, proj):
        out = pj.cmd_project_read_files({'root': 'app', 'path': 'nope.txt'})
        assert 'error' in out


# ═══════════════════════════════════════════════════════════
#  write / apply_diff:快照 + freshness 门(约束③)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestWriteSafetyNet:
    def test_create_new_file_needs_no_token(self, proj):
        out = pj.cmd_project_write_file({'root': 'app', 'path': 'new/a.txt',
                                         'content': 'fresh'})
        assert 'error' not in out
        assert (proj['root'] / 'new' / 'a.txt').read_text() == 'fresh'

    def test_existing_file_requires_read_first(self, proj):
        out = pj.cmd_project_write_file({'root': 'app', 'path': 'README.md',
                                         'content': 'overwrite'})
        assert 'error' in out and 'read' in out['error'].lower()

    def test_read_then_write_ok_and_snapshots(self, proj):
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out = pj.cmd_project_write_file({'root': 'app', 'path': 'README.md',
                                         'content': 'v2'})
        assert 'error' not in out and out.get('snapshot')
        snap = out['snapshot']
        assert os.path.isfile(snap)
        assert open(snap, encoding='utf-8').read() == '# app\nhello world\n'
        # 快照路径契约:<root>/.tofu/file-history/<md5>/<epoch>
        assert f'{os.sep}.tofu{os.sep}file-history{os.sep}' in snap

    def test_external_modification_refused_then_reread_allows(self, proj):
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        # 外部(IDE/用户)改动 — 长度不同,粗粒度 mtime 也必变 size
        (proj['root'] / 'README.md').write_text('# app\nhello world\nEXTERNALLY EDITED\n',
                                                encoding='utf-8')
        out = pj.cmd_project_write_file({'root': 'app', 'path': 'README.md',
                                         'content': 'v2'})
        assert 'error' in out and 'changed on disk' in out['error']
        # 重读刷新令牌 → 放行
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out2 = pj.cmd_project_write_file({'root': 'app', 'path': 'README.md',
                                          'content': 'v2'})
        assert 'error' not in out2

    def test_own_write_rearms_token(self, proj):
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        pj.cmd_project_write_file({'root': 'app', 'path': 'README.md',
                                   'content': 'v2'})
        out = pj.cmd_project_write_file({'root': 'app', 'path': 'README.md',
                                         'content': 'v3'})
        assert 'error' not in out

    def test_apply_diff_single_match(self, proj):
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out = pj.cmd_project_apply_diff({
            'root': 'app', 'path': 'README.md',
            'search': 'hello world', 'replace': 'hi there'})
        assert out.get('replacements') == 1
        assert 'hi there' in (proj['root'] / 'README.md').read_text()

    def test_apply_diff_zero_match(self, proj):
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out = pj.cmd_project_apply_diff({
            'root': 'app', 'path': 'README.md',
            'search': 'not present', 'replace': 'x'})
        assert 'error' in out and 'not found' in out['error']

    def test_apply_diff_ambiguous_requires_replace_all(self, proj):
        (proj['root'] / 'dup.txt').write_text('aa bb aa', encoding='utf-8')
        pj.cmd_project_read_files({'root': 'app', 'path': 'dup.txt'})
        out = pj.cmd_project_apply_diff({
            'root': 'app', 'path': 'dup.txt', 'search': 'aa', 'replace': 'zz'})
        assert 'error' in out and '2' in out['error']
        out2 = pj.cmd_project_apply_diff({
            'root': 'app', 'path': 'dup.txt', 'search': 'aa',
            'replace': 'zz', 'replace_all': True})
        assert out2.get('replacements') == 2

    def test_edit_file_mixed_batch(self, proj):
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out = pj.cmd_project_edit_file({
            'root': 'app',
            'edits': [
                {'path': 'README.md', 'operation': 'insert_after',
                 'anchor': '# app', 'content': 'inserted'},
                {'path': 'README.md', 'operation': 'replace',
                 'anchor': 'hello world', 'content': 'hello remote'},
            ],
        })
        assert out.startswith('Applied 2/2 edits')
        assert (proj['root'] / 'README.md').read_text() == (
            '# app\ninserted\nhello remote\n')

    @pytest.mark.parametrize(('operation', 'replace_all', 'expected'), [
        ('insert_after', False, '# app\ninserted\nhello world\n'),
        ('insert_after', True, '# app\ninserted\nhello world\n'),
        ('insert_before', False, 'inserted\n# app\nhello world\n'),
        ('insert_before', True, 'inserted\n# app\nhello world\n'),
    ])
    def test_edit_file_insert_ignores_replace_all_for_unique_anchor(
            self, proj, operation, replace_all, expected):
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out = pj.cmd_project_edit_file({
            'root': 'app',
            'edits': [{
                'path': 'README.md', 'operation': operation,
                'anchor': '# app', 'content': 'inserted',
                'replace_all': replace_all,
            }],
        })
        assert out.startswith('Applied 1/1 edits')
        assert (proj['root'] / 'README.md').read_text() == expected

    def test_edit_file_replace_all_does_not_enable_ambiguous_insert(self, proj):
        target = proj['root'] / 'README.md'
        target.write_text('# app\n# app\n', encoding='utf-8')
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out = pj.cmd_project_edit_file({
            'root': 'app',
            'edits': [{
                'path': 'README.md', 'operation': 'insert_after',
                'anchor': '# app', 'content': 'inserted',
                'replace_all': True,
            }],
        })
        assert out.startswith('Applied 0/1 edits (1 failed)')
        assert 'matches 2 locations' in out
        assert target.read_text(encoding='utf-8') == '# app\n# app\n'

    def test_edit_file_wrap_replace_rejected_pre_execution(self, proj):
        """Parity with the server-side tool_edit_file wrap gate: a replace
        whose content keeps the anchor verbatim at a boundary is a pure
        insertion and must be refused BEFORE the write lands."""
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out = pj.cmd_project_edit_file({
            'root': 'app',
            'edits': [
                {'path': 'README.md', 'operation': 'replace',
                 'anchor': 'hello world', 'content': 'hello world\nappended'},
                {'path': 'README.md', 'operation': 'insert_after',
                 'anchor': '# app', 'content': 'inserted'},
            ],
        })
        assert out.startswith('Applied 1/2 edits (1 failed)')
        assert 'pure insertion rejected' in out
        assert "operation='insert_after'" in out
        # the rejected edit never landed; the sibling insert did
        assert (proj['root'] / 'README.md').read_text() == (
            '# app\ninserted\nhello world\n')

    def test_edit_file_wrap_gate_kill_switch(self, proj, monkeypatch):
        monkeypatch.setenv('TOFU_EDIT_WRAP_GATE', '0')
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out = pj.cmd_project_edit_file({
            'root': 'app',
            'edits': [
                {'path': 'README.md', 'operation': 'replace',
                 'anchor': 'hello world', 'content': 'hello world\nappended'},
            ],
        })
        assert out.startswith('Applied 1/1 edits')
        assert 'appended' in (proj['root'] / 'README.md').read_text()

    def test_edit_file_neighbour_echo_auto_repaired(self, proj):
        """Desktop parity for the mswlvsfgzwiywr incident: an insert_after
        whose content quotes the following def line is repaired locally."""
        (proj['root'] / 'src' / 'sample.py').write_text(
            'def test_previous():\n'
            '    assert True\n'
            '\n\n'
            'def test_existing():\n'
            "    assert 'x' in 'x'\n", encoding='utf-8')
        pj.cmd_project_read_files({'root': 'app', 'path': 'src/sample.py'})
        out = pj.cmd_project_edit_file({
            'root': 'app',
            'edits': [
                {'path': 'src/sample.py', 'operation': 'insert_after',
                 'anchor': '    assert True',
                 'content': 'def test_existing():\n'
                            'def test_new():\n'
                            "    assert 'y' in 'y'\n"},
            ],
        })
        assert out.startswith('Applied 1/1 edits')
        assert 'auto-repaired' in out
        text = (proj['root'] / 'src' / 'sample.py').read_text(encoding='utf-8')
        assert text.count('def test_existing():') == 1
        assert 'def test_new():' in text
        import ast
        ast.parse(text)

    def test_edit_file_anchor_echo_stripped(self, proj):
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out = pj.cmd_project_edit_file({
            'root': 'app',
            'edits': [
                {'path': 'README.md', 'operation': 'insert_after',
                 'anchor': '# app', 'content': '# app\ninserted'},
            ],
        })
        assert out.startswith('Applied 1/1 edits')
        assert 'auto-repaired' in out
        assert (proj['root'] / 'README.md').read_text(encoding='utf-8') == (
            '# app\ninserted\nhello world\n')

    def test_edit_file_whole_echo_fails(self, proj):
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        out = pj.cmd_project_edit_file({
            'root': 'app',
            'edits': [
                {'path': 'README.md', 'operation': 'insert_after',
                 'anchor': '# app', 'content': 'hello world'},
            ],
        })
        assert out.startswith('Applied 0/1 edits (1 failed)')
        assert 'verbatim copy' in out
        assert (proj['root'] / 'README.md').read_text(encoding='utf-8') == (
            '# app\nhello world\n')


# ═══════════════════════════════════════════════════════════
#  grep / find(复用 lib/project_mod,ignore 规则含 .tofu)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSearchSide:
    def test_grep_finds_and_skips_ignored_dirs(self, proj):
        (proj['root'] / 'node_modules').mkdir()
        (proj['root'] / 'node_modules' / 'dep.js').write_text(
            'hello world', encoding='utf-8')
        out = pj.cmd_project_grep_search({'root': 'app', 'pattern': 'hello world'})
        assert 'README.md' in out['matches']
        assert 'node_modules' not in out['matches']

    def test_grep_skips_tofu_snapshots(self, proj):
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        pj.cmd_project_write_file({'root': 'app', 'path': 'README.md',
                                   'content': 'changed'})
        out = pj.cmd_project_grep_search({'root': 'app', 'pattern': 'hello world'})
        assert '.tofu' not in out['matches']

    def test_find_files_glob(self, proj):
        out = pj.cmd_project_find_files({'root': 'app', 'pattern': '*.py'})
        assert 'main.py' in out['files']

    def test_find_shell_output_preserves_hidden_and_ignore_semantics(self, proj):
        (proj['root'] / '.hidden.py').write_text('x', encoding='utf-8')
        (proj['root'] / 'node_modules').mkdir()
        (proj['root'] / 'node_modules' / 'dep.py').write_text(
            'x', encoding='utf-8')
        out = pj.cmd_project_find_files({
            'root': 'app',
            'path': '.',
            'pattern': '*.py',
            'case_sensitive': True,
            'shell_output': True,
            'respect_project_ignores': False,
        })
        assert './.hidden.py' in out['files']
        assert './node_modules/dep.py' in out['files']

    def test_grep_requires_pattern(self, proj):
        out = pj.cmd_project_grep_search({'root': 'app', 'pattern': ''})
        assert 'error' in out


# ═══════════════════════════════════════════════════════════
#  run_command(基础平价;流式/kill 属 P2)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRunCommand:
    def test_runs_inside_root(self, proj):
        out = pj.cmd_project_run_command(
            {'root': 'app', 'command': 'cat README.md'})
        assert out.get('exit_code') == 0
        assert 'hello world' in out.get('stdout', '')

    def test_dangerous_command_blocked(self, proj):
        out = pj.cmd_project_run_command(
            {'root': 'app', 'command': 'shutdown -h now'})
        assert 'error' in out and 'blocked' in out['error']

    def test_catastrophic_delete_blocked(self, proj):
        out = pj.cmd_project_run_command({'root': 'app', 'command': 'rm -rf /'})
        assert 'error' in out and 'blocked' in out['error']

    def test_workdir_escape_refused(self, proj):
        out = pj.cmd_project_run_command(
            {'root': 'app', 'command': 'ls', 'workdir': '..'})
        assert 'error' in out

    def test_timeout_param_honored(self, proj):
        out = pj.cmd_project_run_command(
            {'root': 'app', 'command': 'sleep 2', 'timeout': 1})
        assert 'error' in out and 'timed out' in out['error']


# ═══════════════════════════════════════════════════════════
#  调度表 + 权限分层(约束①:键 = wire type 逐字)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDispatchIntegration:
    PROJECT_COMMANDS = {
        'project_list_dir', 'project_read_files', 'project_write_file',
        'project_apply_diff', 'project_grep_search', 'project_find_files',
        'project_run_command',
    }

    def test_commands_table_has_all_sever(self):
        assert self.PROJECT_COMMANDS <= set(disp.COMMANDS)

    def test_permission_gates(self, proj):
        perms = {'allow_write': False, 'allow_exec': False, 'allow_gui': False}
        out = disp.dispatch_command('project_write_file', {
            'root': 'app', 'path': 'x.txt', 'content': 'y'}, perms)
        assert 'allow-write' in out['error']
        out = disp.dispatch_command('project_run_command', {
            'root': 'app', 'command': 'echo hi'}, perms)
        assert 'allow-exec' in out['error']
        # 读族无门:全 False 权限也可用
        out = disp.dispatch_command('project_read_files', {
            'root': 'app', 'path': 'README.md'}, perms)
        assert 'content' in out

    def test_write_family_actually_writes_with_perm(self, proj):
        perms = {'allow_write': True, 'allow_exec': True, 'allow_gui': False}
        out = disp.dispatch_command('project_write_file', {
            'root': 'app', 'path': 'perm.txt', 'content': 'ok'}, perms)
        assert 'error' not in out


# ═══════════════════════════════════════════════════════════
#  NEUTER — 两道闸承重证明
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNeuters:
    def test_neuter_freshness_gate_allows_stale_write(self, proj, monkeypatch):
        """剥掉 freshness 门 → 外部改动后裸写成功 = 门承重."""
        monkeypatch.setattr(pj, '_check_write_allowed', lambda _p: None)
        pj.cmd_project_read_files({'root': 'app', 'path': 'README.md'})
        (proj['root'] / 'README.md').write_text('EXTERNALLY EDITED — longer',
                                                encoding='utf-8')
        out = pj.cmd_project_write_file({'root': 'app', 'path': 'README.md',
                                         'content': 'stale overwrite'})
        assert 'error' not in out  # 坏结果:门不在就会放过去

    def test_neuter_path_validation_allows_escape(self, proj, monkeypatch):
        """剥掉路径校验 → '..' 写出根外 = 校验承重."""
        root = proj['root']
        monkeypatch.setattr(pj, '_resolve',
                            lambda _root, rel, roots=None: (str(root), str(root / rel)))
        out = pj.cmd_project_write_file({'root': 'app', 'path': '../evil.txt',
                                         'content': 'escaped'})
        assert 'error' not in out
        assert (proj['tmp'] / 'evil.txt').exists()  # 坏结果:写到了根外


# ═══════════════════════════════════════════════════════════
#  注册帧带 share_roots + 服务端注册表存根
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestShareRootsRegistration:
    def test_agent_frame_carries_share_roots(self, proj, monkeypatch):
        import lib.desktop_agent._run as ar

        class _Resp:
            status_code = 200

            def json(self):
                return {'commands': []}

        stop = threading.Event()
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
            captured['body'] = json
            stop.set()
            return _Resp()

        monkeypatch.setattr(ar.requests, 'post', fake_post)
        ar.run_agent('http://server.example',
                     {'allow_write': True}, poll_interval=0.01, stop_event=stop)
        roots = captured['body']['agent']['share_roots']
        assert roots == [{'name': 'app', 'path': str(proj['root'])}]

    def test_server_registry_stores_share_roots(self):
        from lib.desktop import bridge as db
        with db.command_queue_lock:
            db._agents.clear()
        db.register_agent('agent-X', {
            'name': 'mac', 'share_roots': [{'name': 'app', 'path': '/code/app'}]})
        agent = db.online_agents()[0]
        assert agent['share_roots'] == [{'name': 'app', 'path': '/code/app'}]
        with db.command_queue_lock:
            db._agents.clear()
