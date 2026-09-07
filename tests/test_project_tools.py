"""Unit tests for project-mode tool helpers.

Migrated from debug/test_log_cleanup.py and debug/test_write_targets.py.
Tests _clean_command_output (progress bar compression, device dedup)
and _extract_write_targets / _filter_changes_by_targets (command analysis).
"""

import pytest

from lib.project_mod.tools import (
    _clean_command_output,
    _extract_write_targets,
    _filter_changes_by_targets,
    _is_catastrophic_delete,
    _is_destructive_command,
    _maybe_wrap_rm_with_trash,
    execute_standalone_command,
    parse_safe_directory_listing_command,
    parse_safe_file_find_command,
    tool_list_dir,
)


@pytest.mark.unit
def test_plain_ls_parser_accepts_only_single_bounded_directory_reads():
    assert parse_safe_directory_listing_command('ls') == {
        'path': '.', 'show_hidden': False}
    assert parse_safe_directory_listing_command('ls -lah src') == {
        'path': 'src', 'show_hidden': True}
    assert parse_safe_directory_listing_command('/bin/ls --color=auto -- lib') == {
        'path': 'lib', 'show_hidden': False}
    assert parse_safe_directory_listing_command("ls 'folder name'") == {
        'path': 'folder name', 'show_hidden': False}
    for command in (
            'ls -R', 'ls ../outside', 'ls one two', 'ls | head',
            'ls *.py', 'ls; touch changed', 'ls $HOME', 'ls # comment',
            './ls', '/tmp/ls'):
        assert parse_safe_directory_listing_command(command) is None


@pytest.mark.unit
def test_standalone_run_command_plain_ls_uses_bounded_directory_reader(tmp_path):
    (tmp_path / 'sample.py').write_text('print(1)\n', encoding='utf-8')
    (tmp_path / 'pkg').mkdir()
    result = execute_standalone_command(
        'run_command', {'command': 'ls -lh .'}, working_dir=str(tmp_path))
    assert result.startswith('$ ls -lh .\nDirectory: .')
    assert 'sample.py' in result and 'pkg/' in result
    assert result.rstrip().endswith('[exit code: 0]')

    file_result = execute_standalone_command(
        'run_command', {'command': 'ls sample.py'},
        working_dir=str(tmp_path))
    assert 'sample.py' in file_result.splitlines()
    assert '[exit code: 0]' in file_result


@pytest.mark.unit
def test_standalone_ls_fast_path_keeps_shell_visible_entries(tmp_path):
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / '.hidden.txt').write_text('hidden', encoding='utf-8')
    (tmp_path / 'target.txt').write_text('target', encoding='utf-8')
    (tmp_path / 'link.txt').symlink_to(tmp_path / 'target.txt')

    normal = execute_standalone_command(
        'run_command', {'command': 'ls .'}, working_dir=str(tmp_path))
    assert normal.startswith('$ ls .\n')
    assert normal.rstrip().endswith('[exit code: 0]')
    assert 'node_modules/' in normal
    assert 'link.txt [symlink]' in normal
    assert '.hidden.txt' not in normal

    hidden = execute_standalone_command(
        'run_command', {'command': 'ls -a .'}, working_dir=str(tmp_path))
    assert '.hidden.txt' in hidden


@pytest.mark.unit
def test_list_dir_backend_reports_bounded_scan(monkeypatch, tmp_path):
    import lib.project_mod.read_tools as read_tools
    for index in range(8):
        (tmp_path / f'{index}.txt').write_text('x', encoding='utf-8')
    monkeypatch.setattr(read_tools, '_LIST_DIR_SCAN_LIMIT', 3)
    monkeypatch.setattr(read_tools, '_LIST_DIR_ENTRY_LIMIT', 2)
    result = tool_list_dir(str(tmp_path), '.', shell_compatible=True)
    assert 'listing truncated' in result
    assert 'returned 2' in result

    monkeypatch.setattr(read_tools, '_LIST_DIR_SCAN_LIMIT', 20)
    monkeypatch.setattr(read_tools, '_LIST_DIR_ENTRY_LIMIT', 20)
    monkeypatch.setattr(read_tools, '_LIST_DIR_OUTPUT_CHAR_LIMIT', 8)
    char_bounded = tool_list_dir(
        str(tmp_path), '.', shell_compatible=True)
    assert 'listing truncated' in char_bounded
    assert 'returned 0' in char_bounded


@pytest.mark.unit
def test_safe_find_parser_accepts_only_index_compatible_read_shapes():
    assert parse_safe_file_find_command(
        "find . -type f -name '*.py'") == {
            'path': '.', 'pattern': '*.py', 'case_sensitive': True,
            'max_results': 500,
        }
    assert parse_safe_file_find_command(
        'find src -iname "*.PY" -type f -print') == {
            'path': 'src', 'pattern': '*.PY', 'case_sensitive': False,
            'max_results': 500,
        }
    assert parse_safe_file_find_command('find -type f') == {
        'path': '.', 'pattern': '*', 'case_sensitive': False,
        'max_results': 500,
    }
    for command in (
            "find . -name '*.py'", "find . -type d -name '*.py'",
            "find . -type f -name 'src/*.py'", 'find ../outside -type f',
            "find . -type f -exec echo {} \\;", 'find . -type f | head',
            'find . -type f -delete', 'find . -type f -name *.py',
            'find . -type f # comment', './find . -type f',
            '/tmp/find . -type f'):
        assert parse_safe_file_find_command(command) is None


