"""Test modules imported by basename never share one.

A test directory without ``__init__.py`` (every ``packages/*/tests`` and
``plugins/*/tests``, and a few suite directories) has its modules imported
under their bare file name, so two equal names abort a combined run with
"import file mismatch" (search's and trading's ``test_demand_workers.py``).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_basename_imported_test_modules_are_unique():
    tracked = subprocess.run(
        ['git', 'ls-files', '-z', '--', 'tests', 'packages/*/tests/*', 'plugins/*/tests/*'],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout.split('\0')
    owners = defaultdict(list)
    for name in filter(None, tracked):
        path = Path(name)
        if (path.suffix == '.py' and path.name.startswith('test_')
                and not (ROOT / path.parent / '__init__.py').is_file()):
            owners[path.name].append(name)
    assert {name: paths for name, paths in owners.items() if len(paths) > 1} == {}
