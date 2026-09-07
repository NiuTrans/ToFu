"""Tests for the sticky per-conversation working-directory anchor added to
``run_command`` (2026-07-09).

Goal of the feature: stop the model burning tokens re-emitting ``cd <project>``
/ absolute ``python``/``pip`` on every call. Once a conversation navigates (an
explicit ``working_dir`` OR a trailing ``cd`` inside the command), later
``run_command`` calls with no ``working_dir`` resume from that directory.

Design invariants under test:
  * STATELESS derived affinity — no persistent shell, env re-derived per call.
  * HARD ISOLATION — a cwd is only remembered when it stays inside a root the
    conversation owns (``_cwd_within_conv_roots``); a ``cd /etc`` never sticks.
  * SAFE-DEGRADE — a vanished sticky, or the ``TOFU_STICKY_CWD=0`` gate, falls
    back to the project-root anchor and never corrupts.
  * cd-capture uses a dedicated FILE (never stdout) and preserves the exit code.

The neuter check at the bottom proves the assertions bite: if ``set_conv_cwd``
is made a no-op, the stickiness tests fail.
"""

import os

import pytest

from lib.project_mod import config
from lib.project_mod.tools import _exec_run_command

pytestmark = pytest.mark.unit


def _run(conv_id, base, command, working_dir=None):
    """Invoke run_command exactly as the dispatch layer does."""
    fn_args = {'command': command}
    if working_dir is not None:
        fn_args['working_dir'] = working_dir
    return _exec_run_command(fn_args, base, conv_id, 'task-x', {})


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A conv with a primary root + a nested 'sub' dir, sticky enabled."""
    monkeypatch.setenv('TOFU_STICKY_CWD', '1')
    primary = tmp_path / 'proj'
    sub = primary / 'sub'
    sub.mkdir(parents=True)
    conv_id = 'conv-sticky-cwd'
    config.set_conv_roots(conv_id, str(primary))
    yield conv_id, str(primary), str(sub)
    config.clear_conv_state(conv_id)


# ── config-level registry ────────────────────────────────────

def test_set_get_within_root(workspace):
    conv_id, primary, sub = workspace
    assert config.set_conv_cwd(conv_id, sub) is True
    assert config.get_conv_cwd(conv_id) == os.path.realpath(sub) or \
        config.get_conv_cwd(conv_id) == sub


def test_set_rejects_outside_roots(workspace):
    conv_id, _primary, _sub = workspace
    # /tmp (or any dir outside the registered root) must be refused.
    assert config.set_conv_cwd(conv_id, '/tmp') is False
    assert config.get_conv_cwd(conv_id) is None


def test_get_clears_vanished_dir(workspace, tmp_path):
    conv_id, primary, _sub = workspace
    gone = tmp_path / 'proj' / 'ephemeral'
    gone.mkdir()
    assert config.set_conv_cwd(conv_id, str(gone)) is True
    gone.rmdir()
    # Vanished sticky safe-degrades to None and is cleared.
    assert config.get_conv_cwd(conv_id) is None


def test_clear_conv_state_drops_cwd(workspace):
    conv_id, _primary, sub = workspace
    config.set_conv_cwd(conv_id, sub)
    config.clear_conv_state(conv_id)
    assert config.get_conv_cwd(conv_id) is None


# ── end-to-end through _exec_run_command ─────────────────────

def test_explicit_working_dir_becomes_sticky(workspace):
    conv_id, primary, sub = workspace
    # Navigate explicitly into 'sub' (bare relative resolves under primary).
    _run(conv_id, primary, 'pwd', working_dir='sub')
    assert config.get_conv_cwd(conv_id) == sub
    # Next call with NO working_dir resumes from sub.
    out2 = _run(conv_id, primary, 'pwd')
    assert 'sub' in out2


def test_no_working_dir_uses_anchor_when_no_sticky(workspace):
    conv_id, primary, _sub = workspace
    out = _run(conv_id, primary, 'pwd')
    # No sticky yet → runs at the project-root anchor.
    assert primary in out
    assert config.get_conv_cwd(conv_id) == primary


def test_trailing_cd_is_captured(workspace):
    conv_id, primary, sub = workspace
    # A trailing `cd sub` inside the command must stick for the next call.
    out = _run(conv_id, primary, 'cd sub && pwd')
    assert 'sub' in out
    assert config.get_conv_cwd(conv_id) == os.path.realpath(sub)
    # And the next bare call resumes there.
    out2 = _run(conv_id, primary, 'pwd')
    assert 'sub' in out2


def test_cd_capture_preserves_exit_code(workspace):
    conv_id, primary, _sub = workspace
    out = _run(conv_id, primary, 'false')
    assert '[exit code: 1]' in out


def test_cd_outside_roots_not_captured(workspace):
    conv_id, primary, _sub = workspace
    # Hop outside the registered root → must NOT stick (isolation).
    _run(conv_id, primary, 'cd /tmp && pwd')
    assert config.get_conv_cwd(conv_id) is None
    # Bare call still runs at anchor.
    out = _run(conv_id, primary, 'pwd')
    assert primary in out


def test_gate_off_disables_sticky(workspace, monkeypatch):
    conv_id, primary, sub = workspace
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    _run(conv_id, primary, 'cd sub && pwd', working_dir=None)
    # Nothing remembered while gated off.
    assert config.get_conv_cwd(conv_id) is None


# ── neuter: prove the sticky assertions bite ─────────────────

def test_neuter_set_conv_cwd_breaks_stickiness(workspace, monkeypatch):
    """If set_conv_cwd is neutered to a no-op, stickiness must break."""
    conv_id, primary, sub = workspace
    monkeypatch.setattr(config, 'set_conv_cwd', lambda *a, **k: False)
    # Re-patch the name imported into tools._exec_run_command's module scope.
    import lib.project_mod.tools as _tools
    monkeypatch.setattr(_tools, 'set_conv_cwd', lambda *a, **k: False, raising=False)
    _run(conv_id, primary, 'cd sub && pwd')
    # With the no-op, nothing is remembered.
    assert config.get_conv_cwd(conv_id) is None