@pytest.mark.unit
def test_standalone_find_fast_path_preserves_hidden_ignore_and_case(tmp_path):
    (tmp_path / 'node_modules').mkdir()
    for rel in ('lower.py', 'UPPER.PY', '.hidden.py',
                'node_modules/dependency.py'):
        (tmp_path / rel).write_text('x', encoding='utf-8')

    sensitive = execute_standalone_command(
        'run_command',
        {'command': "find . -type f -name '*.py'"},
        working_dir=str(tmp_path),
    )
    assert sensitive.startswith("$ find . -type f -name '*.py'\n")
    assert sensitive.rstrip().endswith('[exit code: 0]')
    assert './lower.py' in sensitive
    assert './.hidden.py' in sensitive
    assert './node_modules/dependency.py' in sensitive
    assert 'UPPER.PY' not in sensitive

    insensitive = execute_standalone_command(
        'run_command',
        {'command': "find . -type f -iname '*.py'"},
        working_dir=str(tmp_path),
    )
    assert './UPPER.PY' in insensitive

    file_operand = execute_standalone_command(
        'run_command',
        {'command': 'find lower.py -type f'},
        working_dir=str(tmp_path),
    )
@pytest.mark.unit
def test_run_command_fast_path_meta_classifies_as_executed(tmp_path):
    """ls/find fast-paths must not look like a pre-execution refusal.

    Regression pin for the "not run" misclassification: the bounded-reader
    result used to go out without the [exit code: N] marker, and both
    lib/tools/meta.py and the task handler read a missing marker as
    "refused/blocked before it ran" — a successful listing rendered as an
    amber not-run card with the full payload pinned open as the reason.
    """
    from lib.tools.meta import build_project_tool_meta

    (tmp_path / 'sample.py').write_text('print(1)\n', encoding='utf-8')
    content = execute_standalone_command(
        'run_command', {'command': 'ls .'}, working_dir=str(tmp_path))
    meta = build_project_tool_meta(
        'run_command', {'command': 'ls .'}, content)
    assert meta['exitCode'] == '0'
    assert meta.get('notRun') is not True
    assert meta['badge'] == 'done'
    assert 'sample.py' in meta['output']

    find_content = execute_standalone_command(
        'run_command', {'command': "find . -type f -name '*.py'"},
        working_dir=str(tmp_path))
    find_meta = build_project_tool_meta(
        'run_command', {'command': "find . -type f -name '*.py'"},
        find_content)
    assert find_meta['exitCode'] == '0'
    assert find_meta.get('notRun') is not True
    assert './sample.py' in find_meta['output']


@pytest.mark.unit
def test_fast_path_command_result_marks_reader_failures_nonzero():
    from lib.project_mod.tools import _fast_path_command_result

    for failure in ('Unable to list directory: x',
                    'Not a directory: x',
                    "find: 'x': No such directory"):
        assert _fast_path_command_result('ls x', failure).rstrip().endswith(
            '[exit code: 1]'), failure
    ok = _fast_path_command_result(
        'ls .', 'Directory: .\n\nFiles:\n  a.py (1L, 2B)')
    assert ok == '$ ls .\nDirectory: .\n\nFiles:\n  a.py (1L, 2B)\n\n[exit code: 0]'
    # find with no matches prints nothing and still exits 0.
    assert _fast_path_command_result('find .', '') == (
        '$ find .\n\n[exit code: 0]')


@pytest.mark.unit
def test_fast_path_gate_declines_unreadable_directory(tmp_path, monkeypatch):
    """A permission-denied target keeps the real shell (truthful exit/stderr)."""
    import os

    from lib.project_mod import tools as project_tools

    monkeypatch.setattr(os, 'access', lambda _path, _mode: False)
    assert project_tools._bounded_directory_fast_path_available(
        str(tmp_path), '.') is False


# ═══════════════════════════════════════════════════════════
#  _clean_command_output — progress bar compression
# ═══════════════════════════════════════════════════════════

SAMPLE_MULTI_DEVICE_PROGRESS = r"""Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 0] Starting on cuda:0, processing 5021 samples

Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 7] Starting on cuda:7, processing 5021 samples

Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 4] Starting on cuda:4, processing 5021 samples

Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 2] Starting on cuda:2, processing 5021 samples

Loading weights:   3%|▎         | 9/299 [00:00<00:04, 64.97it/s]
Loading weights:   3%|▎         | 9/299 [00:00<00:03, 79.13it/s]
Loading weights:  17%|█▋        | 50/299 [00:00<00:03, 76.75it/s]
Loading weights:  17%|█▋        | 50/299 [00:00<00:03, 72.40it/s]
Loading weights:  16%|█▋        | 49/299 [00:00<00:03, 63.25it/s]"""

