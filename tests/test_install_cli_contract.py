"""Behavioral contracts for install.sh's human-facing option boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / 'install.sh'


def _run_installer(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment['HOME'] = str(tmp_path / 'home')
    return subprocess.run(
        ['bash', str(INSTALLER), *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _uv_reset_helpers() -> str:
    source = INSTALLER.read_text(encoding='utf-8')
    start = source.index('_uv_env_matches_install_marker() {')
    end = source.index('\n\n# The uv fast path.', start)
    return source[start:end]


def _python_match_helper() -> str:
    source = INSTALLER.read_text(encoding='utf-8')
    start = source.index('_python_matches_request() {')
    end = source.index('\n\n_uv_env_matches_install_marker() {', start)
    return source[start:end]


def _run_uv_reset(project: Path) -> subprocess.CompletedProcess:
    script = f"""
set -euo pipefail
warn() {{ printf 'WARN %s\\n' "$*" >&2; }}
ok() {{ printf 'OK %s\\n' "$*"; }}
{_uv_reset_helpers()}
RESET_ENV=1
INSTALL_DIR="$1"
_UV_RESET_REFUSED=0
_reset_uv_env_if_requested "${{INSTALL_DIR}}/.venv"
"""
    return subprocess.run(
        ['bash', '-c', script, 'reset-harness', str(project)],
        capture_output=True,
        text=True,
        timeout=10,
    )


def _conda_marker_helper() -> str:
    source = INSTALLER.read_text(encoding='utf-8')
    start = source.index('_conda_env_matches_install_marker() {')
    end = source.index('\n\nENV_EXISTS=0', start)
    return source[start:end]


def _run_conda_marker_check(
        project: Path, conda_base: Path, env_name: str) -> subprocess.CompletedProcess:
    script = f"""
set -euo pipefail
{_conda_marker_helper()}
INSTALL_DIR="$1"
CONDA_BASE="$2"
ENV_NAME="$3"
CONDA_ENV_PREFIX="${{CONDA_BASE}}/envs/${{ENV_NAME}}"
_conda_env_matches_install_marker
"""
    return subprocess.run(
        ['bash', '-c', script, 'marker-harness', str(project),
         str(conda_base), env_name],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_help_is_side_effect_free(tmp_path):
    result = _run_installer(tmp_path, '--help')

    assert result.returncode == 0
    assert 'Usage: install.sh [OPTIONS]' in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('arguments, message', [
    (('--pg-major', '17'), 'install.sh supports personal SQLite only'),
    (('--reinit-pgdata',), 'install.sh supports personal SQLite only'),
])
def test_removed_postgres_options_fail_before_installation(
        tmp_path, arguments, message):
    result = _run_installer(tmp_path, *arguments)

    assert result.returncode == 2
    assert message in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('name', ['-unsafe', 'has/slash', 'has space', 'x' * 129])
def test_invalid_conda_environment_name_fails_before_installation(tmp_path, name):
    result = _run_installer(tmp_path, '--env', name)

    assert result.returncode == 2
    assert '--env must use 1-128' in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('version', ['latest', '3', '3.9', '4.0', '3.12.*'])
def test_invalid_or_unsupported_python_fails_before_installation(tmp_path, version):
    result = _run_installer(tmp_path, '--python', version)

    assert result.returncode == 2
    assert '--python must' in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_conda_specific_options_cannot_fall_through_to_uv():
    source = INSTALLER.read_text(encoding='utf-8')

    for option in (
        '--env)', '--env=*)', '--no-update-conda)', '--min-conda)',
        '--min-conda=*)', '--force-sibling-conda)', '--with-docling)',
    ):
        line = next(line for line in source.splitlines() if option in line)
        assert 'USE_CONDA=1' in line, f'{option} can be silently ignored by uv'


def test_force_sibling_conda_outranks_existing_environment_marker():
    source = INSTALLER.read_text(encoding='utf-8')
    marker_probe = source.index(
        'if [[ "$FORCE_SIBLING_CONDA" -ne 1 && -f "$_TOFU_ENV_MARKER" ]]')
    candidate_selection = source.index(
        'if [[ "$FORCE_SIBLING_CONDA" -eq 1 ]]', marker_probe)
    sibling_reuse = source.index(
        'if [[ -z "$CONDA_BIN" && -x "${SIBLING_CONDA_DIR}/bin/conda" ]]',
        candidate_selection)

    assert marker_probe < candidate_selection < sibling_reuse
    assert 'ignoring marker and user-owned conda installations' in source[
        candidate_selection:sibling_reuse]


def test_explicit_install_directory_is_never_replaced_by_cwd():
    source = INSTALLER.read_text(encoding='utf-8')

    assert 'DIR_EXPLICIT=0' in source
    assert source.count('DIR_EXPLICIT=1') == 2
    assert 'elif [[ "$DIR_EXPLICIT" -eq 0 && -f "server.py" ]]' in source
    assert 'Install target is non-empty but is not a Tofu checkout' in source


def test_reset_removes_only_an_installer_owned_checkout_venv(tmp_path):
    project = tmp_path / 'tofu'
    venv = project / '.venv'
    venv.mkdir(parents=True)
    (venv / '.tofu-install-owned').write_text(
        'tofu-install-owned-v1\n', encoding='utf-8')
    (venv / 'keep-until-reset').write_text('x', encoding='utf-8')

    result = _run_uv_reset(project)

    assert result.returncode == 0, result.stderr
    assert not venv.exists()


def test_reset_refuses_unowned_or_symlinked_venv(tmp_path):
    unowned_project = tmp_path / 'unowned'
    unowned = unowned_project / '.venv'
    unowned.mkdir(parents=True)

    unowned_result = _run_uv_reset(unowned_project)

    assert unowned_result.returncode == 1
    assert unowned.is_dir()
    assert 'Refusing --reset-env' in unowned_result.stderr

    target = tmp_path / 'someone-elses-venv'
    target.mkdir()
    (target / '.tofu-install-owned').write_text(
        'tofu-install-owned-v1\n', encoding='utf-8')
    linked_project = tmp_path / 'linked'
    linked_project.mkdir()
    (linked_project / '.venv').symlink_to(target, target_is_directory=True)

    linked_result = _run_uv_reset(linked_project)

    assert linked_result.returncode == 1
    assert target.is_dir()
    assert (linked_project / '.venv').is_symlink()


def test_matching_uv_marker_is_accepted_as_legacy_ownership_proof(tmp_path):
    project = tmp_path / 'tofu'
    python_dir = project / '.venv' / 'bin'
    python_dir.mkdir(parents=True)
    (python_dir / 'python').symlink_to(sys.executable)
    (project / '.tofu_env.json').write_text(json.dumps({
        'backend': 'uv',
        'env_prefix': str(project / '.venv'),
        'owned_by_tofu_install': True,
    }), encoding='utf-8')

    result = _run_uv_reset(project)

    assert result.returncode == 0, result.stderr
    assert not (project / '.venv').exists()


def test_explicit_python_version_is_checked_before_reusing_environments():
    source = INSTALLER.read_text(encoding='utf-8')
    current = f'{sys.version_info.major}.{sys.version_info.minor}'
    mismatch = f'{sys.version_info.major}.{sys.version_info.minor + 1}'
    script = f"""
