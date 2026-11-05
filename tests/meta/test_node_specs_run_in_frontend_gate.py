"""Node and co-located TypeScript specs run in the shared frontend gate.

Inputs: tracked ``*.test.cjs`` files and the ``check:node-tests`` /
``check:frontend`` package scripts. Node specs are collected by neither pytest
nor Vitest, so a spec that no package script reaches silently rots: a stale
harness keeps "passing" by never running, and the regressions it exists to
catch ship. The gate owns specs by directory glob; this check rejects a spec
outside those globs instead of trusting authors to register each file.
The complete Vitest owner suite must also run locally, through the same gate
used by CI rather than a CI-only follow-up command.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _scripts() -> dict[str, str]:
    return json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['scripts']


def _tracked_node_specs() -> list[str]:
    out = subprocess.run(['git', 'ls-files', '-z', '--', '*.test.cjs'], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    # An unstaged deletion stays in ls-files but is absent from the gate's run.
    return [path for path in out.stdout.split('\0')
            if path and os.path.isfile(ROOT / path)]


def test_every_tracked_node_spec_is_matched_by_the_node_gate():
    specs = _tracked_node_specs()
    assert specs, 'git ls-files found no Node specs; not a real checkout?'
    patterns = [token for token in _scripts()['check:node-tests'].split()
                if token.endswith('.test.cjs')]
    # The shell expands these single-level globs; Path.glob has the same semantics.
    matched = {path.relative_to(ROOT).as_posix()
               for pattern in patterns for path in ROOT.glob(pattern)}
    unmatched = sorted(set(specs) - matched)
    assert not unmatched, (
        'Node specs outside every check:node-tests glob never run in CI; move '
        'them under a gated directory or add their directory glob: '
        + ', '.join(unmatched))


def test_frontend_gate_runs_the_node_specs():
    assert 'npm run check:node-tests' in _scripts()['check:frontend']


def test_frontend_gate_runs_all_colocated_unit_specs():
    scripts = _scripts()
    commands = [shlex.split(command) for command in scripts['check:frontend'].split('&&')]
    assert ['npm', 'run', 'test:unit'] in commands, (
        'The local frontend gate must execute every co-located owner spec; '
        'a feature filter or separate CI command leaves local verification incomplete'
    )
    assert scripts['test:unit'] == 'vitest run', (
        'The shared unit entrypoint must use the complete Vitest configuration '
        'without a feature filter, concurrency override or isolation override'
    )
