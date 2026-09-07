"""Container and frontend-CI build boundaries must fail loudly."""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding='utf-8')


def test_frontend_ci_uses_lockfile_without_fallback():
    workflow = _read('.github/workflows/ci.yml')
    assert 'run: npm ci\n' in workflow
    assert 'npm ci || npm install' not in workflow


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
    assert 'gcc' not in runtime
    assert 'g++' not in runtime
    assert 'playwright' not in api.lower()


def test_public_agent_image_contains_only_the_installed_runtime():
    dockerfile = _read('Dockerfile')
    agent = dockerfile.split('FROM ${PYTHON_IMAGE} AS agent', 1)[1]
    dependency_install = dockerfile.split(
        'UV_PROJECT_ENVIRONMENT=/opt/tofu-agent', 1)[1].split('\n\n', 1)[0]

    assert 'COPY . .' not in agent
    assert 'COPY --from=agent-builder' in agent
    assert '/opt/tofu-agent /opt/tofu-agent' in agent
    assert '--extra app' not in dependency_install
    assert '--extra storage' not in dependency_install
    assert 'EXPOSE 15001' in agent
    assert 'CMD ["tofu-agent", "serve"]' in agent
    assert '/app/data' not in agent
    assert 'TOFU_AGENT_CONFIG_PATH=/home/tofu/.config/tofu-agent/provider.json' in agent
    assert '/home/tofu/.config/tofu-agent' in agent


def test_personal_compose_targets_browser_capable_worker_image():
    compose = _read('docker-compose.yml')
    assert 'target: worker' in compose


def test_ci_builds_and_inspects_both_container_targets():
    workflow = _read('.github/workflows/ci.yml')
    assert 'target: [api, worker]' in workflow
    assert 'docker build' in workflow
    assert '--target ${{ matrix.target }}' in workflow
    assert "'{{.Config.User}}'" in workflow
    assert "! command -v pg_ctl" in workflow
    assert "find_spec('playwright') is None" in workflow


def test_ci_generates_cyclonedx_and_gates_source_and_image_vulnerabilities():
    workflow = _read('.github/workflows/ci.yml')
    pinned_trivy = (
        'aquasecurity/trivy-action@'
        'ed142fd0673e97e23eac54620cfb913e5ce36c25')

    assert 'supply-chain:' in workflow
    assert workflow.count(pinned_trivy) == 5
    assert workflow.count('format: cyclonedx') == 2
    assert 'output: tofu-repository.cdx.json' in workflow
    assert 'output: tofu-${{ matrix.target }}.cdx.json' in workflow
    assert 'scanners: vuln,misconfig' in workflow
    assert 'scanners: secret' in workflow
    assert 'Gate checked-in secrets at every scanner severity' in workflow
    assert 'TRIVY_HELM_KUBE_VERSION: "1.28.0"' in workflow
    assert 'TRIVY_HELM_SET_STRING:' in workflow
    assert 'images.api.digest=sha256:' in workflow
    assert 'images.worker.digest=sha256:' in workflow
    assert workflow.count("severity: HIGH,CRITICAL") == 2
    assert workflow.count("exit-code: '1'") >= 3
    assert 'ignore-unfixed: false' in workflow
    assert 'if-no-files-found: error' in workflow


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
    assert '!debug/inspect_conversation.py' in ignored