SAMPLE_SINGLE_PROGRESS = """
Downloading model:   0%|          | 0/100 [00:00<?, ?it/s]
Downloading model:  10%|█         | 10/100 [00:02<00:18, 5.00it/s]
Downloading model:  50%|█████     | 50/100 [00:10<00:10, 5.00it/s]
Downloading model: 100%|██████████| 100/100 [00:20<00:00, 5.00it/s]
Done!
"""

SAMPLE_MULTI_DEVICE_STARTUP = """[Worker 0] Starting on cuda:0, processing 5021 samples
[Worker 1] Starting on cuda:1, processing 5021 samples
[Worker 2] Starting on cuda:2, processing 5021 samples
[Worker 3] Starting on cuda:3, processing 5021 samples
[Worker 4] Starting on cuda:4, processing 5021 samples
[Worker 5] Starting on cuda:5, processing 5021 samples
[Worker 6] Starting on cuda:6, processing 5021 samples
[Worker 7] Starting on cuda:7, processing 5021 samples
"""


# ═══════════════════════════════════════════════════════════
#  rg/grep 命令构建:IGNORE_DIRS 排除必须完整且确定
#  潜伏 bug(RWA P3 批次抓获):_build_rg_cmd/_build_grep_cmd 用
#  `list(IGNORE_DIRS)[:30]` —— IGNORE_DIRS 是无序 set,一旦超过 30 条
#  (2026-07 已 34+),每个进程按哈希种子随机丢掉几条排除项,
#  node_modules 等会以 ~40% 概率重新出现在 grep 结果里。
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestGrepIgnoreDirsComplete:
    def test_rg_cmd_excludes_every_ignore_dir(self):
        from lib.project_mod.config import IGNORE_DIRS
        from lib.project_mod.read_tools import _build_rg_cmd
        cmd = _build_rg_cmd('/base', '/base', 'pat', None, 0)
        excluded = {cmd[i + 1].lstrip('!').rstrip('/')
                    for i, a in enumerate(cmd) if a == '-g' and i + 1 < len(cmd)}
        missing = IGNORE_DIRS - excluded
        assert not missing, f'rg 命令丢了 {len(missing)} 条排除项: {sorted(missing)[:5]}'

    def test_grep_cmd_excludes_every_ignore_dir(self):
        from lib.project_mod.config import IGNORE_DIRS
        from lib.project_mod.read_tools import _build_grep_cmd
        cmd = _build_grep_cmd('/base', '/base', 'pat', None, 0)
        excluded = {cmd[i + 1]
                    for i, a in enumerate(cmd) if a == '--exclude-dir' and i + 1 < len(cmd)}
        missing = IGNORE_DIRS - excluded
        assert not missing, f'grep 命令丢了 {len(missing)} 条排除项: {sorted(missing)[:5]}'

    def test_exclusion_order_deterministic(self):
        # argv 顺序固定(sorted),日志/diff/缓存才可读。
        from lib.project_mod.read_tools import _build_rg_cmd
        a = _build_rg_cmd('/base', '/base', 'pat', None, 0)
        b = _build_rg_cmd('/base', '/base', 'pat', None, 0)
        assert a == b


@pytest.mark.unit
class TestCleanCommandOutput:
    def test_multi_device_progress_compresses(self):
        result = _clean_command_output(SAMPLE_MULTI_DEVICE_PROGRESS)
        lines = result.strip().split('\n')
        assert len(lines) < 15, f'Expected < 15 lines, got {len(lines)}'

    def test_multi_device_shows_device_count(self):
        result = _clean_command_output(SAMPLE_MULTI_DEVICE_PROGRESS)
        assert '×' in result and 'device' in result

    def test_multi_device_includes_start_and_end_progress(self):
        result = _clean_command_output(SAMPLE_MULTI_DEVICE_PROGRESS)
        assert '0%' in result

    def test_single_device_preserves_endpoints(self):
        result = _clean_command_output(SAMPLE_SINGLE_PROGRESS)
        assert '0%' in result
        assert '100%' in result
        assert 'Done!' in result

    def test_multi_device_startup_collapsed(self):
        result = _clean_command_output(SAMPLE_MULTI_DEVICE_STARTUP)
        lines = result.strip().split('\n')
        # ≤4: first line + fold marker + the deliberate summary footer
        # ([output folded: N of M lines omitted …]) the renderer now appends.
        assert len(lines) <= 4, f'Expected <= 4 lines, got {len(lines)}'
        assert 'cuda:0-7' in result


