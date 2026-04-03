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
    _is_destructive_command,
)

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
        assert len(lines) <= 3, f'Expected <= 3 lines, got {len(lines)}'
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
