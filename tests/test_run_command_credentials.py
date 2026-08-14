from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SECRET = 'ghp_SelectedOnly0123456789abcdef'


@pytest.fixture()
def isolated_vault(tmp_path, monkeypatch):
    import lib.credentials_vault as vault

    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / '.vault.key')
    monkeypatch.setattr(vault, '_fernet', None)
    return vault


def _presence_command(var='GITHUB_TOKEN'):
    return (
        "python -c \"import os; print('set' if os.getenv('"
        + var
        + "') else 'unset')\""
    )


def test_run_command_general_credentials_are_default_deny(
        isolated_vault, monkeypatch, tmp_path):
    isolated_vault.set_entry('github_token', _SECRET)
    monkeypatch.setenv('GITHUB_TOKEN', 'ambient-parent-value')

    from lib.project_mod.run_command import tool_run_command

    out = tool_run_command(str(tmp_path), _presence_command())
    assert '\nunset\n' in out
    assert _SECRET not in out
    assert 'ambient-parent-value' not in out


def test_run_command_strips_unregistered_ambient_credentials(
        isolated_vault, monkeypatch, tmp_path):
    monkeypatch.setenv('UNREGISTERED_API_KEY', 'ambient-api-secret')
    monkeypatch.setenv('PROXY_HK_GW_AUTH', 'ambient-proxy-secret')
    monkeypatch.setenv('DATABASE_URL', 'postgres://ambient-db-secret')
    monkeypatch.setenv('TOFU_AUTH_MODE', 'private')

    from lib.project_mod.run_command import tool_run_command

    command = (
        _presence_command('UNREGISTERED_API_KEY') + '; '
        + _presence_command('PROXY_HK_GW_AUTH') + '; '
        + _presence_command('DATABASE_URL') + '; '
        + _presence_command('TOFU_AUTH_MODE')
    )
    out = tool_run_command(str(tmp_path), command)
    states = [line for line in out.splitlines()
              if line in ('set', 'unset')]
    assert states == ['unset', 'unset', 'unset', 'set']
    assert 'ambient-api-secret' not in out
    assert 'ambient-proxy-secret' not in out
    assert 'ambient-db-secret' not in out


def test_run_command_injects_only_explicitly_selected_entry(
        isolated_vault, monkeypatch, tmp_path):
    isolated_vault.set_entry('github_token', _SECRET)
    isolated_vault.set_entry('pypi_token', 'pypi-other-secret')
    monkeypatch.setenv('GITHUB_TOKEN', 'ambient-parent-value')
    monkeypatch.setenv('PYPI_TOKEN', 'ambient-pypi-value')

    from lib.project_mod.run_command import tool_run_command

    out = tool_run_command(
        str(tmp_path),
        _presence_command('GITHUB_TOKEN') + '; ' + _presence_command('PYPI_TOKEN'),
        credentials=['github_token'],
    )
    assert out.count('\nset\n') == 1
    assert out.count('\nunset\n') == 1
    assert _SECRET not in out
    assert 'pypi-other-secret' not in out
    assert 'ambient-parent-value' not in out
    assert 'ambient-pypi-value' not in out


@pytest.mark.parametrize('requested, fragment', [
    ('', 'must be an array'),
    ('github_token', 'must be an array'),
    ([123], 'must be a vault entry name string'),
    (['missing_token'], 'does not exist'),
    (['skill.flyai.api_key'], 'skill-scoped'),
])
def test_invalid_credential_request_is_rejected_before_spawn(
        isolated_vault, monkeypatch, tmp_path, requested, fragment):
    isolated_vault.set_entry('github_token', _SECRET)
    isolated_vault.set_entry('skill.flyai.api_key', 'skill-secret')

    def tripwire(*_args, **_kwargs):
        raise AssertionError('subprocess must not spawn for rejected credentials')

    monkeypatch.setattr('subprocess.Popen', tripwire)
    from lib.project_mod.run_command import tool_run_command

    out = tool_run_command(str(tmp_path), 'true', credentials=requested)
    assert 'Credential request rejected' in out
    assert fragment in out
    assert _SECRET not in out


def test_colliding_selected_entry_names_are_rejected_before_spawn(
        isolated_vault, monkeypatch, tmp_path):
    isolated_vault.set_entry('acme-token', 'one-secret')
    isolated_vault.set_entry('acme_token', 'two-secret')

    def tripwire(*_args, **_kwargs):
        raise AssertionError('subprocess must not spawn for colliding entries')

    monkeypatch.setattr('subprocess.Popen', tripwire)
    from lib.project_mod.run_command import tool_run_command

    out = tool_run_command(
        str(tmp_path), 'true', credentials=['acme-token', 'acme_token'])
    assert 'Credential request rejected' in out
    assert 'both map to $ACME_TOKEN' in out
    assert 'one-secret' not in out and 'two-secret' not in out


def test_never_created_vault_allows_ordinary_command(
        isolated_vault, tmp_path):
    assert not isolated_vault._STORE_PATH.exists()

    from lib.project_mod.run_command import tool_run_command

    out = tool_run_command(str(tmp_path), 'printf ordinary')
    assert '\nordinary\n' in out
    assert '[exit code: 0]' in out
    assert not isolated_vault._STORE_PATH.exists()
    assert not isolated_vault._KEY_PATH.exists()