# ═══════════════════════════════════════════════════════════
#  _extract_write_targets — command write target analysis
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestExtractWriteTargets:
    """Test command write-target extraction."""

    # Read-only commands → empty set
    def test_cat_grep_tail_readonly(self):
        t = _extract_write_targets('cat logs/postgresql.log 2>/dev/null | grep -i error | tail -40')
        assert t == set()

    def test_grep_readonly(self):
        assert _extract_write_targets('grep -r TODO src/') == set()

    def test_ls_readonly(self):
        assert _extract_write_targets('ls -la') == set()

    def test_find_wc_readonly(self):
        assert _extract_write_targets('find . -name "*.py" | wc -l') == set()

    def test_git_status_readonly(self):
        assert _extract_write_targets('git status') == set()

    def test_diff_readonly(self):
        assert _extract_write_targets('diff file1.py file2.py') == set()

    # Redirect targets
    def test_redirect_output(self):
        t = _extract_write_targets('echo hello > output.txt')
        assert 'output.txt' in (t or set())

    def test_redirect_append(self):
        t = _extract_write_targets('cat a.txt >> log.txt')
        assert 'log.txt' in (t or set())

    def test_redirect_with_stderr_null(self):
        t = _extract_write_targets('sort data.csv > sorted.csv 2>/dev/null')
        assert t is not None and 'sorted.csv' in t and '/dev/null' not in str(t)

    # rm targets
    def test_rm_files(self):
        t = _extract_write_targets('rm file1.txt file2.txt')
        assert t is not None and 'file1.txt' in t and 'file2.txt' in t

    def test_rm_rf_dir(self):
        t = _extract_write_targets('rm -rf build/')
        assert t is not None and 'build/' in t

    # cp → dest only
    def test_cp_dest_only(self):
        t = _extract_write_targets('cp src.txt dst.txt')
        assert t is not None and 'dst.txt' in t and 'src.txt' not in t

    # mv → both
    def test_mv_both(self):
        t = _extract_write_targets('mv old.txt new.txt')
        assert t is not None and 'new.txt' in t and 'old.txt' in t

    # touch
    def test_touch(self):
        t = _extract_write_targets('touch new_file.py')
        assert t is not None and 'new_file.py' in t

    # sed -i
    def test_sed_i(self):
        t = _extract_write_targets("sed -i 's/old/new/g' file1.py file2.py")
        assert t is not None and 'file1.py' in t and 'file2.py' in t

    # gawk -i inplace (in-place extension) — mirrors sed -i coverage
    def test_gawk_i_inplace(self):
        t = _extract_write_targets("gawk -i inplace '{print $1}' file1.txt file2.txt")
        assert t is not None and 'file1.txt' in t and 'file2.txt' in t

    def test_gawk_i_inplace_glued(self):
        t = _extract_write_targets("gawk -iinplace '{print $1}' data.csv")
        assert t is not None and 'data.csv' in t

    def test_gawk_include_inplace(self):
        t = _extract_write_targets("gawk --include=inplace '{print}' a.log")
        assert t is not None and 'a.log' in t

    def test_gawk_inplace_skips_program_and_options(self):
        # -F / -v consume values; the program text and assignments are
        # not file targets. Junk over-inclusion is acceptable, missing
        # the real file is not.
        t = _extract_write_targets(
            "gawk -F: -v thresh=9 -i inplace '{print $2}' thresh=8 /etc/passwd")
        assert t is not None and '/etc/passwd' in t

    def test_gawk_inplace_after_pipeline_isolated(self):
        t = _extract_write_targets(
            "cat in.txt | gawk -i inplace '{print}' out.txt")
        assert t is not None and 'out.txt' in t

    # awk without -i inplace is a pure filter (read-only)
    def test_awk_without_inplace_readonly(self):
        assert _extract_write_targets("awk '{print $1}' input.txt") == set()

    def test_awk_pipeline_readonly(self):
        assert _extract_write_targets("ps aux | awk '{print $2}'") == set()

    # Opaque commands → None
    def test_python_opaque(self):
        assert _extract_write_targets('python3 script.py') is None

    def test_make_opaque(self):
        assert _extract_write_targets('make build') is None

    def test_npm_opaque(self):
        assert _extract_write_targets('npm install') is None

    # /dev/null should not pollute targets
    def test_devnull_excluded(self):
        assert _extract_write_targets('ls 2>/dev/null') == set()
        assert _extract_write_targets('echo test > /dev/null') == set()

    # sed without -i is read-only
    def test_sed_without_i_readonly(self):
        assert _extract_write_targets("sed 's/foo/bar/g' input.txt") == set()

    # Quoted args with special chars
    def test_quoted_pipe_in_grep(self):
        t = _extract_write_targets('grep -i "error\\|warning\\|fatal" app.log | tail -20')
        assert t == set()