set -euo pipefail
{_python_match_helper()}
_python_matches_request "$1" "$2"
"""

    matching = subprocess.run(
        ['bash', '-c', script, 'python-match', sys.executable, current],
        capture_output=True, text=True, timeout=10)
    mismatching = subprocess.run(
        ['bash', '-c', script, 'python-match', sys.executable, mismatch],
        capture_output=True, text=True, timeout=10)

    assert matching.returncode == 0, matching.stderr
    assert mismatching.returncode == 1
    assert '_UV_CONFIG_CONFLICT=1' in source
    assert "Existing conda env '${ENV_NAME}' does not satisfy --python" in source


def test_reset_preserves_existing_conda_backend_and_guards_deletion():
    source = INSTALLER.read_text(encoding='utf-8')
    reset_selection = source[
        source.index('# A destructive reset applies'):source.index(
            '# ═══════════════════════════════════════════════════════════════\n'
            '#  Step 0.6', source.index('# A destructive reset applies'))]

    assert '"conda_base"' in reset_selection
    assert 'USE_CONDA=1' in reset_selection
    assert 'ENV_EXPLICIT' in reset_selection
    gate = source.index('_conda_env_matches_install_marker || fail')
    deletion = source.index('conda env remove -n "$ENV_NAME" -y', gate)
    assert gate < deletion
    assert "'backend':     'conda'" in source


def test_conda_reset_marker_must_match_exact_environment(tmp_path):
    project = tmp_path / 'tofu'
    project.mkdir()
    conda_base = tmp_path / 'miniforge'
    (conda_base / 'bin').mkdir(parents=True)
    (conda_base / 'bin' / 'python').symlink_to(sys.executable)
    env_name = 'tofu-safe'
    env_prefix = conda_base / 'envs' / env_name
    (project / '.tofu_env.json').write_text(json.dumps({
        'backend': 'conda',
        'conda_base': str(conda_base),
        'env_name': env_name,
        'env_prefix': str(env_prefix),
    }), encoding='utf-8')

    matching = _run_conda_marker_check(project, conda_base, env_name)
    mismatching = _run_conda_marker_check(project, conda_base, 'someone-else')

    assert matching.returncode == 0, matching.stderr
    assert mismatching.returncode == 1