@pytest.mark.parametrize('payload', ['[]', '{}', '{"entries": []}'])
def test_existing_invalid_vault_shape_is_rejected_before_spawn(
        isolated_vault, monkeypatch, tmp_path, payload):
    isolated_vault._STORE_PATH.write_text(payload, encoding='utf-8')

    def tripwire(*_args, **_kwargs):
        raise AssertionError('subprocess must not spawn for invalid vault shape')

    monkeypatch.setattr('subprocess.Popen', tripwire)
    from lib.project_mod.run_command import tool_run_command

    out = tool_run_command(str(tmp_path), 'true')
    assert 'Credential request rejected' in out
    assert 'vault unavailable' in out


@pytest.mark.parametrize('failure', ['invalid-json', 'read-error'])
def test_vault_snapshot_failure_is_rejected_before_spawn(
        isolated_vault, monkeypatch, tmp_path, failure):
    isolated_vault.set_entry('github_token', _SECRET)
    monkeypatch.setenv('GITHUB_TOKEN', 'ambient-value-must-not-leak')
    if failure == 'invalid-json':
        isolated_vault._STORE_PATH.write_text('{broken json', encoding='utf-8')
    else:
        monkeypatch.setattr(
            isolated_vault, '_read_store',
            lambda **_kwargs: (_ for _ in ()).throw(OSError('read failed')),
        )

    def tripwire(*_args, **_kwargs):
        raise AssertionError('subprocess must not spawn without a vault snapshot')

    monkeypatch.setattr('subprocess.Popen', tripwire)
    from lib.project_mod.run_command import tool_run_command

    out = tool_run_command(str(tmp_path), 'true')
    assert 'Credential request rejected' in out
    assert _SECRET not in out
    assert 'ambient-value-must-not-leak' not in out


def test_unreadable_selected_credential_is_rejected_before_spawn(
        isolated_vault, monkeypatch, tmp_path):
    isolated_vault.set_entry('github_token', _SECRET)
    isolated_vault._KEY_PATH.write_bytes(b'corrupt-key')
    isolated_vault._fernet = None

    def tripwire(*_args, **_kwargs):
        raise AssertionError('subprocess must not spawn with unreadable credential')

    monkeypatch.setattr('subprocess.Popen', tripwire)
    from lib.project_mod.run_command import tool_run_command

    out = tool_run_command(
        str(tmp_path), 'true', credentials=['github_token'])
    assert 'Credential request rejected' in out
    assert 'could not be resolved' in out
    assert _SECRET not in out


def test_interactive_runner_receives_only_selected_credential(
        isolated_vault, tmp_path):
    isolated_vault.set_entry('github_token', _SECRET)

    from lib.project_mod.run_command import tool_run_command

    out = tool_run_command(
        str(tmp_path), _presence_command(),
        credentials=['github_token'], stdin_callback=lambda _hint: None,
    )
    assert '\nset\n' in out
    assert _SECRET not in out


def test_standalone_dispatch_passes_credential_capability(
        isolated_vault, tmp_path):
    isolated_vault.set_entry('github_token', _SECRET)

    from lib.project_mod import execute_standalone_command

    out = execute_standalone_command(
        'run_command',
        {'command': _presence_command(), 'credentials': ['github_token']},
        working_dir=str(tmp_path),
    )
    assert '\nset\n' in out
    assert _SECRET not in out


def test_run_command_schema_exposes_bounded_credential_names():
    from lib.tools.code_exec import CODE_EXEC_TOOL
    from lib.tools.project import PROJECT_TOOL_RUN_COMMAND

    for tool in (PROJECT_TOOL_RUN_COMMAND, CODE_EXEC_TOOL):
        prop = tool['function']['parameters']['properties']['credentials']
        assert prop['type'] == 'array'
        assert prop['items'] == {'type': 'string'}
        assert prop['maxItems'] == 16
        assert 'THIS child process only' in prop['description']


def test_vault_prompt_requires_explicit_run_command_selection(isolated_vault):
    isolated_vault.set_entry('github_token', _SECRET)
    block = isolated_vault.build_vault_index()
    assert "run_command's `credentials` array" in block
    assert 'only those selected values are injected' in block
    assert _SECRET not in block


def test_remote_run_command_schema_documents_vault_unavailability():
    from lib.tools.project import PROJECT_TOOL_RUN_COMMAND, with_remote_hint

    remote_tool = with_remote_hint([PROJECT_TOOL_RUN_COMMAND])[0]
    description = remote_tool['function']['description']
    assert 'Server vault credentials are unavailable' in description
    assert 'do not pass the `credentials` field' in description


@pytest.mark.parametrize('credential_field', [
    ['github_token'], [], '', {}, 0, None,
])
def test_remote_worktree_refuses_any_server_vault_capability_field(
        monkeypatch, credential_field):
    import lib.desktop
    import lib.tasks_pkg.handlers.project as handler

    monkeypatch.setattr(
        lib.desktop,
        'send_desktop_command',
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError('server credential request must not reach desktop')),
    )
    finalized = []
    monkeypatch.setattr(
        handler, '_finalize_tool_round',
        lambda task, rn, round_entry, metas: finalized.extend(metas),
    )

    result = handler._execute_remote_run_command(
        {'id': 'task1'}, 'tc1',
        {'command': 'gh auth status', 'credentials': credential_field},
        1, {}, {'agent_id': 'agent123456', 'root': '/workspace'},
    )

    assert 'server vault credentials are unavailable' in result[1]
    assert finalized[0]['notRun'] is True
    assert finalized[0]['exitCode'] == 'not-run'