@pytest.mark.unit
class TestIsDestructiveCommand:
    def test_echo_not_destructive(self):
        assert not _is_destructive_command('echo hello')

    def test_ls_not_destructive(self):
        assert not _is_destructive_command('ls -la')

    def test_rm_rf_destructive(self):
        assert _is_destructive_command('rm -rf /tmp/foo')

    def test_python_destructive(self):
        assert _is_destructive_command('python script.py')

    def test_git_status_not_destructive(self):
        assert not _is_destructive_command('git status')

    def test_git_checkout_destructive(self):
        assert _is_destructive_command('git checkout main')

    def test_sed_i_destructive(self):
        assert _is_destructive_command("sed -i 's/foo/bar/' file.py")

    def test_sed_without_i_not_destructive(self):
        assert not _is_destructive_command("sed 's/foo/bar/g' input.txt")

    def test_gawk_i_inplace_destructive(self):
        assert _is_destructive_command("gawk -i inplace '{print}' file.txt")

    def test_gawk_iinplace_glued_destructive(self):
        assert _is_destructive_command("gawk -iinplace '{print}' file.txt")

    def test_awk_plain_not_destructive(self):
        assert not _is_destructive_command("awk '{print $1}' file.txt")


# ═══════════════════════════════════════════════════════════
#  _is_catastrophic_delete — top-level deletion guard
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestIsCatastrophicDelete:
    # ── Must BLOCK: filesystem root + first-level dirs ──
    def test_rm_rf_root(self):
        assert _is_catastrophic_delete('rm -rf /') == '/'

    def test_rm_mnt(self):
        assert _is_catastrophic_delete('rm -rf /mnt') is not None

    def test_rm_home(self):
        assert _is_catastrophic_delete('rm -r /home') is not None

    def test_rm_data_trailing_slash(self):
        assert _is_catastrophic_delete('rm -rf /data/') is not None

    def test_flag_order_does_not_smuggle(self):
        # rm /mnt -r -f  → still blocked (flags after the path)
        assert _is_catastrophic_delete('rm /mnt -r -f') is not None

    def test_wildcard_on_toplevel_blocked(self):
        assert _is_catastrophic_delete('rm -rf /mnt/*') is not None

    def test_rmdir_toplevel_blocked(self):
        assert _is_catastrophic_delete('rmdir /usr') is not None

    def test_blocked_in_pipeline_segment(self):
        # A safe command chained with a catastrophic one is still caught.
        assert _is_catastrophic_delete('cd /tmp && rm -rf /home') is not None

    # ── Must ALLOW: deep absolute + any relative target ──
    def test_deep_absolute_allowed(self):
        assert _is_catastrophic_delete('rm -rf /mnt/team/your-username/build') is None

    def test_relative_target_allowed(self):
        assert _is_catastrophic_delete('rm -rf build/') is None

    def test_relative_file_allowed(self):
        assert _is_catastrophic_delete('rm foo.txt') is None

    def test_non_delete_command_ignored(self):
        assert _is_catastrophic_delete('ls /mnt') is None
        assert _is_catastrophic_delete('cat /etc/passwd') is None


# ═══════════════════════════════════════════════════════════
#  _maybe_wrap_rm_with_trash — recoverable deletion shim
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRmTrashWrap:
    def test_rm_gets_wrapped(self):
        out = _maybe_wrap_rm_with_trash('rm -rf build/', '/work/ws')
        assert out != 'rm -rf build/'
        assert 'rm() {' in out
        assert '.tofu_trash' in out
        assert out.rstrip().endswith('rm -rf build/')

    def test_trash_root_is_self_ignoring(self):
        # The shim must drop a `.gitignore` (containing `*`) into the trash
        # root so trashed files never leak into git in the cwd repo.
        out = _maybe_wrap_rm_with_trash('rm -rf build/', '/work/ws')
        assert '.tofu_trash/.gitignore' in out

    def test_trash_lazy_prune_present(self):
        # The shim lazily prunes old trash entries (default TTL 7 days) so the
        # bin can't grow unbounded. The prune must run the real `rm` (a
        # `find ... -exec rm -rf` placed BEFORE the rm() function shadows it).
        out = _maybe_wrap_rm_with_trash('rm -rf build/', '/work/ws')
        assert '-mtime +7' in out
        assert out.index('-mtime') < out.index('rm() {')

    def test_trash_prune_disabled_when_ttl_zero(self, monkeypatch):
        import lib.project_mod.run_command as t
        monkeypatch.setattr(t, '_TRASH_TTL_DAYS', 0)
        out = t._maybe_wrap_rm_with_trash('rm -rf build/', '/work/ws')
        assert '-mtime' not in out
        assert 'rm() {' in out  # trashing itself still active

    def test_non_rm_untouched(self):
        assert _maybe_wrap_rm_with_trash('ls -la', '/work/ws') == 'ls -la'

    def test_rm_substring_not_wrapped(self):
        # 'confirm' contains 'rm' but is not an rm command.
        cmd = 'cat confirm.txt'
        assert _maybe_wrap_rm_with_trash(cmd, '/work/ws') == cmd

    def test_already_referencing_trash_not_double_wrapped(self):
        cmd = 'rm -rf .tofu_trash/old'
        assert _maybe_wrap_rm_with_trash(cmd, '/work/ws') == cmd

    def test_disabled_via_flag(self, monkeypatch):
        import lib.project_mod.run_command as t
        monkeypatch.setattr(t, '_RM_TRASH_ENABLED', False)
        assert t._maybe_wrap_rm_with_trash('rm -rf build/', '/work/ws') == 'rm -rf build/'
    def test_trash_anchors_at_git_root(self, tmp_path):
        # A sticky `cd` into a subdirectory must NOT drop the undo bin
        # inside the source tree — the bin belongs to the workspace root
        # (regression: lib/tasks_pkg/.tofu_trash held 161K of stale copies).
        repo = tmp_path / 'repo'
        sub = repo / 'lib' / 'pkg'
        sub.mkdir(parents=True)
        (repo / '.git').mkdir()
        out = _maybe_wrap_rm_with_trash('rm -rf build/', str(sub))
        assert f'{repo}/.tofu_trash' in out
        assert f'{sub}/.tofu_trash' not in out

    def test_trash_anchors_via_worktree_gitfile(self, tmp_path):
        # A git WORKTREE has a `.git` FILE (not a dir) — still a root.
        repo = tmp_path / 'wt'
        sub = repo / 'sub'
        sub.mkdir(parents=True)
        (repo / '.git').write_text('gitdir: /elsewhere\n')
        out = _maybe_wrap_rm_with_trash('rm -rf build/', str(sub))
        assert f'{repo}/.tofu_trash' in out

    def test_trash_falls_back_to_cwd_without_git(self, tmp_path):
        # Un-versioned workspace: historical cwd-anchored behaviour kept.
        d = tmp_path / 'noworkspace'
        d.mkdir()
        out = _maybe_wrap_rm_with_trash('rm -rf build/', str(d))
        assert f'{d}/.tofu_trash' in out


