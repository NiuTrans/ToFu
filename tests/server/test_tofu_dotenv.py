"""Contract tests for the one dotenv parser used by every runtime surface."""

from __future__ import annotations

import ast
import os

import pytest

from tofu_dotenv import (
    MAX_DOTENV_BYTES,
    load_dotenv_file,
    parse_dotenv,
    parse_env_boolean,
    read_dotenv_values,
    update_dotenv_file,
)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('1', True), ('TRUE', True), ('enabled', True),
        ('0', False), ('No', False), ('disabled', False),
        ('', None), ('sometimes', None), (None, None),
    ],
)
def test_environment_boole_have_one_explicit_vocabulary(value, expected):
    assert parse_env_boolean(value) is expected


def test_parser_supports_documented_common_forms_without_hidden_coercion():
    values = parse_dotenv(
        '\ufeffPORT="15599"\n'
        "BIND_HOST='127.0.0.1'\n"
        'LLM_MODEL=model#revision\n'
        'TOFU_TLS=0\n'
        'EMPTY=\n'
        'export IGNORED=value\n'
        'NOT VALID=value\n'
        '=missing-name\n')

    assert values == {
        'PORT': '15599',
        'BIND_HOST': '127.0.0.1',
        'LLM_MODEL': 'model#revision',
        'TOFU_TLS': '0',
        'EMPTY': '',
    }


def test_surrounding_quotes_are_removed_and_hashes_remain_data():
    values = parse_dotenv(
        'QUOTED="value # stays"\n'
        "SINGLE='also # stays'\n"
        'UNQUOTED=value # remains data\n')
    assert values['QUOTED'] == 'value # stays'
    assert values['SINGLE'] == 'also # stays'
    assert values['UNQUOTED'] == 'value # remains data'


def test_later_assignment_wins_and_explicit_environment_is_never_overwritten(
        tmp_path):
    path = tmp_path / '.env'
    path.write_text('PORT=15000\nPORT=16000\nMODEL=file-model\n', encoding='utf-8')
    environ = {'PORT': '17000'}

    assert read_dotenv_values(path) == {'PORT': '16000', 'MODEL': 'file-model'}
    loaded = load_dotenv_file(path, environ)
    assert loaded['PORT'] == '16000'
    assert environ == {'PORT': '17000', 'MODEL': 'file-model'}


def test_missing_file_is_optional_but_other_io_errors_surface(tmp_path):
    assert read_dotenv_values(tmp_path / 'missing.env') == {}
    with pytest.raises(IsADirectoryError):
        read_dotenv_values(tmp_path)


def test_invalid_or_oversized_dotenv_fails_loudly_without_parsing(tmp_path):
    invalid = tmp_path / 'invalid.env'
    invalid.write_bytes(b'PORT=15000\xff\n')
    with pytest.raises(OSError, match='must be valid UTF-8'):
        read_dotenv_values(invalid)

    oversized = tmp_path / 'oversized.env'
    oversized.write_bytes(b'X' * (MAX_DOTENV_BYTES + 1))
    with pytest.raises(OSError, match='exceeds'):
        read_dotenv_values(oversized)


def test_all_runtime_consumers_delegate_to_the_canonical_parser():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    expected = {
        # server.py's dotenv load rides the lib/server/server_boot_env.py facade;
        # the canonical-parser delegation is pinned at that owner.
        'lib/server/server_boot_env.py': 'load_dotenv_file',
        'bootstrap_pkg/runtime.py': 'load_dotenv_file',
        'healthcheck.py': 'read_dotenv_values',
        'serverctl.py': 'read_dotenv_values',
        'serverctl_pkg/support_bundle.py': 'parse_dotenv',
    }
    for relative, symbol in expected.items():
        with open(os.path.join(root, relative), encoding='utf-8') as stream:
            source = stream.read()
        imports = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module == 'tofu_dotenv'
            for alias in node.names
        }
        assert symbol in imports


def test_executable_startup_surfaces_dotenv_failures_as_actionable_errors():
    """Both direct launchers must stop before treating bad config as repair."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for relative_path, prefix in (
        ('bootstrap.py', '[bootstrap] Invalid project .env:'),
        ('server.py', '[server.py] Invalid project .env:'),
    ):
        with open(os.path.join(root, relative_path), encoding='utf-8') as stream:
            source = stream.read()
        assert 'except OSError as _dotenv_error:' in source
        assert prefix in source
        assert 'python serverctl.py doctor' in source
        assert 'raise SystemExit(2) from None' in source


def test_lifecycle_manager_reader_stays_compatible_with_canonical_syntax(
        tmp_path):
    # server_manager.py is intentionally dependency-light and owns an explicit
    # allowlist. Keep its reader behavior pinned to the canonical parser even
    # though it filters the result for the process-launch boundary.
    from server_manager import SERVER_ENV_KEYS, project_server_env

    path = tmp_path / '.env'
    path.write_text(
        'PORT="15599"\n'
        "BIND_HOST='127.0.0.1'\n"
        'TOFU_TLS=0\n'
        'TOFU_AGENT_QUEUE_CAPACITY=96\n'
        'TOFU_AGENT_STUCK_REPLACEMENTS=2\n'
        'IGNORED=value\n',
        encoding='utf-8')
    canonical = {
        key: value for key, value in read_dotenv_values(path).items()
        if key in SERVER_ENV_KEYS
    }
    assert project_server_env(str(tmp_path)) == canonical
    assert canonical['TOFU_AGENT_QUEUE_CAPACITY'] == '96'
    assert canonical['TOFU_AGENT_STUCK_REPLACEMENTS'] == '2'


def test_atomic_writer_replaces_all_equivalent_keys_and_preserves_unrelated_text(tmp_path):
    path = tmp_path / '.env'
    path.write_text('# comment\nKEEP="a=b #data"\n\ufeffPORT =1\nPORT=2\nOLD =gone')
    update_dotenv_file(path, {'PORT': '3', 'LLM_API_KEYS': 'fake&key\\hash#data'}, remove=('OLD',))
    assert read_dotenv_values(path) == {'KEEP': 'a=b #data', 'PORT': '3', 'LLM_API_KEYS': 'fake&key\\hash#data'}
    assert path.read_text().startswith('# comment\nKEEP="a=b #data"\n')
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('value', ['bad\nPORT=9', 'bad\r', 'bad\x00', 'bad\u2028', '"quoted"', ' leading'])
def test_writer_rejects_injection_and_nonroundtrip_before_writing(tmp_path, value):
    path = tmp_path / '.env'
    path.write_text('KEEP=old\n')
    with pytest.raises(ValueError):
        update_dotenv_file(path, {'LLM_API_KEYS': value})
    assert path.read_text() == 'KEEP=old\n'


def test_writer_refuses_symlink_and_rolls_back_failed_replacement(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    path.write_text('KEEP=old\n')
    link = tmp_path / 'linked'
    link.symlink_to(path)
    with pytest.raises(ValueError, match='symlink'):
        update_dotenv_file(link, {'KEEP': 'new'})
    def reject(*args):
        raise OSError('injected')
    monkeypatch.setattr(os, 'replace', reject)
    with pytest.raises(OSError, match='injected'):
        update_dotenv_file(path, {'KEEP': 'new'})
    assert path.read_text() == 'KEEP=old\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['.env', 'linked']
