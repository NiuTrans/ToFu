"""Regression: a scoped absolute-path delete (``rm -rf /tmp/wt_fill``) must NOT
be hard-blocked by the dangerous-pattern guard.

Incident (2026-07-28, tool-round screenshot): an agent cleaning up its own
temp git worktree issued

    cd <repo> && rm -rf /tmp/wt_fill 2>/dev/null
    git worktree add -q --detach /tmp/wt_fill <sha>

and got ``Error: Command blocked for safety: matches dangerous pattern.``
The match was ``DANGEROUS_PATTERNS[0]`` = ``\\brm\\s+-rf\\s+/`` — a
first-generation blunt regex that fires on ANY absolute-path delete and was
checked in ``run_command`` BEFORE the argument-parsing
``_is_catastrophic_delete`` guard. The parser already implements the real
rule the blunt regex pretended to (depth < 2 ⇒ catastrophic: ``/``,
``/mnt``, ``/home``; depth ≥ 2 ⇒ scoped, allowed) — but never got to run.

Fix:
  * the regex is REMOVED from ``DANGEROUS_PATTERNS``;
    ``_is_catastrophic_delete`` is the single delete authority in every
    enforcement point (run_command / desktop agent / safety pre-hook);
  * the parser now also sees through a leading ``sudo``/``doas`` wrapper —
    the one realistic shape the old substring net caught that the parser
    missed.

These tests pin the RESULT at three seams:
  1. end-to-end through ``tool_run_command``: the incident command runs, and
     ``rm -rf /`` is refused BEFORE any subprocess is spawned;
  2. the guard matrix: catastrophic shapes (incl. sudo/doas) refused, scoped
     shapes (incl. the two-segment ``/tmp/wt_fill``) allowed;
  3. complement: the regex layer no longer owns deletes at all — and its
     removal did NOT leave ``rm -rf /`` unguarded (proven by seam 1).
"""

from __future__ import annotations

import os

import pytest

from lib.project_mod.command_analysis import (
    _is_catastrophic_delete,
    _unwrap_command_parts,
)
from lib.project_mod.run_command import _is_dangerous_command, tool_run_command

pytestmark = pytest.mark.unit

# "rm" assembled at runtime so THIS test file's source / any shell that scans
# it never itself trips a dangerous-command guard (convention from
# tests/test_prehook_block_finalizes_round.py).
_RM = chr(114) + chr(109)

# A unique nonexistent probe path — deleting it is a guaranteed no-op.
_PROBE = f'/tmp/tofu_guard_probe_{os.getpid()}'


# ═══════════════════════════════════════════════════════════════════════════
#  1. End-to-end through tool_run_command
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestScopedDeleteRunsEndToEnd:
    """The exact incident shapes must reach the shell (not the block list)."""

    def test_two_segment_tmp_delete_allowed(self, monkeypatch):
        # Trash shim disabled: the guard verdict is what is under test, and
        # this keeps the test hermetic (no .tofu_trash litter in /tmp).
        monkeypatch.setattr('lib.project_mod.run_command._RM_TRASH_ENABLED', False)
        result = tool_run_command('/tmp', f'{_RM} -rf {_PROBE} 2>/dev/null')
        assert 'blocked for safety' not in result
        assert '[exit code: 0]' in result

    def test_incident_command_chain_allowed(self, monkeypatch):
        """The screenshot's full chain shape: cd && rm -rf /tmp/<wt> 2>/dev/null
        && a follow-up command — every segment must pass."""
        monkeypatch.setattr('lib.project_mod.run_command._RM_TRASH_ENABLED', False)
        cmd = (f'cd /tmp && {_RM} -rf {_PROBE} 2>/dev/null && echo cleaned')
        result = tool_run_command('/tmp', cmd)
        assert 'blocked for safety' not in result
        assert 'cleaned' in result
        assert '[exit code: 0]' in result

    def test_root_delete_refused_before_any_spawn(self, monkeypatch):
        """``rm -rf /`` must be refused by the guard chain BEFORE a subprocess
        exists. The Popen tripwire proves the refusal is the guard's verdict,
        not the OS's — and makes this test safe even if the guard regresses.
        """
        def _tripwire(*_a, **_kw):
            raise RuntimeError('subprocess spawned for a refused command')
        monkeypatch.setattr('subprocess.Popen', _tripwire)
        result = tool_run_command('/tmp', f'{_RM} -rf /')
        assert 'blocked for safety' in result
        assert "top-level path '/'" in result

    def test_sudo_root_delete_refused_before_any_spawn(self, monkeypatch):
        def _tripwire(*_a, **_kw):
            raise RuntimeError('subprocess spawned for a refused command')
        monkeypatch.setattr('subprocess.Popen', _tripwire)
        result = tool_run_command('/tmp', f'sudo {_RM} -rf /')
        assert 'blocked for safety' in result