# ═══════════════════════════════════════════════════════════
#  _filter_changes_by_targets
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestFilterChangesByTargets:
    CHANGES = [
        {'rel_path': 'logs/postgresql.log', 'change_type': 'modified'},
        {'rel_path': 'logs/app.log', 'change_type': 'modified'},
        {'rel_path': 'src/main.py', 'change_type': 'modified'},
        {'rel_path': 'build/output.js', 'change_type': 'created'},
        {'rel_path': 'old_file.txt', 'change_type': 'deleted'},
    ]

    def test_specific_targets(self):
        f = _filter_changes_by_targets(self.CHANGES, {'src/main.py', 'old_file.txt'}, '/tmp')
        assert len(f) == 2
        assert {c['rel_path'] for c in f} == {'src/main.py', 'old_file.txt'}

    def test_none_keeps_all(self):
        f = _filter_changes_by_targets(self.CHANGES, None, '/tmp')
        assert len(f) == 5

    def test_empty_set_keeps_none(self):
        f = _filter_changes_by_targets(self.CHANGES, set(), '/tmp')
        assert len(f) == 0

    def test_dir_prefix_match(self):
        f = _filter_changes_by_targets(self.CHANGES, {'build/'}, '/tmp')
        assert any(c['rel_path'] == 'build/output.js' for c in f)

    def test_dir_without_slash(self):
        f = _filter_changes_by_targets(self.CHANGES, {'logs'}, '/tmp')
        paths = {c['rel_path'] for c in f}
        assert 'logs/postgresql.log' in paths and 'logs/app.log' in paths

    def test_exact_match_only(self):
        f = _filter_changes_by_targets(self.CHANGES, {'src/main.py'}, '/tmp')
        assert len(f) == 1
        assert f[0]['rel_path'] == 'src/main.py'


