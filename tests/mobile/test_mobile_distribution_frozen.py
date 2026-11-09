"""The frozen desktop server must carry the same published mobile receipt."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import shutil

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_frozen_data_list_carries_the_mobile_receipt_at_its_runtime_path(tmp_path):
    tree = ast.parse((ROOT / 'tofu.spec').read_text(encoding='utf-8'))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == '_candidate_datas'
                              for target in node.targets))
    # Execute only the declarative data list, not PyInstaller/native build work.
    data = eval(compile(ast.Expression(assignment.value), '<frozen-data-list>', 'eval'),
                {'os': os, 'ROOT': str(ROOT)})
    bundled = tmp_path / '_internal'
    for source, destination in data:
        if source == str(ROOT / 'android/published-release.json'):
            target = bundled / destination / Path(source).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    assert (bundled / 'android/published-release.json').is_file(), (
        'Frozen health metadata cannot read a publication receipt omitted from the data list')


def test_package_receipt_path_resolves_at_the_frozen_bundle_root(tmp_path):
    """Moving policy into a package must not move its artifact authority."""
    source = ROOT / 'lib/mobile_distribution/__init__.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'PUBLISHED_RELEASE_PATH'
                              for target in node.targets))
    bundle = tmp_path / '_internal'
    frozen_module = bundle / 'lib/mobile_distribution/__init__.py'
    receipt = eval(compile(ast.Expression(assignment.value), '<publication-path>', 'eval'),
                   {'Path': Path, '__file__': str(frozen_module)})
    assert receipt == bundle / 'android/published-release.json'
