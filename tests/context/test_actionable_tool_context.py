"""Automatic capability directories follow the admitted tool surface.

Real composition and rendering are retained; only directory acquisition and
unrelated source I/O are replaced with synthetic, owner-independent fixtures.
"""

from dataclasses import replace

import pytest

from lib.tasks_pkg.context_composer import ComposeRequest, render_context
from lib.tasks_pkg.context_composer import _providers as providers


pytestmark = pytest.mark.unit


def request(*, wire=(), deferred=(), real=True, task=None):
    value = {'config': {}, '_executable_tool_catalog': [
        {'type': 'function', 'function': {'name': name}}
        for name in deferred]}
    value.update(task or {})
    return ComposeRequest(user_id=7, has_real_tools=real,
                          tool_names=frozenset(wire), task=value,
                          system_prompt_mode='replace')


@pytest.fixture
def directories(monkeypatch):
    import lib.skills
    import lib.skills.activation
    import lib.skills.paths
    import lib.credentials_vault

    acquired = []

    def skills(*args, **kwargs):
        acquired.append(('skills', kwargs['owner_user_id']))
        return '<skills_index>example skill</skills_index>'

    def vault():
        acquired.append(('vault', None))
        return '<credential_vault>synthetic entry name</credential_vault>'

    monkeypatch.setattr(lib.skills, 'build_skills_index', skills)
    monkeypatch.setattr(lib.credentials_vault, 'build_vault_index', vault)
    monkeypatch.setattr(lib.skills.paths, 'extra_skill_project_paths', lambda *_: [])
    monkeypatch.setattr(lib.skills.activation, 'explicit_skill_context',
                        lambda *a, **k: {'content': '', 'receipts': []})
    monkeypatch.setattr(providers, '_profile_block', lambda _: '')
    return acquired


def blocks(value):
    return {row.id: row for row in providers.collect_context_blocks(
        [{'role': 'system', 'content': 'operator instruction'},
         {'role': 'user', 'content': 'reduce dataset values'}], value)}


@pytest.mark.parametrize('wire', [(), ('custom__read_dataset',),
                                ('custom__read_dataset', 'execute_program')])
def test_unavailable_directories_are_not_acquired_or_injected(directories, wire):
    result = blocks(request(wire=wire))
    assert directories == []
    for name, reason in [('skills_index', 'load_skill_not_enabled'),
                         ('credential_vault', 'run_command_not_enabled')]:
        assert result[name].content == ''
        assert result[name].suppressed_reason == reason


@pytest.mark.parametrize('surface', ['wire', 'deferred'])
@pytest.mark.parametrize(('tool', 'block', 'source'), [
    ('load_skill', 'skills_index', 'skills'),
    ('run_command', 'credential_vault', 'vault'),
])
def test_actionable_directory_survives_direct_or_deferred_exposure(
        directories, surface, tool, block, source):
    value = request(wire=[tool] if surface == 'wire' else ['execute_program'],
                    deferred=[tool] if surface == 'deferred' else [])
    result = blocks(value)
    assert result[block].content
    assert result[block].suppressed_reason == ''
    assert directories == [(source, 7 if source == 'skills' else None)]


def test_no_tool_authority_wins_over_catalog_names(directories):
    result = blocks(request(wire=['load_skill', 'run_command'],
                            deferred=['load_skill', 'run_command'], real=False))
    assert directories == []
    assert result['skills_index'].suppressed_reason == 'skills_disabled'
    assert result['credential_vault'].suppressed_reason == 'vault_disabled'


@pytest.mark.parametrize(('tool', 'block'), [
    ('load_skill', 'skills_index'), ('run_command', 'credential_vault')])
@pytest.mark.parametrize('known_catalog', [True, False])
def test_declared_capability_removal_retracts_but_unknown_catalog_retains(
        directories, tool, block, known_catalog):
    value = request(wire=[tool])
    messages = [{'role': 'user', 'content': 'continue'}]
    first = blocks(value)[block]
    render_context(messages, [first], value)
    removed = request(task={'_executable_tool_catalog': [] if known_catalog else None})
    missing = blocks(removed)[block]
    result = render_context(messages, [missing], removed)
    reason = result.manifest[0]['reason']
    if known_catalog:
        assert reason.endswith('retracted:' + tool + '_not_enabled')
        assert 'no longer active' in messages[-1]['content'][0]['text']
    else:
        assert reason.endswith('retained_after:' + tool + '_unavailable')
        assert 'no longer active' not in messages[-1]['content'][0]['text']


@pytest.mark.parametrize(('tool', 'block'), [
    ('load_skill', 'skills_index'), ('run_command', 'credential_vault')])
def test_directory_acquisition_failure_retains_prior_snapshot(
        directories, monkeypatch, tool, block):
    import lib.skills
    import lib.credentials_vault

    messages = [{'role': 'user', 'content': 'continue'}]
    value = request(wire=[tool])
    first = blocks(value)[block]
    render_context(messages, [first], value)

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic acquisition failure')

    monkeypatch.setattr(lib.skills if tool == 'load_skill' else lib.credentials_vault,
                        'build_skills_index' if tool == 'load_skill' else 'build_vault_index',
                        fail)
    retry = request(wire=[tool])
    failed = blocks(retry)[block]
    assert failed.content == ''
    assert failed.suppressed_reason == 'provider_unavailable'
    result = render_context(messages, [failed], retry)
    assert result.manifest[0]['reason'].endswith('retained_after:provider_unavailable')
    assert 'no longer active' not in messages[-1]['content'][0]['text']


def test_cached_skill_directory_is_hidden_after_capability_removal(directories):
    value = request(wire=['load_skill'])
    assert providers._skill_index(value)
    assert directories == [('skills', 7)]
    assert providers._skill_index(replace(value, tool_names=frozenset())) == ''
    assert providers._skill_index(value)
    assert directories == [('skills', 7)]


def test_explicit_selected_skill_body_is_preserved_without_load_tool(
        directories, monkeypatch):
    import lib.skills.activation

    monkeypatch.setattr(lib.skills.activation, 'explicit_skill_context',
                        lambda *a, **k: {'content': 'selected workflow body',
                                         'receipts': [{'id': 'explicit-selection'}]})
    result = blocks(request(wire=['custom__read_dataset', 'execute_program']))
    assert result['skills_index'].content == ''
    assert result['selected_skills'].content == 'selected workflow body'
    assert result['selected_skills'].provenance['skills'] == [{'id': 'explicit-selection'}]
    assert directories == []
