"""tests/test_run_command_scan_guard.py — unbounded recursive-scan guard (B2).

Incident (2026-07-31, task 96c56840): the model ended a verification script
with ``grep -rn "mcp>=1.0.0" ../ --include=pyproject.toml | grep -v … | head``
— a recursive scan of the workspace PARENT, a FUSE mount holding 8GB+ of
dumps, a PG data dir, 7392 swarm dirs and every sibling repo. The pipeline
ran 2.5h with zero output, the task wedged, and NO timeout existed to end it
(the run_command call carried ``timeout=unlimited``).

The guard (owner ruling 2026-07-31, ``_unbounded_recursive_scan_target`` +
its call site in ``tool_run_command``): refuse a recursive scan whose target
is (a) an ANCESTOR of the workspace cwd, or (b) a shallow FUSE-mount path
(≤3 components under /mnt) — but ONLY when the invocation is unbounded.
Escape hatches, all legitimate: scan a subdir, pass an explicit ``timeout``,
or wrap the scan in coreutils ``timeout``.

Pinned here:
  1. classifier matrix — incident shapes refused; in-workspace / sibling /
     deep-specific / bounded shapes allowed;
  2. end-to-end through ``tool_run_command`` — refused BEFORE any subprocess
     (Popen tripwire), allowed shapes actually execute;
  3. escape hatches (explicit timeout / coreutils wrapper) stay open;
  4. the kill switch TOFU_RUN_SCAN_GUARD=0.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_run_command_scan_guard.py -v
"""

from __future__ import annotations

import os

import pytest

from lib.project_mod.command_analysis import _unbounded_recursive_scan_target
from lib.project_mod.run_command import tool_run_command

pytestmark = pytest.mark.unit


@pytest.fixture()
def ws(tmp_path):
    """A workspace two levels deep so `..` / `../..` are ancestors."""
    base = tmp_path / 'parent' / 'chatui'
    (base / 'lib').mkdir(parents=True)
    (base / 'lib' / 'a.py').write_text('x = 1\n', encoding='utf-8')
    (base / 'README.md').write_text('x readme\n', encoding='utf-8')
    # A sibling repo next to the workspace — NOT an ancestor, must stay legal.
    sib = tmp_path / 'parent' / 'sibling-repo'
    sib.mkdir()
    (sib / 'pyproject.toml').write_text('x = 1\n', encoding='utf-8')
    return str(base)


# ═════════════════════════════════════════════════════════════════════
#  1. Classifier matrix
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestScanGuardBlockedShapes:
    @pytest.mark.parametrize('command', [
        'grep -rn "mcp>=1.0.0" ../ --include=pyproject.toml',   # the incident
        'grep -rn "mcp>=1.0.0" ../ --include=pyproject.toml | grep -v "<2" | head -3',
        'grep -rn x ..',
        'grep -rn x ../..',
        'grep -r x ..',
        'grep --recursive x ..',
        'grep -nHr x ..',
        'rg "pattern" ../..',
        'rg x ..',
        'find .. -name "*.py"',
        'find ../.. -name pyproject.toml',
        'du -sh ..',
        'tree ..',
        'grep -rn x ".."',                        # quoted target
        'sudo grep -rn x ..',                     # privilege wrapper seen through
        'cd /tmp && grep -rn x ../..',            # chain segment
    ])
    def test_ancestor_scans_blocked(self, command, ws):
        assert _unbounded_recursive_scan_target(command, ws) is not None, (
            f'should refuse: {command}')

    @pytest.mark.parametrize('command', [
        'grep -rn x /mnt',
        'grep -rn x /mnt/dolphinfs',
        'find /mnt/your-fs -name x',
        'du -sh /mnt/dolphinfs',
    ])
    def test_fuse_root_scans_blocked(self, command, ws):
        assert _unbounded_recursive_scan_target(command, ws) is not None, (
            f'should refuse FUSE-root scan: {command}')


