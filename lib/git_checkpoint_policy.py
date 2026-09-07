"""Shared policy for Git checkpoint contents and verification scope.

This module is the single source of truth for paths that may not enter a Tofu
checkpoint and for suffixes that require a semantic project gate.  Both the
isolated integration controller and the single-checkout linear checkpoint
runtime consume it; neither packaging nor task orchestration redefines the
policy.
"""

from __future__ import annotations

from pathlib import Path


FORBIDDEN_CHECKPOINT_PARTS = frozenset({
    '.git', '.mypy_cache', '.playwright-cli', '.project_sessions',
    '.pytest_cache', '.ruff_cache', '.tofu_trash', '__pycache__',
    'node_modules',
})

# Root-owned runtime/reconstructible trees.  A same-named nested source package
# remains legal (for example ``android/.../data``); matching is root-anchored.
FORBIDDEN_CHECKPOINT_ROOTS = frozenset({
    '.eval-runs', 'abtest_workdir', 'data', 'eval-runs',
    'evaluation_results', 'logs', 'output', 'swebench_workdir', 'uploads',
})

PROJECT_GATE_REQUIRED_SUFFIXES = frozenset({
    '.cjs', '.css', '.html', '.js', '.json', '.jsx', '.mjs', '.py',
    '.sh', '.sql', '.toml', '.ts', '.tsx', '.yaml', '.yml',
})


def normalize_checkpoint_path(path: str) -> str:
    """Return one repository-relative path in canonical slash form."""
    normalized = str(path or '').replace('\\', '/').strip('/')
    while normalized.startswith('./'):
        normalized = normalized[2:]
    return normalized


def forbidden_checkpoint_paths(paths: list[str]) -> list[str]:
    """Return generated, dependency, or runtime paths that may not land."""
    refused: list[str] = []
    for raw in paths:
        normalized = normalize_checkpoint_path(raw)
        parts = tuple(part for part in normalized.split('/') if part)
        if (
            not normalized
            or (parts and parts[0] in FORBIDDEN_CHECKPOINT_ROOTS)
            or any(part in FORBIDDEN_CHECKPOINT_PARTS for part in parts)
            or normalized.endswith(('.pyc', '.pyo'))
            or normalized.startswith('data/integration/worktrees/')
        ):
            refused.append(raw)
    return refused[:64]


def semantic_gate_paths(paths: list[str]) -> list[str]:
    """Return changed paths whose suffix requires a semantic project gate."""
    return [
        path for path in paths
        if Path(normalize_checkpoint_path(path)).suffix.lower()
        in PROJECT_GATE_REQUIRED_SUFFIXES
    ]


__all__ = [
    'FORBIDDEN_CHECKPOINT_PARTS',
    'FORBIDDEN_CHECKPOINT_ROOTS',
    'PROJECT_GATE_REQUIRED_SUFFIXES',
    'forbidden_checkpoint_paths',
    'normalize_checkpoint_path',
    'semantic_gate_paths',
]
