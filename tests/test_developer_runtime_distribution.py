"""Static release contracts for the clone-free developer runtime."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tomllib

import pytest
import yaml

from scripts.check_developer_runtime_artifacts import (
    FORBIDDEN_MEMBERS,
    FORBIDDEN_PREFIXES,
    REQUIRED_MEMBERS,
    validate_sdist,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
_AUDIT_SYNTHETIC_REPO_PATHS = frozenset({
    '../outside.py',
    'audit_codex_session.py',
    'lib/database/secret.py',
    'lib/storage_sidecar/secret.py',
})


def _toml(relative_path: str) -> dict:
    return tomllib.loads(
        (ROOT / relative_path).read_text(encoding='utf-8'))


def _write_test_sdist(path: Path, relative_members: set[str]) -> None:
    distribution_root = 'tofu_agent-0.17.0'
    with tarfile.open(path, mode='w:gz') as archive:
        root_info = tarfile.TarInfo(distribution_root)
        root_info.type = tarfile.DIRTYPE
        archive.addfile(root_info)
        for relative_name in sorted(relative_members):
            archive.addfile(tarfile.TarInfo(
                f'{distribution_root}/{relative_name}'))


def test_public_artifacts_share_one_release_version():
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    assert _toml('pyproject.toml')['project']['version'] == version
    assert _toml('clients/python/pyproject.toml')['project']['version'] == version
    typescript = json.loads(
        (ROOT / 'clients/typescript/package.json').read_text(encoding='utf-8'))
    assert typescript['version'] == version
    assert typescript['devDependencies']['typescript']
    assert (ROOT / 'clients/typescript/package-lock.json').is_file()


def test_default_python_distribution_excludes_application_storage():
    project = _toml('pyproject.toml')
    metadata = project['project']
    dependencies = {
        re.split(r'[\[<>=!~ ]', value.lower(), maxsplit=1)[0]
        for value in metadata['dependencies']
    }

    assert metadata['name'] == 'tofu-agent'
    assert metadata['scripts']['tofu-agent'] == 'tofu_agent.cli:main'
    assert dependencies.isdisjoint({'sqlalchemy', 'psycopg', 'uv'})
    assert all(
        not value.startswith('sqlalchemy')
        for values in metadata['optional-dependencies'].values()
        for value in values
    )
    package_find = project['tool']['setuptools']['packages']['find']
    assert package_find['namespaces'] is False
    assert project['tool']['setuptools']['include-package-data'] is False
    for excluded in (
        'routes*', 'lib.database*', 'lib.storage', 'lib.storage.*',
        'lib.storage_sidecar*', 'lib.tests*',
    ):
        assert excluded in package_find['exclude']

    manifest = (ROOT / 'MANIFEST.in').read_text(encoding='utf-8')
    for pruned in (
        'routes', 'tests', 'frontend', 'static',
        'lib/database', 'lib/storage', 'lib/storage_sidecar', 'lib/tests',
    ):
        assert f'prune {pruned}' in manifest
    assert 'exclude audit_codex_session.py' in manifest
    assert 'audit_codex_session.py' in FORBIDDEN_MEMBERS
    assert 'lib/database/' in FORBIDDEN_PREFIXES
    assert 'lib/storage_sidecar/' in FORBIDDEN_PREFIXES


def test_model_metadata_is_package_owned_not_frontend_owned():
    package_template = ROOT / 'lib/model_info/data/openai.json'

    assert package_template.is_file()
    packaged = json.loads(package_template.read_text(encoding='utf-8'))
    assert packaged['key'] == 'openai'
    assert not (ROOT / 'static/provider_templates/openai.json').exists()


def test_agent_package_owns_only_the_small_provider_control_plane_assets():
    project = _toml('pyproject.toml')
    package_data = project['tool']['setuptools']['package-data']['tofu_agent']
    assert package_data == [
        'setup_ui/*.html', 'setup_ui/*.css', 'setup_ui/*.js',
    ]
    setup_dir = ROOT / 'tofu_agent/setup_ui'
    assert {path.name for path in setup_dir.iterdir()} == {
        'index.html', 'setup.css', 'setup.js',
    }


def test_release_build_backend_and_validation_tools_are_lock_owned():
    project = _toml('pyproject.toml')
    python_client = _toml('clients/python/pyproject.toml')
    release = project['project']['optional-dependencies']['release']

    for dependency in ('build', 'twine', 'setuptools', 'wheel'):
        assert any(item.startswith(dependency) for item in release)
    assert project['build-system']['requires'] == [
        'setuptools==80.10.2', 'wheel==0.48.0']
    assert python_client['build-system']['requires'] == [
        'setuptools==80.10.2', 'wheel==0.48.0']


def test_release_artifact_checker_accepts_the_current_sdist_boundary(tmp_path):
    sdist = tmp_path / 'tofu_agent-0.17.0.tar.gz'
    _write_test_sdist(sdist, set(REQUIRED_MEMBERS) | {'PKG-INFO'})

    report = validate_sdist(sdist)

    assert report['sdist'] == sdist.name
    assert report['leaked_members'] == 0


@pytest.mark.parametrize(
    ('malicious_member', 'error_pattern'),
    [
        ('audit_codex_session.py', 'leaked excluded application members'),
        ('lib/database/secret.py', 'leaked excluded application members'),
        ('lib/storage_sidecar/secret.py', 'leaked excluded application members'),
        ('../outside.py', 'contains a non-canonical path'),
    ],
)
def test_release_artifact_checker_rejects_malicious_sdist_members(
        tmp_path,
        malicious_member,
        error_pattern,
):
    sdist = tmp_path / 'tofu_agent-0.17.0.tar.gz'
    members = set(REQUIRED_MEMBERS) | {malicious_member}
    _write_test_sdist(sdist, members)

    with pytest.raises(ValueError, match=error_pattern):
        validate_sdist(sdist)


def test_storage_only_tool_handlers_do_not_break_the_agent_wheel():
    """A missing durable storage package must only remove that one handler."""
    source = (ROOT / 'lib/tasks_pkg/handlers/__init__.py').read_text(
        encoding='utf-8')
    tree = ast.parse(source)

    def imports_tool_result_artifacts(node: ast.AST) -> bool:
        return any(
            isinstance(candidate, ast.ImportFrom)
            and candidate.module == 'lib.tasks_pkg.handlers'
            and any(alias.name == 'tool_result_artifacts'
                    for alias in candidate.names)
            for candidate in ast.walk(node)
        )

    assert not any(
        isinstance(node, ast.ImportFrom)
        and imports_tool_result_artifacts(node)
        for node in tree.body
    )
    guarded_imports = [
        node for node in tree.body
        if isinstance(node, ast.Try) and imports_tool_result_artifacts(node)
    ]
    assert len(guarded_imports) == 1
    assert any(
        isinstance(handler.type, ast.Name)
        and handler.type.id == 'ModuleNotFoundError'
        for handler in guarded_imports[0].handlers
    )
    assert "startswith('lib.storage')" in source


def test_tag_release_publishes_all_clone_free_artifacts():
    workflow = (ROOT / '.github/workflows/publish-developer-runtime.yml') \
        .read_text(encoding='utf-8')
    for contract in (
        'pypa/gh-action-pypi-publish',
        'npm publish dist/npm/*.tgz',
        'working-directory: clients/typescript',
        'npm ci',
        'npm test',
        'target: agent',
        'ghcr.io/rangehow/tofu-agent',
        'scripts/check_developer_runtime_artifacts.py',
        'softprops/action-gh-release',
    ):
        assert contract in workflow


def test_tag_release_is_blocked_on_source_and_agent_supply_chain_gates():
    workflow = (ROOT / '.github/workflows/publish-developer-runtime.yml') \
        .read_text(encoding='utf-8')
    pinned_trivy = (
        'aquasecurity/trivy-action@'
        'ed142fd0673e97e23eac54620cfb913e5ce36c25')

    assert 'release-supply-chain:' in workflow
    assert 'gate-agent-image:' in workflow
    assert workflow.count(pinned_trivy) == 7
    assert 'tofu-repository.cdx.json' in workflow
    assert 'tofu-agent-linux-amd64.cdx.json' in workflow
    assert 'tofu-agent-linux-arm64.cdx.json' in workflow
    assert 'tofu-agent-digests.json' in workflow
    assert 'tofu-agent-promotion.json' in workflow
    assert 'staging-${GITHUB_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}' in workflow
    assert 'scanners: vuln,misconfig' in workflow
    assert 'scanners: secret' in workflow
    assert 'target: agent' in workflow
    assert 'id: candidate' in workflow
    assert 'index_digest: ${{ steps.candidate.outputs.digest }}' in workflow
    assert "('linux', 'amd64')" in workflow
    assert "('linux', 'arm64')" in workflow
    assert 'docker buildx imagetools create' in workflow
    assert 'Promote the gated OCI index without rebuilding' in workflow
    assert 'needs: [build-artifacts, gate-agent-image]' in workflow
    assert 'needs: gate-agent-image' in workflow
    assert 'name: developer-runtime-sbom-${{ github.ref_name }}' in workflow
    assert 'name: developer-runtime-agent-evidence-${{ github.ref_name }}' in workflow
    assert 'name: developer-runtime-agent-promotion-${{ github.ref_name }}' in workflow
    assert 'path: dist/sbom' in workflow
    assert 'pip install --disable-pip-version-check build twine' not in workflow
    assert 'uv sync --frozen --extra release --no-install-project' in workflow
    assert 'python -I -m build --no-isolation' in workflow
    assert 'dist/pypi/tofu_agent-*.tar.gz' in workflow


def test_tag_release_yaml_promotes_only_the_previously_gated_digest():
    workflow = (ROOT / '.github/workflows/publish-developer-runtime.yml') \
        .read_text(encoding='utf-8')
    parsed = yaml.load(workflow, Loader=yaml.BaseLoader)
    jobs = parsed['jobs']
    gate = jobs['gate-agent-image']
    publisher = jobs['publish-agent-image']

    assert gate['needs'] == ['build-artifacts', 'release-supply-chain']
    gate_actions = [step.get('uses', '') for step in gate['steps']]
    publish_actions = [step.get('uses', '') for step in publisher['steps']]
    assert any(value.startswith('docker/build-push-action@')
               for value in gate_actions)
    assert not any(value.startswith('docker/build-push-action@')
                   for value in publish_actions)
    assert publisher['needs'] == 'gate-agent-image'

    candidate = next(
        step for step in gate['steps'] if step.get('id') == 'candidate')
    assert candidate['with']['push'] == 'true'
    assert candidate['with']['platforms'] == 'linux/amd64,linux/arm64'
    assert candidate['with']['tags'] == '${{ steps.staging.outputs.ref }}'

    promotion = next(
        step for step in publisher['steps']
        if step.get('name') == 'Promote the gated OCI index without rebuilding'
    )
    assert promotion['env']['INDEX_DIGEST'] == (
        '${{ needs.gate-agent-image.outputs.index_digest }}')
    assert 'immutable_source="${AGENT_IMAGE}@${INDEX_DIGEST}"' in (
        promotion['run'])
    assert 'imagetools create "${promotion_args[@]}" "$immutable_source"' \
        in promotion['run']
    assert 'test "$promoted_digest" = "$INDEX_DIGEST"' in promotion['run']

    for line in workflow.splitlines():
        match = re.search(r'\buses:\s+[^@\s]+@([^\s#]+)', line)
        if match:
            assert re.fullmatch(r'[0-9a-f]{40}', match.group(1)), line


def test_tag_release_embedded_shell_is_valid_bash():
    workflow = (ROOT / '.github/workflows/publish-developer-runtime.yml') \
        .read_text(encoding='utf-8')
    jobs = yaml.load(workflow, Loader=yaml.BaseLoader)['jobs']
    checked_steps = 0

    for job_name, job in jobs.items():
        for step in job['steps']:
            script = step.get('run')
            if script is None:
                continue
            # GitHub resolves expressions before invoking bash. Substitute a
            # harmless token so bash only validates the resulting shell shape.
            normalized = re.sub(r'\$\{\{.*?\}\}', 'ci-expression', script)
            result = subprocess.run(
                ['bash', '-n'],
                input=normalized,
                text=True,
                capture_output=True,
                check=False,
            )
            assert result.returncode == 0, (
                job_name, step.get('name'), result.stderr)
            checked_steps += 1

    assert checked_steps >= 10
