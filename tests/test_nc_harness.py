"""Verify the shared NC harness (tests/_nc_harness.py) is xdist-safe:
it neuters via a throwaway sys.modules entry, never writes the shipped file,
and never mutates the canonical module object.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')


def test_module_name_from_path():
    from tests._nc_harness import module_name_from_path
    assert module_name_from_path(_BOARD_SRC) == 'lib.conversations.project_board'


def test_neuter_bites_and_leaves_no_trace():
    """The neuter must FLIP behavior inside the context and leave ZERO trace:
    the shipped file byte-identical, the canonical sys.modules object unchanged.
    """
    from tests._nc_harness import neutered_source

    with open(_BOARD_SRC, encoding='utf-8') as f:
        original_bytes = f.read()

    import lib.conversations.project_board as pb_before
    canonical_id = id(pb_before)

    # Real behavior: an expired claim reads 'open' (anti-deadlock).
    from lib.conversations.project_board import _effective_status
    now = 1_000_000
    assert _effective_status('claimed', now - 5000, now) == 'open'

    # Neuter the reclaim → inside the context an expired claim STAYS 'claimed'.
    with neutered_source(
        _BOARD_SRC,
        "    if stored_status == 'claimed' and lease_expires_at and lease_expires_at <= now_ms:\n        return 'open'\n    return stored_status",
        "    return stored_status  # NC (reclaim disabled)",
    ) as mod:
        assert mod._effective_status('claimed', now - 5000, now) == 'claimed', \
            'neuter must BITE: expired claim stays claimed with reclaim disabled'
        # The swapped module is the throwaway, not the canonical one.
        assert sys.modules['lib.conversations.project_board'] is mod
        assert id(mod) != canonical_id

    # After the context: canonical module restored verbatim, file untouched.
    assert sys.modules['lib.conversations.project_board'] is pb_before
    with open(_BOARD_SRC, encoding='utf-8') as f:
        assert f.read() == original_bytes, 'shipped source must be byte-identical'
    # And the canonical function still works (was never reloaded/mutated).
    assert _effective_status('claimed', now - 5000, now) == 'open'


def test_patch_restore_runs_closure_and_restores():
    """patch_restore calls the run() closure (0-arg form) under the neuter and
    restores afterward."""
    from tests._nc_harness import patch_restore

    seen = {}

    def run():
        import lib.conversations.project_board as pb
        # Inside: sys.modules entry is the neutered throwaway.
        seen['effective'] = pb._effective_status('claimed', 5, 10)

    patch_restore(
        _BOARD_SRC,
        "    if stored_status == 'claimed' and lease_expires_at and lease_expires_at <= now_ms:\n        return 'open'\n    return stored_status",
        "    return stored_status  # NC (reclaim disabled)",
        run,
    )
    assert seen['effective'] == 'claimed', 'closure saw the neutered module'
    # Restored: canonical reclaim works again.
    from lib.conversations.project_board import _effective_status
    assert _effective_status('claimed', 5, 10) == 'open'
