"""Container and frontend-CI build boundaries must fail loudly."""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding='utf-8')


def test_frontend_ci_uses_lockfile_without_fallback():
    workflow = _read('.github/workflows/ci.yml')
    assert 'run: npm ci\n' in workflow
    assert 'npm ci || npm install' not in workflow


def _frontend_job() -> str:
    workflow = _read('.github/workflows/ci.yml')
    start = workflow.index('\n  frontend:')
    end = workflow.index('\n  supply-chain:')
    return workflow[start:end]


def test_frontend_ci_installs_python_tools_before_contract_checks():
    job = _frontend_job()
    sync = job.index('uv sync --frozen --extra test')
    path = job.index('GITHUB_PATH')
    check = job.index('npm run check:frontend')
    assert 0 <= sync < path < check


def _dependency_stage() -> str:
    return _read('Dockerfile').split(
        'FROM ${PYTHON_IMAGE} AS python-dependencies', 1)[1].split('FROM ', 1)[0]


def test_docker_copies_workspace_members_before_frozen_sync():
    deps = _dependency_stage()
    sync = deps.index('uv sync --frozen')
    assert 0 <= deps.index('COPY packages/tofu-search') < sync
    assert 0 <= deps.index('COPY plugins/tofu-trading') < sync


def test_docker_dependency_stage_installs_the_toolchain_the_kernel_builds_with():
    """`tofu-repo-kernel` compiles its Rust helper, so the stage needs cargo.

    The kernel wheel is built in this stage, and its ``setup.py`` shells out to
    ``cargo build``. Debian ships ``cargo`` and ``rustc`` as separate packages,
    so installing only ``rustc`` let the image fail late with
    ``error: [Errno 2] No such file or directory: 'cargo'``. Keep both names
    pinned here so removing either one fails this contract instead of the
    release build.
    """
    deps = _dependency_stage()
    packages = deps[deps.index('apt-get install'):deps.index('rm -rf /var/lib/apt/lists/*')]
    installed = set(packages.replace('\\', ' ').split())
    assert {'gcc', 'g++', 'rustc', 'cargo'} <= installed
    # The kernel is built in this stage, so the toolchain must precede it.
    assert deps.index('cargo') < deps.index('uv build --wheel --out-dir /native-wheelhouse')


def test_docker_uv_pin_tracks_the_committed_lock():
    """A stale uv pin re-installs the release-gated version into the image."""
    import re

    deps = _dependency_stage()
    pinned = re.search(r'pip install --no-cache-dir uv==(\d+\.\d+\.\d+)', deps)
    assert pinned, 'the dependency stage must pin uv'
    import tomllib

    packages = tomllib.loads(_read('uv.lock'))['package']
    resolved = {p['name']: p['version'] for p in packages if p['name'] == 'uv'}
    assert resolved, 'uv.lock must resolve uv'
    locked = resolved['uv']
    assert pinned.group(1) == locked, (
        f'Dockerfile pins uv {pinned.group(1)} but uv.lock resolves {locked}'
    )


def test_docker_playwright_install_fails_loudly():
    dockerfile = _read('Dockerfile')
    assert 'python -m playwright install chromium --with-deps' in dockerfile
    assert 'playwright install chromium --with-deps 2>/dev/null || true' not in dockerfile


def test_docker_has_pinned_rootless_api_worker_and_agent_targets():
    dockerfile = _read('Dockerfile')
    instructions = '\n'.join(
        line for line in dockerfile.splitlines()
        if not line.lstrip().startswith('#'))
    assert (
        'python:3.12-slim@sha256:'
        '2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a'
    ) in dockerfile
    assert 'FROM runtime-base AS api' in dockerfile
    assert 'FROM runtime-base AS worker' in dockerfile
    assert 'FROM ${PYTHON_IMAGE} AS agent' in dockerfile
    assert dockerfile.count('USER tofu:tofu') == 3
    assert 'postgresql' not in instructions.lower()

    runtime = instructions.split('FROM ${PYTHON_IMAGE} AS runtime-base', 1)[1]
    api = runtime.split('FROM runtime-base AS api', 1)[1].split(
        'FROM runtime-base AS worker', 1)[0]
    assert 'gcc' not in runtime.split()  # libgcc-s1 is a runtime dependency
    assert 'g++' not in runtime
    assert 'playwright' not in api.lower()


def test_public_agent_image_contains_only_the_installed_runtime():
    dockerfile = _read('Dockerfile')
    agent = dockerfile.split('FROM ${PYTHON_IMAGE} AS agent', 1)[1]
    assert 'COPY --from=agent-builder' in agent
    assert 'COPY . .' not in agent
    assert 'playwright' not in agent.lower()


def test_docker_context_excludes_local_bulk_and_secrets():
    ignored = {
        line.strip() for line in _read('.dockerignore').splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    }
    required = {
        '.git', '.env', 'data/', 'logs/', 'uploads/', 'tests/', 'debug/*',
        'benchmarks/', 'codex/', 'evaluations/', 'node_modules/', 'node_modules.*/',
        'swebench_*/', '*.log', '.coverage', '.venv/', 'venv/', '.conda/',
        '.tofu_env.json', '.migrate_backup/', '.update_backup/',
        '.credentials_vault.key', 'build/', 'dist/', '*.db-wal', '*.db-shm',
    }
    assert required <= ignored
    assert '!README.md' in ignored
    assert '!packages/tofu-search/README.md' in ignored
    assert '!plugins/tofu-trading/README.md' in ignored
    assert '!debug/inspect_conversation.py' in ignored
