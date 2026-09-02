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
_POLICY_SRC = os.path.join(
    ROOT, 'lib', 'conversations', 'project_board_policy.py')


def test_module_name_from_path():
    from tests._nc_harness import module_name_from_path
    assert module_name_from_path(
        _POLICY_SRC) == 'lib.conversations.project_board_policy'


def test_neuter_bites_and_leaves_no_trace():
    """The neuter must FLIP behavior inside the context and leave ZERO trace:
    the shipped file byte-identical, the canonical sys.modules object unchanged.
    """
    from tests._nc_harness import neutered_source

    with open(_POLICY_SRC, encoding='utf-8') as f:
        original_bytes = f.read()

    import lib.conversations.project_board_policy as pb_before
    canonical_id = id(pb_before)

    # Real behavior: an expired claim reads 'open' (anti-deadlock).
    from lib.conversations.project_board_policy import effective_board_status
    now = 1_000_000
    assert effective_board_status('claimed', now - 5000, now) == 'open'

    # Neuter the reclaim → inside the context an expired claim STAYS 'claimed'.
    with neutered_source(
        _POLICY_SRC,
        '    if (\n'
        '        stored_status == "claimed"\n'
        '        and lease_expires_at\n'
        '        and lease_expires_at <= current_time_ms\n'
        '    ):\n'
        '        return "open"\n'
        '    return stored_status',
        "    return stored_status  # NC (reclaim disabled)",
    ) as mod:
        assert mod.effective_board_status(
            'claimed', now - 5000, now) == 'claimed', \
            'neuter must BITE: expired claim stays claimed with reclaim disabled'
        # The swapped module is the throwaway, not the canonical one.
        assert sys.modules['lib.conversations.project_board_policy'] is mod
        assert id(mod) != canonical_id

    # After the context: canonical module restored verbatim, file untouched.
    assert sys.modules['lib.conversations.project_board_policy'] is pb_before
    with open(_POLICY_SRC, encoding='utf-8') as f:
        assert f.read() == original_bytes, 'shipped source must be byte-identical'
    # And the canonical function still works (was never reloaded/mutated).
    assert effective_board_status('claimed', now - 5000, now) == 'open'


def test_patch_restore_runs_closure_and_restores():
    """patch_restore calls the run() closure (0-arg form) under the neuter and
    restores afterward."""
    from tests._nc_harness import patch_restore

    seen = {}

    def run():
        import lib.conversations.project_board_policy as pb
        # Inside: sys.modules entry is the neutered throwaway.
        seen['effective'] = pb.effective_board_status('claimed', 5, 10)

    patch_restore(
        _POLICY_SRC,
        '    if (\n'
        '        stored_status == "claimed"\n'
        '        and lease_expires_at\n'
        '        and lease_expires_at <= current_time_ms\n'
        '    ):\n'
        '        return "open"\n'
        '    return stored_status',
        "    return stored_status  # NC (reclaim disabled)",
        run,
    )
    assert seen['effective'] == 'claimed', 'closure saw the neutered module'
    # Restored: canonical reclaim works again.
    from lib.conversations.project_board_policy import effective_board_status
    assert effective_board_status('claimed', 5, 10) == 'open'