# ═══════════════════════════════════════════════════════════════════════════
#  2. Guard matrix — the parser is the single delete authority
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCatastrophicDeleteMatrix:
    """Blocked: every root-ish shape, INCLUDING behind a privilege wrapper."""

    @pytest.mark.parametrize('command', [
        f'{_RM} -rf /',
        f'{_RM} -rf /mnt',
        f'{_RM} -r /home',
        f'{_RM} -rf /data/',
        f'{_RM} /mnt -r -f',          # flag order can't smuggle
        f'{_RM} -rf /mnt/*',          # wildcard tail stripped
        'rmdir /usr',
        f'sudo {_RM} -rf /',
        f'sudo -E {_RM} -rf /mnt',
        f'sudo -u root {_RM} -rf /',
        f'doas {_RM} -rf /home',
        f'cd /tmp && sudo {_RM} -rf /',   # wrapped delete in a chain
    ])
    def test_catastrophic_shapes_blocked(self, command):
        assert _is_catastrophic_delete(command) is not None

    @pytest.mark.parametrize('command', [
        f'{_RM} -rf /tmp/wt_fill',                    # the incident shape
        f'{_RM} -rf /tmp/wt_fill 2>/dev/null',
        f'{_RM} -rf /mnt/team/your-username/build',    # deep shared path
        f'{_RM} -rf build/',                          # relative stays in cwd
        f'sudo {_RM} -rf /tmp/wt_fill',               # scoped sudo delete
        f'{_RM} -rf ~/old_build',                     # deep home subpath
        'echo sudo rm -rf /',                         # printed, not executed
        'find . -name "*.pyc" | head',               # no delete at all
    ])
    def test_scoped_shapes_allowed(self, command):
        assert _is_catastrophic_delete(command) is None


# ═══════════════════════════════════════════════════════════════════════════
#  3. Wrapper unwrap unit + complement: the regex layer owns deletes no more
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestUnwrapCommandParts:
    def test_plain_command_untouched(self):
        parts = ['rm', '-rf', '/tmp/x']
        assert _unwrap_command_parts(parts) == parts

    def test_sudo_stripped(self):
        assert _unwrap_command_parts(['sudo', 'rm', '-rf', '/']) == ['rm', '-rf', '/']

    def test_sudo_flags_stripped(self):
        assert _unwrap_command_parts(
            ['sudo', '-E', 'rm', '-rf', '/']) == ['rm', '-rf', '/']

    def test_sudo_arg_flag_consumes_value(self):
        # `-u root` must be skipped TOGETHER or 'root' becomes the "command".
        assert _unwrap_command_parts(
            ['sudo', '-u', 'root', 'rm', '-rf', '/']) == ['rm', '-rf', '/']

    def test_doas_stripped(self):
        assert _unwrap_command_parts(['doas', 'rm', '-rf', '/']) == ['rm', '-rf', '/']

    def test_wrapper_without_command_returns_empty(self):
        assert _unwrap_command_parts(['sudo', '-E']) == []

    def test_full_path_wrapper_stripped(self):
        assert _unwrap_command_parts(
            ['/usr/bin/sudo', 'rm', '-rf', '/']) == ['rm', '-rf', '/']


@pytest.mark.unit
class TestRegexLayerNoLongerOwnsDeletes:
    """Complement pin: the blunt regex is GONE (both shapes now read False
    here) — and `rm -rf /` is still refused, by the parser (seam 1 proves
    it end-to-end). If someone re-adds a delete regex, the first assertion
    turns red; if someone removes the parser guard, seam 1 turns red."""

    def test_dangerous_regex_ignores_scoped_delete(self):
        assert _is_dangerous_command(f'{_RM} -rf /tmp/wt_fill') is False

    def test_dangerous_regex_ignores_root_delete_too(self):
        assert _is_dangerous_command(f'{_RM} -rf /') is False

    def test_other_structural_patterns_still_blocked(self):
        # The list itself was narrowed, not emptied.
        assert _is_dangerous_command('mkfs.ext4 /dev/sda1') is True
        assert _is_dangerous_command('shutdown -h now') is True


