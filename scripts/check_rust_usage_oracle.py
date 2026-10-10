#!/usr/bin/env python3
"""Read-only canonical usage parity gate for the native captured input oracle.

Entry main; dependencies current lib.cost and native fixture JSON. No stores,
provider requests, pricing arithmetic or source writes. Bounds fixture bytes.
"""
import ast
import json
import logging
from pathlib import Path
from typing import Optional
ROOT = Path(__file__).resolve().parents[1]


def canonical_observer():
    """Execute unchanged pure authority bodies without importing app/pricing."""
    names = {'_USAGE_KEY_ALIASES', '_USAGE_NESTED_ALIASES', '_ANTHROPIC_RESIDUAL_KEYS',
             'normalize_usage', 'usage_cache_convention', '_nested_cached',
             '_anthropic_residual_input', 'split_input_tokens', 'observed_usage_totals'}
    source = ROOT / 'lib/cost.py'
    tree = ast.parse(source.read_text())
    nodes = [node for node in tree.body if
             (isinstance(node, ast.FunctionDef) and node.name in names) or
             (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names)]
    if len(nodes) != len(names):
        raise RuntimeError('canonical usage authority declarations changed')
    scope = {'Optional': Optional, 'logger': logging.getLogger(__name__)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), scope)
    return scope['observed_usage_totals']


def main():
    observed_usage_totals = canonical_observer()
    path = ROOT / 'packages/tofu-runtime/tests/fixtures/python_usage_observations.json'
    if path.stat().st_size > 2 * 1024 * 1024:
        raise SystemExit('native usage fixture exceeds 2 MiB gate bound')
    fixture = json.loads(path.read_text())
    for ordinal, case in enumerate(fixture['cases']):
        actual = observed_usage_totals(case['usage'])
        if actual != (case['tokens'], case['complete']):
            raise SystemExit(f'canonical usage oracle drift at case {ordinal}')
    print(f"{len(fixture['cases'])} current canonical usage observations match")


if __name__ == '__main__':
    main()