# ═══════════════════════════════════════════════════════════
#  Unicode-escape normalization in apply_diff / insert_content
#  Regression: model emits a real glyph (⏰, em-dash …) where the file
#  holds the literal escape text (\u23f0, \u2014) — or vice-versa — and
#  json.loads collapses the model's escape into a glyph at the arg
#  boundary, so a literal compare never matches. The matcher decodes
#  \uXXXX / \UXXXXXXXX / \xXX on both sides as a final fallback tier.
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestUnicodeEscapeNormalization:
    import os as _os
    import tempfile as _tempfile

    def _setup(self):
        import os
        import tempfile
        from lib.project_mod import config as cfg
        d = tempfile.mkdtemp()
        cfg._roots['esc_test'] = {'path': d}
        return d

    def _write(self, d, name, text):
        import os
        p = os.path.join(d, name)
        with open(p, 'w') as f:
            f.write(text)
        return p

    def test_decode_helper(self):
        from lib.project_mod.write_tools import _decode_unicode_escapes
        assert _decode_unicode_escapes(r'a \u23f0 b \u2014 c') == 'a \u23f0 b \u2014 c'
        # Non-escape text untouched; \n left alone (only numeric escapes decoded)
        assert _decode_unicode_escapes(r'plain \n text') == r'plain \n text'

    def test_apply_diff_glyph_search_escape_file(self):
        from lib.project_mod.write_tools import _apply_one_diff
        d = self._setup()
        self._write(d, 'a.py', "x = 1\nmsg = '\\u23f0 timed out'\ny = 2\n")
        r = _apply_one_diff(d, 'a.py', "msg = '\u23f0 timed out'", "msg = '[timed out]'")
        assert r['ok'], r.get('error')
        with open(self._os.path.join(d, 'a.py')) as f:
            assert f.read() == "x = 1\nmsg = '[timed out]'\ny = 2\n"

    def test_apply_diff_escape_search_glyph_file(self):
        from lib.project_mod.write_tools import _apply_one_diff
        d = self._setup()
        self._write(d, 'b.py', "x = 1\nmsg = '\u23f0 timed out'\ny = 2\n")
        r = _apply_one_diff(d, 'b.py', "msg = '\\u23f0 timed out'", "msg = '[ok]'")
        assert r['ok'], r.get('error')
        with open(self._os.path.join(d, 'b.py')) as f:
            assert f.read() == "x = 1\nmsg = '[ok]'\ny = 2\n"

    def test_insert_content_glyph_anchor_escape_file(self):
        from lib.project_mod.write_tools import _insert_one
        d = self._setup()
        self._write(d, 'c.py', "a = 1\nmsg = '\\u2014 dash'\nb = 2\n")
        r = _insert_one(d, 'c.py', "msg = '\u2014 dash'", "inserted = True", position='after')
        assert r['ok'], r.get('error')
        with open(self._os.path.join(d, 'c.py')) as f:
            body = f.read()
        assert 'inserted = True' in body
        # The literal-escape line is preserved verbatim, not rewritten.
        assert "msg = '\\u2014 dash'" in body

    def test_absent_text_still_fails(self):
        from lib.project_mod.write_tools import _apply_one_diff
        d = self._setup()
        self._write(d, 'e.py', "nothing relevant here\n")
        r = _apply_one_diff(d, 'e.py', "totally absent line", "x")
        assert not r['ok']
        assert 'not found' in r['error']

    def test_plain_ascii_unaffected(self):
        from lib.project_mod.write_tools import _apply_one_diff
        d = self._setup()
        self._write(d, 'f.py', "def foo():\n    return 1\n")
        r = _apply_one_diff(d, 'f.py', "    return 1", "    return 2")
        assert r['ok'], r.get('error')
        with open(self._os.path.join(d, 'f.py')) as f:
            assert f.read() == "def foo():\n    return 2\n"



