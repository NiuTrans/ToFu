"""Long-function ratchet born from the 2026-08-13 repository audit incident.

The audit found that file splitting had hidden five 860–1,039 line functions:
the module names looked modular, but the actual reasoning units were still
monoliths. This pins every function of at least the threshold's length so they
may shrink but cannot grow. A function that shrinks makes the baseline
intentionally loose and turns this test red until the recorded budget is
lowered. The checker discovers long functions itself: a budget list of named
functions let unlisted ones grow unnoticed (``_handle_project_tool`` went from
824 to 841 lines between 2026-10-22 and 2026-10-27 while only six functions
carried budgets).

NEUTER: increasing one function budget, appending statements inside one of
these functions, or deleting one budget entry must fail the tightness, growth
or unbudgeted assertion respectively.
"""

from __future__ import annotations

import subprocess

import pytest

from scripts.check_function_sizes import (
    FUNCTION_LINE_THRESHOLD,
    _unique_budgets,
    audit_function_sizes,
)


pytestmark = pytest.mark.unit


def test_budget_registry_refuses_a_repeated_function():
    # Concurrent edits that both budget one function merge cleanly; the
    # registry must refuse the repeat instead of keeping the later value.
    assert _unique_budgets(('pkg/a.py', 'f', 600), ('pkg/a.py', 'g', 601)) == {
        ('pkg/a.py', 'f'): 600, ('pkg/a.py', 'g'): 601}
    with pytest.raises(ValueError, match=(
            r'^pkg/a.py:f has more than one budget; keep one entry, then run '
            r'python3 scripts/check_function_sizes.py to check its size$')):
        _unique_budgets(('pkg/a.py', 'f', 600), ('pkg/a.py', 'f', 601))


def test_long_function_budgets_are_tight_and_never_grow():
    result = audit_function_sizes()

    assert not result.stale_entries, (
        'Function-size budgets name functions that no longer exist:\n  '
        + '\n  '.join(result.stale_entries))
    assert not result.changed_entries, (
        'Long-function budget changed. Growth is forbidden; shrinkage is good '
        'but must lower the baseline in the checker so the earned headroom '
        'cannot be silently spent later:\n  '
        + '\n  '.join(result.changed_entries))
    assert not result.unbudgeted_entries, (
        f'Function(s) of {FUNCTION_LINE_THRESHOLD}+ lines without a budget. '
        'Split them below the threshold instead of adding a new long '
        'reasoning unit:\n  '
        + '\n  '.join(result.unbudgeted_entries))


def _function(name: str, lines: int, indent: str = '') -> str:
    body = ''.join(f'{indent}    value_{index} = {index}\n' for index in range(lines - 1))
    return f'{indent}def {name}():\n{body}'


def test_discovery_requires_an_exact_budget_for_every_long_function(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, timeout=60)
    threshold = FUNCTION_LINE_THRESHOLD
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'pkg' / 'units.py').write_text(
        _function('long_unit', threshold)
        + _function('short_unit', threshold - 1)
        + 'class Owner:\n' + _function('method', threshold + 1, indent='    '),
        encoding='utf-8')
    (tmp_path / '.gitignore').write_text('ignored/\n', encoding='utf-8')
    (tmp_path / 'ignored').mkdir()
    (tmp_path / 'ignored' / 'scratch.py').write_text(
        _function('ignored_unit', threshold), encoding='utf-8')

    unbudgeted = audit_function_sizes(tmp_path, {})
    remedy = (f'split it below {threshold} lines (if it was moved or renamed, '
              'move its old budget to this path and name instead)')
    assert unbudgeted.unbudgeted_entries == (
        f'pkg/units.py:Owner.method has {threshold + 1} lines '
        f'(threshold {threshold}) and no budget; {remedy}',
        f'pkg/units.py:long_unit has {threshold} lines '
        f'(threshold {threshold}) and no budget; {remedy}',
    )
    assert not unbudgeted.ok

    exact = {('pkg/units.py', 'long_unit'): threshold,
             ('pkg/units.py', 'Owner.method'): threshold + 1}
    assert audit_function_sizes(tmp_path, exact).ok

    loose = audit_function_sizes(tmp_path, {
        **exact, ('pkg/units.py', 'long_unit'): threshold + 1,
        ('pkg/units.py', 'short_unit'): threshold})
    assert loose.changed_entries == (
        f'pkg/units.py:long_unit SHRANK: budget={threshold + 1}, '
        f'actual={threshold}; lower its budget to {threshold}',
        f'pkg/units.py:short_unit SHRANK: budget={threshold}, '
        f'actual={threshold - 1}; lower its budget to {threshold - 1}, or '
        f'delete the budget now that the function is below {threshold} lines',
    )
    grown = audit_function_sizes(tmp_path, {**exact, ('pkg/units.py', 'long_unit'): threshold - 1})
    assert grown.changed_entries == (
        f'pkg/units.py:long_unit GREW: budget={threshold - 1}, '
        f'actual={threshold}; split it until it has at most {threshold - 1} '
        'lines (a budget is never raised)',)
    assert not loose.unbudgeted_entries and not grown.unbudgeted_entries

    stale = audit_function_sizes(tmp_path, {**exact, ('pkg/gone.py', 'unit'): threshold})
    assert stale.stale_entries == (
        'pkg/gone.py:unit no longer exists; delete its budget, or move the '
        'budget to the new path or name if the function was moved or renamed',)


def test_git_enumeration_failure_is_never_a_clean_audit(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        audit_function_sizes(tmp_path, {})
