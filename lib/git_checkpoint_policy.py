"""Shared checkpoint contents and repository-scoped verification authority.

select_gate_commands is consumed by integration, delivery/status and linear
checkpoints. Local Git policy wins; environment defaults belong only to the
application Git common-dir (or an explicit operator root), never all projects.
No storage, scans, caches or background work participate in this decision.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess

from lib.git_runtime import git_executable


FORBIDDEN_CHECKPOINT_PARTS = frozenset({
    '.git', '.mypy_cache', '.playwright-cli', '.project_sessions',
    '.pytest_cache', '.ruff_cache', '.tofu_trash', '__pycache__',
    'node_modules',
})

# Root-owned runtime/reconstructible trees. A nested source package stays legal.
FORBIDDEN_CHECKPOINT_ROOTS = frozenset({
    '.eval-runs', 'abtest_workdir', 'data', 'eval-runs',
    'evaluation_results', 'logs', 'output', 'swebench_workdir', 'uploads',
})

PROJECT_GATE_REQUIRED_SUFFIXES = frozenset({
    '.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.rs', '.go',
    '.java', '.kt', '.swift', '.rb', '.php', '.ini', '.cfg', '.env',
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
    return [path for path in paths
            if Path(normalize_checkpoint_path(path)).suffix.lower()
            in PROJECT_GATE_REQUIRED_SUFFIXES
            or Path(normalize_checkpoint_path(path)).name.lower()
            in {'dockerfile', 'makefile', 'gnumakefile'}
            or Path(normalize_checkpoint_path(path)).name.lower().startswith('.env')]


class GatePolicyError(RuntimeError):
    """Repository identity or configured policy could not be safely selected."""


@dataclass(frozen=True)
class GateCommands:
    integration: str
    stable: str
    linear: str
    stable_explicit: bool


def git_policy_environment() -> dict[str, str]:
    """Drop ambient Git routing/config injection; callers may add owned inputs."""
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith('GIT_')}


def _policy_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [git_executable(), *args], cwd=str(root),
            env=git_policy_environment(), text=True, capture_output=True,
            timeout=10, check=False)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise GatePolicyError(f'Git gate policy read failed: {error}') from error


def _common_dir(root: Path, *, optional: bool = False) -> Path | None:
    if optional and not (root / '.git').exists():
        return None  # A nested installed copy cannot borrow its enclosing Git.
    result = _policy_git(root, ['rev-parse', '--show-toplevel', '--git-common-dir', '--git-dir'])
    if result.returncode:
        raise GatePolicyError('Git gate policy identity check failed')
    lines = result.stdout.splitlines()
    if len(lines) != 3 or not all(lines):
        raise GatePolicyError('Git gate policy identity response is ambiguous')
    top = Path(lines[0]).resolve()
    common = (root / lines[1]).resolve()
    if not top.is_dir() or not common.is_dir() or not root.is_relative_to(top):
        raise GatePolicyError('Git gate policy identity is invalid')
    if optional and top != root:
        return None
    dot_git = top / '.git'
    if dot_git.is_symlink():
        raise GatePolicyError('Git gate policy checkout anchor is symlinked')
    admin = (root / lines[2]).resolve()
    if not dot_git.exists():
        raise GatePolicyError('Git gate policy checkout anchor is missing')
    if dot_git.is_dir() and (dot_git.resolve() != admin or admin != common):
        raise GatePolicyError('Git gate policy checkout identity is inconsistent')
    if dot_git.is_file():
        try:
            # Linked-worktree identity needs its own registered backlink;
            # copying an application's .git pointer does not transfer policy.
            with (admin / 'gitdir').open(encoding='utf-8') as stream:
                backlink = stream.read(4097).strip()
            with (admin / 'commondir').open(encoding='utf-8') as stream:
                linked_common = stream.read(4097).strip()
            if (not backlink or not linked_common or len(backlink) > 4096
                    or len(linked_common) > 4096
                    or Path(backlink).resolve() != dot_git.resolve()
                    or (admin / linked_common).resolve() != common):
                raise GatePolicyError('Git gate policy worktree identity is invalid')
        except (OSError, UnicodeError) as error:
            raise GatePolicyError('Git gate policy worktree backlink failed') from error
    return common


def _command(value: str, source: str) -> str:
    value = value.strip()
    if not value or len(value) > 16384 or any(ord(c) < 32 for c in value):
        raise GatePolicyError(f'Invalid or empty Git gate command: {source}')
    try:
        argv = shlex.split(value)
    except ValueError as error:
        raise GatePolicyError(f'Invalid Git gate command {source}: {error}') from error
    if not argv or not argv[0]:
        raise GatePolicyError(f'Empty Git gate argv: {source}')
    return value


def select_gate_commands(root: str | Path) -> GateCommands:
    """Select commands only for this Git common-dir; errors never mean unset.

    Only direct local config (no includes/global/worktree config) is trusted.
    Duplicate/empty/malformed local entries fail closed, even with an env gate.
    Stable/linear use their explicit command or the selected integration gate.
    """
    root = Path(root).expanduser().resolve()
    common = _common_dir(root)
    keys = {'tofu.integrationtestcommand': 'integration',
            'tofu.integrationstabletestcommand': 'stable',
            'tofu.linearcheckpointtestcommand': 'linear'}
    result = _policy_git(root, [
        'config', '--local', '--no-includes', '--null', '--get-regexp',
        r'^tofu\.(integrationtestcommand|integrationstabletestcommand|linearcheckpointtestcommand)$'])
    if result.returncode not in (0, 1) or (result.returncode == 1 and (result.stdout or result.stderr)):
        raise GatePolicyError('Git local gate config read failed')
    local: dict[str, str] = {}
    for entry in result.stdout.split('\0'):
        if not entry:
            continue
        key, separator, value = entry.partition('\n')
        kind = keys.get(key.lower())
        if not separator or not kind or kind in local:
            raise GatePolicyError('Git local gate config is unknown or ambiguous')
        local[kind] = _command(value, key)
    pin = os.environ.get('TOFU_INTEGRATION_TEST_ROOT')
    if pin is not None and not pin.strip():
        raise GatePolicyError('TOFU_INTEGRATION_TEST_ROOT is empty')
    source = Path(pin).expanduser().resolve() if pin is not None else Path(__file__).resolve().parent.parent
    scope = _common_dir(source, optional=pin is None)
    selected = dict(local)
    if scope == common:
        for kind, name in (
            ('integration', 'TOFU_INTEGRATION_TEST_CMD'),
            ('stable', 'TOFU_INTEGRATION_STABLE_TEST_CMD'),
            ('linear', 'TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD'),
        ):
            value = os.environ.get(name, '').strip()
            if kind not in selected and value:
                selected[kind] = _command(value, name)
    integration = selected.get('integration', '')
    return GateCommands(integration, selected.get('stable', integration),
                        selected.get('linear', integration), bool(selected.get('stable')))


def require_unchanged_gates(root: str | Path, expected: GateCommands) -> None:
    """Bounded freshness check; external config writers are not atomically fenced."""
    if select_gate_commands(root) != expected:
        raise GatePolicyError('Git gate policy changed during verification')


__all__ = [
    'FORBIDDEN_CHECKPOINT_PARTS', 'FORBIDDEN_CHECKPOINT_ROOTS',
    'PROJECT_GATE_REQUIRED_SUFFIXES', 'forbidden_checkpoint_paths',
    'normalize_checkpoint_path', 'semantic_gate_paths', 'GatePolicyError',
    'GateCommands', 'git_policy_environment', 'select_gate_commands',
    'require_unchanged_gates',
]
