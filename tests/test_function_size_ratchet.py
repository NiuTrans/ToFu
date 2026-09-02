"""Long-function ratchet born from the 2026-08-13 repository audit incident.

The audit found that file splitting had hidden five 860–1,039 line functions:
the module names looked modular, but the actual reasoning units were still
monoliths. This pins the current worst functions so they may shrink but cannot
grow. A function that shrinks makes the baseline intentionally loose and turns
this test red until the recorded budget is lowered.

NEUTER: increasing one function budget or appending statements inside one of
these functions must fail the tightness or growth assertion respectively.
"""

from __future__ import annotations

import pytest

from scripts.check_function_sizes import audit_function_sizes


pytestmark = pytest.mark.unit


def test_long_function_budgets_are_tight_and_never_grow():
    result = audit_function_sizes()

    assert not result.stale_entries, (
        'Function-size ratchet has stale entries; remove them only after the '
        'function was genuinely split:\n  '
        + '\n  '.join(result.stale_entries))
    assert not result.changed_entries, (
        'Long-function budget changed. Growth is forbidden; shrinkage is good '
        'but must lower the baseline in the checker so the earned headroom '
        'cannot be silently spent later:\n  '
        + '\n  '.join(result.changed_entries))