# ═══════════════════════════════════════════════════════════════════════════
#  4. Trash wrap boundary: the bin is the WORKSPACE's undo, not the machine's
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestTrashWrapSkipsOutsideWorkspace:
    """Once scoped absolute deletes were unblocked, they reached the rm→trash
    shim for the first time — and the shim used to move EVERY operand into
    ``<cwd>/.tofu_trash/``. For ``rm -rf /tmp/wt_fill`` that is a
    cross-device copy of a whole worktree into the (FUSE) project dir on
    every cleanup, plus 7 days of non-project temp data in the bin.

    Contract: an absolute operand resolving OUTSIDE the workspace cwd is
    deleted directly; relative / in-workspace absolute operands keep full
    trash semantics. These tests use REAL directories and assert the result
    (target gone? copy in the bin?) — not the shim's text.
    """

    def _enable_trash(self, monkeypatch):
        monkeypatch.setattr(
            'lib.project_mod.run_command._RM_TRASH_ENABLED', True)

    def test_outside_target_deleted_directly_never_trashed(
            self, tmp_path, monkeypatch):
        self._enable_trash(monkeypatch)
        ws = tmp_path / 'ws'
        ws.mkdir()
        victim = tmp_path / 'victim'
        (victim / 'sub').mkdir(parents=True)
        (victim / 'sub' / 'data.txt').write_text('x', encoding='utf-8')
        result = tool_run_command(str(ws), f'{_RM} -rf {victim}')
        assert 'blocked for safety' not in result
        assert not victim.exists(), 'outside target must really be deleted'
        assert not (ws / '.tofu_trash').exists(), (
            'nothing may be copied into the workspace trash bin')

    def test_relative_inside_target_still_trashed(self, tmp_path, monkeypatch):
        """Complement: without this, 'never trash anything' also passes above."""
        self._enable_trash(monkeypatch)
        ws = tmp_path / 'ws'
        (ws / 'sub').mkdir(parents=True)
        (ws / 'sub' / 'keep.txt').write_text('keep', encoding='utf-8')
        result = tool_run_command(str(ws), f'{_RM} -rf sub')
        assert 'blocked for safety' not in result
        assert not (ws / 'sub').exists()
        found = list((ws / '.tofu_trash').rglob('keep.txt'))
        assert found, 'workspace delete must land in the trash bin'

    def test_absolute_inside_workspace_still_trashed(
            self, tmp_path, monkeypatch):
        self._enable_trash(monkeypatch)
        ws = tmp_path / 'ws'
        (ws / 'sub').mkdir(parents=True)
        (ws / 'sub' / 'keep.txt').write_text('keep', encoding='utf-8')
        result = tool_run_command(str(ws), f'{_RM} -rf {ws / "sub"}')
        assert 'blocked for safety' not in result
        assert not (ws / 'sub').exists()
        found = list((ws / '.tofu_trash').rglob('keep.txt'))
        assert found, 'in-workspace absolute delete must land in the bin'

    def test_mixed_command_splits_by_target(self, tmp_path, monkeypatch):
        """Per-operand split in ONE command: outside → direct delete,
        inside → trash. This is the case a command-level all-or-nothing
        skip cannot express."""
        self._enable_trash(monkeypatch)
        ws = tmp_path / 'ws'
        (ws / 'sub').mkdir(parents=True)
        (ws / 'sub' / 'keep.txt').write_text('keep', encoding='utf-8')
        victim = tmp_path / 'victim'
        (victim / 'sub').mkdir(parents=True)
        (victim / 'sub' / 'data.txt').write_text('x', encoding='utf-8')
        result = tool_run_command(
            str(ws), f'{_RM} -rf {victim} && {_RM} -rf sub')
        assert 'blocked for safety' not in result
        assert not victim.exists() and not (ws / 'sub').exists()
        trash = ws / '.tofu_trash'
        assert list(trash.rglob('keep.txt')), 'inside operand must be trashed'
        assert not list(trash.rglob('data.txt')), (
            'outside operand must NOT be copied into the bin')

    def test_boundary_disabled_without_absolute_cwd(self):
        """Fail-safe shape: when the workspace boundary is unknown (relative
        cwd), the shim keeps the old trash-everything behaviour — the
        direct-delete branch only exists when cwd is absolute."""
        from lib.project_mod.run_command import _maybe_wrap_rm_with_trash
        out = _maybe_wrap_rm_with_trash(f'{_RM} -rf build/', 'rel/base')
        assert 'command rm' not in out and '.tofu_trash' in out
        out_abs = _maybe_wrap_rm_with_trash(f'{_RM} -rf build/', '/abs/base')
        assert 'command rm' in out_abs