@pytest.mark.unit
class TestScanGuardAllowedShapes:
    @pytest.mark.parametrize('command', [
        'grep -rn x .',                           # cwd — the normal case
        'grep -rn x lib/',                        # subdir
        'grep -rn x ./lib',
        'grep -rn x',                             # no target → scans cwd
        'grep -rn x README.md',                   # a file, not a dir
        'grep -n x lib/a.py',                     # non-recursive
        'grep x lib/a.py',
        'rg x lib',
        'rg x ../sibling-repo',                   # sibling ≠ ancestor
        'grep -rn x ../sibling-repo',
        'find ../sibling-repo -name "*.toml"',    # sibling find
        'find . -name "*.py"',
        'du -sh ../sibling-repo',                 # sibling du
        'timeout 300 grep -rn x ..',              # coreutils-bounded escape
        'grep -rn "foo|bar" lib/',                # quoted pipe stays unsplit
    ])
    def test_legit_scans_allowed(self, command, ws):
        assert _unbounded_recursive_scan_target(command, ws) is None, (
            f'should allow: {command}')

    def test_grep_e_flag_target_after_pattern_flag(self, ws):
        # With -e, the pattern is consumed by the flag; the next positional
        # IS the target — ancestor must still be refused.
        assert _unbounded_recursive_scan_target('grep -rn -e x ..', ws) is not None

    def test_deep_specific_mnt_path_allowed(self, ws):
        # A deep, specific /mnt path (not a mount root) is a legitimate target.
        deep = os.path.join(ws, 'lib')
        assert _unbounded_recursive_scan_target(f'grep -rn x {deep}', ws) is None


# ═════════════════════════════════════════════════════════════════════
#  2. End-to-end through tool_run_command
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestScanGuardEndToEnd:
    def test_incident_shape_refused_before_any_spawn(self, ws, monkeypatch):
        """The exact incident command must be refused by the guard BEFORE a
        subprocess exists — the Popen tripwire proves it."""
        def _tripwire(*_a, **_kw):
            raise RuntimeError('subprocess spawned for a refused command')
        monkeypatch.setattr('subprocess.Popen', _tripwire)
        result = tool_run_command(
            ws, 'grep -rn "mcp>=1.0.0" ../ --include=pyproject.toml '
                '2>/dev/null | grep -v "<2" | head -3')
        assert 'blocked for safety' in result
        assert 'unbounded recursive scan' in result
        # The error is ACTIONABLE: it must teach the three escape hatches.
        assert 'timeout' in result

    def test_in_workspace_scan_actually_runs(self, ws):
        result = tool_run_command(ws, 'grep -rn "x = 1" lib/')
        assert 'blocked for safety' not in result
        assert 'a.py' in result

    def test_sibling_scan_actually_runs(self, ws):
        result = tool_run_command(ws, 'grep -rn "x = 1" ../sibling-repo/')
        assert 'blocked for safety' not in result
        assert 'pyproject.toml' in result


@pytest.mark.unit
class TestScanGuardEscapeHatches:
    def test_explicit_timeout_bypasses_guard(self, ws):
        """Owner's contract: passing an explicit budget makes the scan legal
        (a bounded crawl cannot wedge the task forever)."""
        result = tool_run_command(ws, 'grep -rn "x = 1" ../', timeout=30)
        assert 'blocked for safety' not in result
        assert 'sibling-repo' in result or 'a.py' in result

    def test_coreutils_timeout_wrapper_bypasses_guard(self, ws):
        result = tool_run_command(ws, 'timeout 30 grep -rn "x = 1" ../')
        assert 'blocked for safety' not in result

    def test_kill_switch_disables_guard(self, ws, monkeypatch):
        monkeypatch.setenv('TOFU_RUN_SCAN_GUARD', '0')
        # Guard off: the incident shape falls through to execution. Run it in
        # the tiny tmp parent so it completes instantly.
        result = tool_run_command(ws, 'grep -rn "x = 1" ../ --include=pyproject.toml')
        assert 'blocked for safety' not in result
        assert 'pyproject.toml' in result


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
