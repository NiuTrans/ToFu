"""Tests for lib/timeutil.py — the shared time helpers.

Covers now_ms() contract and that the two consolidated consumers
(lib/artifacts/core.py, lib/swarm/persistence.py) reuse it rather than
carrying their own duplicate _now_ms copy.
"""

import time

import pytest

from lib import timeutil


def test_now_ms_returns_int_milliseconds():
    before = int(time.time() * 1000)
    val = timeutil.now_ms()
    after = int(time.time() * 1000)
    assert isinstance(val, int)
    assert before <= val <= after


def test_now_ms_monotonic_nondecreasing():
    a = timeutil.now_ms()
    b = timeutil.now_ms()
    assert b >= a


def test_now_ms_exported():
    assert 'now_ms' in timeutil.__all__


# Consumers whose _now_ms consolidation is committed (present in the tracked
# tree). The project_status / project_watch modules are also repointed in the
# working tree but are untracked sibling files, so they are not asserted here
# (they carry their own local _now_ms until their author commits them).
@pytest.mark.parametrize('modname', [
    'lib.artifacts.core',
    'lib.swarm.persistence',
    'lib.conversations.project_board',
])
def test_consumers_reuse_shared_now_ms(modname):
    """Every consolidated consumer must delegate to timeutil.now_ms, not define its own."""
    import importlib

    mod = importlib.import_module(modname)
    # Their module-local _now_ms alias must BE the shared helper (same object).
    assert getattr(mod, '_now_ms') is timeutil.now_ms
