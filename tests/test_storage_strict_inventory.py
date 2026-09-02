"""Direct storage-capability ratchet outside the Sidecar.

The live boundary rejects driver and SQL ownership in application code. This
suite separately pins the reviewed offline operator tools reported by
:func:`lib.storage_boundary.strict_inventory` to
``tests/fixtures/storage_strict_inventory_budget.json``:

* any file whose capability count *increases* fails;
* any *new* file with storage capabilities fails;
* the total can never exceed the committed budget.

Cleanup slices lower the budget in the same commit (see the regeneration
command in the fixture's ``description`` field).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUDGET_PATH = ROOT / 'tests' / 'fixtures' / (
    'storage_strict_inventory_budget.json')
pytestmark = pytest.mark.unit


def _budget() -> dict[str, object]:
    payload = json.loads(BUDGET_PATH.read_text(encoding='utf-8'))
    assert isinstance(payload.get('files'), dict)
    assert isinstance(payload.get('total'), int)
    return payload


def test_strict_inventory_never_exceeds_budget():
    from lib.storage_boundary import strict_inventory

    budget = _budget()
    inventory = strict_inventory(ROOT)
    counts: dict[str, int] = inventory['files']
    budget_files: dict[str, int] = budget['files']

    new_files = sorted(set(counts) - set(budget_files))
    assert not new_files, (
        'new files with storage capabilities entered the strict inventory; '
        'route them through StorageClient or extend the budget deliberately: '
        + ', '.join(new_files))

    increases = {
        path: (budget_files[path], counts[path])
        for path in counts
        if path in budget_files and counts[path] > budget_files[path]
    }
    assert not increases, (
        'storage capability counts regressed (budget, actual): '
        + json.dumps(increases, indent=1))

    assert inventory['total'] <= budget['total'], (
        f"strict inventory total {inventory['total']} exceeds budget "
        f"{budget['total']}")


def test_budget_file_is_sorted_and_consistent():
    budget = _budget()
    files: dict[str, int] = budget['files']
    assert list(files) == sorted(files), 'budget files must be sorted'
    assert all(isinstance(v, int) and v > 0 for v in files.values())
    assert budget['total'] == sum(files.values()), (
        'budget total must equal the per-file sum')