# ═══════════════════════════════════════════════════════════
#  Large-file size gate vs. bounded range reads
#  Regression: a whole-file read of a >MAX_FILE_SIZE file is rejected
#  ("File too large"), but a bounded start_line/end_line read must still
#  succeed — its output is capped by the range, not the total size.
#  Rejecting it created a deadlock with the read-before-edit gate (the
#  only gate-satisfying tool, read_files, was itself blocked).
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestLargeFileRangeRead:
    def _make_big_file(self):
        import os
        import tempfile
        from lib.project_mod.config import MAX_FILE_SIZE
        d = tempfile.mkdtemp()
        # One line per row, comfortably over the size cap.
        nlines = (MAX_FILE_SIZE // 10) + 5000
        lines = [f'line-{i:08d}\n' for i in range(nlines)]
        with open(os.path.join(d, 'big.txt'), 'w') as f:
            f.writelines(lines)
        assert os.path.getsize(os.path.join(d, 'big.txt')) > MAX_FILE_SIZE
        return d

    def test_whole_file_read_still_blocked(self):
        from lib.project_mod.read_tools import _read_project_file
        d = self._make_big_file()
        r = _read_project_file(d, 'big.txt')
        assert 'File too large' in r

    def test_bounded_range_read_succeeds(self):
        from lib.project_mod.read_tools import _read_project_file
        d = self._make_big_file()
        r = _read_project_file(d, 'big.txt', 96, 96)
        assert 'File too large' not in r
        assert 'lines 96-96' in r
        assert 'line-00000095' in r  # line 96 is index 95

    def test_start_line_only_succeeds(self):
        from lib.project_mod.read_tools import _read_project_file
        d = self._make_big_file()
        r = _read_project_file(d, 'big.txt', 96)
        assert 'File too large' not in r
        assert 'line-00000095' in r

    def test_small_project_file_explicit_range_is_never_widened(self, tmp_path):
        """Incident mtc6xp7kka0hls: changed ranges returned one full file."""
        from lib.project_mod.read_tools import tool_read_files

        path = tmp_path / 'small.txt'
        path.write_text(''.join(
            f'LINE_{line:04d} value\n' for line in range(1, 401)),
            encoding='utf-8')
        assert path.stat().st_size < 40_000

        relative = tool_read_files(str(tmp_path), [{
            'path': 'small.txt', 'start_line': 130, 'end_line': 260,
        }])
        absolute = tool_read_files(str(tmp_path), [{
            'path': str(path), 'start_line': 130, 'end_line': 260,
        }])

        for result in (relative, absolute):
            assert 'lines 130-260 of 400' in result
            assert 'LINE_0130' in result and 'LINE_0260' in result
            assert 'LINE_0129' not in result and 'LINE_0261' not in result

    def test_invalid_explicit_range_never_degrades_to_whole_file(self, tmp_path):
        from lib.project_mod.read_tools import tool_read_files

        path = tmp_path / 'small.txt'
        path.write_text('SECRET_FIRST_LINE\nsecond\n', encoding='utf-8')
        for invalid in (0, -1, False, 'not-a-line'):
            result = tool_read_files(str(tmp_path), [{
                'path': 'small.txt', 'start_line': invalid,
            }])
            assert 'must be a positive 1-based integer' in result
            assert 'SECRET_FIRST_LINE' not in result

    def test_read_files_schema_rejects_non_positive_bounds(self):
        from lib.tools.project import READ_FILES_TOOL

        properties = READ_FILES_TOOL['function']['parameters']['properties']
        assert properties['start_line']['minimum'] == 1
        assert properties['end_line']['minimum'] == 1
        item_properties = properties['reads']['items']['properties']
        assert item_properties['start_line']['minimum'] == 1
        assert item_properties['end_line']['minimum'] == 1


# ═══════════════════════════════════════════════════════════
#  create_directory / delete_directory — folder-browser CRUD
#  Backs the project path panel's "New folder" / "Delete folder" so a
#  user can scaffold/remove a project dir without leaving the app.
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCreateDirectory:
    def _tmp(self):
        import tempfile
        return tempfile.mkdtemp()

    def test_create_ok(self):
        import os
        from lib.project_mod import create_directory
        d = self._tmp()
        r = create_directory(d, 'newproj')
        assert r.get('ok'), r
        assert os.path.isdir(os.path.join(d, 'newproj'))
        assert r['path'] == os.path.join(d, 'newproj')

    def test_reject_slash_name(self):
        from lib.project_mod import create_directory
        assert create_directory(self._tmp(), 'a/b').get('error')

    def test_reject_parent_ref_escape(self):
        # '..' must never let a create escape the parent dir.
        import os
        from lib.project_mod import create_directory
        d = self._tmp()
        r = create_directory(d, '../escape')
        assert r.get('error')
        assert not os.path.exists(os.path.join(os.path.dirname(d), 'escape'))

    def test_reject_empty_name(self):
        from lib.project_mod import create_directory
        assert create_directory(self._tmp(), '   ').get('error')

    def test_reject_duplicate(self):
        from lib.project_mod import create_directory
        d = self._tmp()
        create_directory(d, 'dup')
        assert 'exists' in create_directory(d, 'dup').get('error', '').lower()

    def test_missing_parent(self):
        import os
        from lib.project_mod import create_directory
        r = create_directory(os.path.join(self._tmp(), 'nope'), 'x')
        assert r.get('error')

    def test_reject_system_path_parent(self):
        # Creating directly under a forbidden system root is refused.
        from lib.project_mod import create_directory
        assert create_directory('/', 'etchack').get('error')


@pytest.mark.unit
class TestDeleteDirectory:
    def _tmp(self):
        import tempfile
        return tempfile.mkdtemp()

    def test_delete_moves_to_trash(self):
        import os
        from lib.project_mod import delete_directory
        d = self._tmp()
        target = os.path.join(d, 'gone')
        os.mkdir(target)
        r = delete_directory(target)
        assert r.get('ok'), r
        assert not os.path.exists(target)          # removed from original spot
        assert os.path.exists(r['trashed'])        # recoverable in trash bin
        assert '.tofu_trash' in r['trashed']

    def test_reject_non_directory(self):
        import os
        from lib.project_mod import delete_directory
        d = self._tmp()
        f = os.path.join(d, 'file.txt')
        open(f, 'w').close()
        assert delete_directory(f).get('error')

    def test_reject_system_path(self):
        from lib.project_mod import delete_directory
        assert 'system path' in delete_directory('/etc').get('error', '').lower()

    def test_reject_active_workspace_root(self):
        # A directory registered as a workspace root must not be deletable.
        import os
        from lib.project_mod import delete_directory
        from lib.project_mod.config import _make_root_state, _roots
        d = self._tmp()
        root = os.path.join(d, 'openproj')
        os.mkdir(root)
        _roots['tp_open_root'] = _make_root_state(root)
        try:
            r = delete_directory(root)
            assert r.get('error') and 'workspace root' in r['error'].lower()
            assert os.path.isdir(root)  # untouched
        finally:
            _roots.pop('tp_open_root', None)

    def test_reject_symlink(self):
        import os
        from lib.project_mod import delete_directory
        d = self._tmp()
        real = os.path.join(d, 'real'); os.mkdir(real)
        link = os.path.join(d, 'link'); os.symlink(real, link)
        r = delete_directory(link)
        assert r.get('error') and 'symlink' in r['error'].lower()
        assert os.path.isdir(real)
