#!/usr/bin/env python3
"""Regression: mlockall must gate on LIVE cgroup headroom, not the raw ceiling.

Root cause (2026-07-20): on a SHARED cgroup whose limit is the whole machine
(200 GiB) but which sits at ~99.9% full of sibling processes + FUSE page/slab
cache, ``_tofu_should_mlock()`` used to return True purely because the *limit*
was generous. Pinning there both adds unreclaimable pages and inflates tofu's
own ``oom_score`` (it becomes the highest-RSS process in the group), so the
cgroup OOM killer SIGKILLs tofu first — repeatedly (observed ``oom_kill 566``).

The fix adds a live-usage gate: when usage is already past
``TOFU_MLOCK_MAX_USAGE_PCT`` (default 85%) of the limit, skip pinning even
though the ceiling is large. A roomy cgroup still pins (SIGBUS mitigation
preserved); unknown usage still proceeds (matches prior behaviour).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_mlock_headroom_gate.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit

_GIB = 1 << 30


@pytest.fixture(scope='module')
def server_module():
    try:
        import quart  # noqa: F401
        import hypercorn  # noqa: F401
    except ImportError as e:
        pytest.skip(f'quart/hypercorn not installed: {e}')
    import server
    return server


def _force_auto_on_fuse(server, monkeypatch):
    """Neutralise the FUSE + mode short-circuits so the headroom path is reached."""
    monkeypatch.setenv('TOFU_MLOCK', 'auto')
    monkeypatch.delenv('TOFU_MLOCK_MAX_USAGE_PCT', raising=False)
    monkeypatch.delenv('TOFU_MLOCK_MIN_LIMIT_GB', raising=False)
    monkeypatch.setattr(server, '_tofu_path_is_fuse', lambda _p: True)


def test_default_is_off(server_module, monkeypatch):
    """A future allocation spike must not become unreclaimable by default."""
    monkeypatch.delenv('TOFU_MLOCK', raising=False)
    monkeypatch.setattr(server_module, '_tofu_path_is_fuse', lambda _p: True)
    do_it, reason = server_module._tofu_should_mlock()
    assert do_it is False
    assert 'disabled' in reason


def test_skips_on_contended_shared_cgroup(server_module, monkeypatch):
    """Big limit but ~full → skip (the real observed OOM scenario)."""
    _force_auto_on_fuse(server_module, monkeypatch)
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_limit_bytes',
                        lambda: 200 * _GIB)
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_usage_bytes',
                        lambda: int(199.7 * _GIB))  # 99.85% full
    do_it, reason = server_module._tofu_should_mlock()
    assert do_it is False, reason
    assert 'full' in reason


def test_pins_on_roomy_cgroup(server_module, monkeypatch):
    """Big limit AND low usage → still pin (SIGBUS mitigation preserved)."""
    _force_auto_on_fuse(server_module, monkeypatch)
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_limit_bytes',
                        lambda: 200 * _GIB)
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_usage_bytes',
                        lambda: int(20 * _GIB))  # 10% full
    do_it, reason = server_module._tofu_should_mlock()
    assert do_it is True, reason


def test_unknown_usage_proceeds(server_module, monkeypatch):
    """Usage unreadable → proceed, matching prior (limit-only) behaviour."""
    _force_auto_on_fuse(server_module, monkeypatch)
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_limit_bytes',
                        lambda: 200 * _GIB)
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_usage_bytes',
                        lambda: None)
    do_it, reason = server_module._tofu_should_mlock()
    assert do_it is True, reason


def test_threshold_is_tunable(server_module, monkeypatch):
    """TOFU_MLOCK_MAX_USAGE_PCT tightens/loosens the gate."""
    _force_auto_on_fuse(server_module, monkeypatch)
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_limit_bytes',
                        lambda: 200 * _GIB)
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_usage_bytes',
                        lambda: int(60 * _GIB))  # 30% full
    # Default 85% → 30% is fine, pin.
    assert server_module._tofu_should_mlock()[0] is True
    # Lower the bar to 25% → 30% now over the line, skip.
    monkeypatch.setenv('TOFU_MLOCK_MAX_USAGE_PCT', '25')
    do_it, reason = server_module._tofu_should_mlock()
    assert do_it is False, reason


def test_neuter_ignoring_usage_would_wrongly_pin(server_module, monkeypatch):
    """NEUTER: if the gate ignored usage (old behaviour), the contended case pins.

    Proves the usage signal is load-bearing — reverting to a limit-only check
    flips the contended scenario back to True.
    """
    _force_auto_on_fuse(server_module, monkeypatch)
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_limit_bytes',
                        lambda: 200 * _GIB)
    # Simulate the pre-fix world: usage probe returns None (never consulted).
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_usage_bytes',
                        lambda: None)
    do_it, _ = server_module._tofu_should_mlock()
    assert do_it is True  # old behaviour — the very bug we fixed for real usage


def test_forced_on_overrides_headroom(server_module, monkeypatch):
    """TOFU_MLOCK=1 still forces pinning regardless of headroom (escape hatch)."""
    monkeypatch.setattr(server_module, '_tofu_path_is_fuse', lambda _p: True)
    monkeypatch.setattr(server_module, '_tofu_cgroup_mem_usage_bytes',
                        lambda: int(199.9 * _GIB))
    monkeypatch.setenv('TOFU_MLOCK', '1')
    do_it, reason = server_module._tofu_should_mlock()
    assert do_it is True
    assert 'forced' in reason


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
