"""tests/test_desktop_cc_persistence.py — tray computer-control state survives restarts.

WHAT THIS GUARDS
----------------
Before this fix, the tray's "Enable Computer Control" checkbox and the
three permission tiers (allow_write / allow_exec / allow_gui) lived only
in the launcher's in-memory ``_cc_state`` — every app restart silently
reset the user to OFF/deny-all, and they had to re-click everything.

The fix has three load-bearing properties, each pinned here:

  * ROUND-TRIP — an explicit toggle/perm click is persisted to the agent
    config (``computer_control: {enabled, perms}``) and restored verbatim;
  * DENY-BY-DEFAULT FLOOR — a fresh install (or a malformed blob)
    restores NOTHING: enabled=False, perms untouched. Restore may never
    widen the default posture;
  * PERSIST-ONLY-ON-EXPLICIT-ACTION — the wiring persists from the two
    click handlers (enable toggle, permission tier) and NOWHERE else:
    quitting the app calls _stop_computer_control, and if that path also
    persisted, every Quit would erase the user's choice.

The config store is isolated via TOFU_DESKTOP_CONFIG.

Run:  pytest tests/test_desktop_cc_persistence.py -q
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolate the agent config store in a tmp dir."""
    import desktop.launcher as launcher
    from lib.desktop_agent import config as agent_config
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG',
                       str(tmp_path / 'desktop_agent.json'))
    return launcher, agent_config


# ── config layer: load/save_computer_control ─────────────────────────

def test_fresh_install_restores_nothing(env):
    _launcher, agent_config = env
    enabled, perms = agent_config.load_computer_control()
    assert enabled is False, 'a fresh install must come up OFF'
    assert perms == {}, 'nothing was ever chosen — nothing to restore'


def test_toggle_state_round_trips_with_coerced_bools(env):
    _launcher, agent_config = env
    agent_config.save_computer_control(
        1, {'allow_write': 1, 'allow_exec': 0, 'allow_gui': True})
    enabled, perms = agent_config.load_computer_control()
    assert enabled is True
    assert perms == {'allow_write': True, 'allow_exec': False,
                     'allow_gui': True}, (
        'values are coerced to plain bools and round-trip verbatim')


def test_unknown_perm_keys_are_dropped_on_load(env):
    _launcher, agent_config = env
    agent_config.save_computer_control(
        True, {'allow_write': True, 'allow_format_c_drive': True})
    _enabled, perms = agent_config.load_computer_control()
    assert 'allow_format_c_drive' not in perms, (
        'only canonical PERMISSION_KEYS may come back from disk')
    assert perms.get('allow_write') is True


def test_malformed_blob_restores_nothing(env):
    _launcher, agent_config = env
    cfg = agent_config.load_config()
    cfg['computer_control'] = 'junk'
    agent_config.save_config(cfg)
    enabled, perms = agent_config.load_computer_control()
    assert enabled is False and perms == {}
    cfg['computer_control'] = {'enabled': True, 'perms': 'junk'}
    agent_config.save_config(cfg)
    enabled, perms = agent_config.load_computer_control()
    assert enabled is True, 'enabled survives a bad perms blob'
    assert perms == {}, 'a malformed perms blob restores no tiers'


def test_save_preserves_the_other_config_fields(env):
    _launcher, agent_config = env
    agent_config.save_remote_server('http://127.0.0.1:15000', 'tok')
    agent_config.save_computer_control(True, {'allow_exec': True})
    url, secret = agent_config.remote_server()
    assert (url, secret) == ('http://127.0.0.1:15000', 'tok'), (
        'persisting cc state must not clobber the remote attachment')


# ── launcher layer: _restore/_persist_cc_state ───────────────────────

def test_restore_merges_saved_perms_over_the_deny_all_baseline(env):
    launcher, agent_config = env
    agent_config.save_computer_control(True, {'allow_write': True})
    state = {'enabled': False, 'perms': None}
    assert launcher._restore_cc_state(state) is True
    perms = state['perms']
    assert perms['allow_write'] is True, 'the saved tier wins'
    assert perms['allow_exec'] is False
    assert perms['allow_gui'] is False
    assert perms['allow_egress'] is False, (
        'tiers absent from the file merge in from the deny-all baseline — '
        'a tier added later still defaults OFF for old configs')


def test_restore_on_fresh_install_leaves_state_untouched(env):
    launcher, _agent_config = env
    state = {'enabled': False, 'perms': None}
    assert launcher._restore_cc_state(state) is False
    assert state['perms'] is None, (
        'fresh install: restore must not widen anything — the launcher '
        'keeps its own deny-all default path')


def test_persist_writes_enabled_and_perms(env):
    launcher, agent_config = env
    state = {'enabled': True,
             'perms': {'allow_write': False, 'allow_exec': True,
                       'allow_gui': False, 'allow_egress': False}}
    launcher._persist_cc_state(state)
    enabled, perms = agent_config.load_computer_control()
    assert enabled is True
    assert perms['allow_exec'] is True
    assert perms['allow_write'] is False


# ── wiring pins: the state leaves memory ONLY via explicit clicks ────

def test_tray_restores_state_before_running_the_menu():
    import inspect
    import desktop.launcher as launcher
    src = inspect.getsource(launcher._run_tray)
    assert '_restore_cc_state(_cc_state)' in src, (
        'launch must restore the persisted state, or the feature is dead')


def test_persist_is_wired_to_exactly_the_two_click_handlers():
    import inspect
    import desktop.launcher as launcher
    src = inspect.getsource(launcher._run_tray)
    assert src.count('_persist_cc_state(_cc_state)') == 2, (
        'persist must fire from the enable toggle and the tier handler — '
        'and from NOWHERE else: on_quit calls _stop_computer_control, '
        'which would erase the user\'s choice on every Quit if it persisted')
