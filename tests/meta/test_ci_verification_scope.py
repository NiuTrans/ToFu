"""CI scopes retain full qualification while ordinary candidates use a real base."""
import importlib.util
import json
from pathlib import Path

import pytest

from tests.project_mod.test_landing_verification import (
    git, write, commit, shared as shared, private_local_root as private_local_root,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def scope_module():
    spec = importlib.util.spec_from_file_location(
        'ci_verification_scope', ROOT / '.github/scripts/verification_scope.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(('event_name', 'event'), [
    ('schedule', {}), ('workflow_dispatch', {}),
    ('push', {'ref': 'refs/tags/v1.2.3'}),
])
def test_qualification_events_require_full_scope(shared, event_name, event):
    result = scope_module().choose_scope(shared, event_name, event)
    assert result['profile'] == 'full'
    assert result['head'] == git(shared, 'rev-parse', 'HEAD').strip()


@pytest.mark.parametrize('event_name', ['push', 'pull_request'])
def test_ordinary_events_verify_from_the_comparison_commit(shared, event_name):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return 3 * x\n')
    commit(shared, 'candidate')
    event = {'before': base} if event_name == 'push' else {'pull_request': {'base': {'sha': base}}}
    result = scope_module().choose_scope(shared, event_name, event)
    assert result['profile'] == 'incremental'
    assert result['base'] == base
    assert result['head'] == git(shared, 'rev-parse', 'HEAD').strip()


@pytest.mark.parametrize('base', ['', '0' * 40, 'f' * 40, '--not-a-commit'])
def test_absent_comparison_history_expands_to_full(shared, base):
    result = scope_module().choose_scope(shared, 'push', {'before': base})
    assert result['profile'] == 'full' and result['base'] == ''
    assert result['reason']


def test_invalid_event_evidence_is_rejected(shared):
    with pytest.raises(ValueError):
        scope_module().choose_scope(shared, 'push', [])
    with pytest.raises(ValueError):
        scope_module().choose_scope(shared, 'unknown-event', {})


def test_cli_publishes_scope_and_candidate_identity(shared, tmp_path, monkeypatch, capsys):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    event, output = tmp_path / 'event.json', tmp_path / 'output'
    event.write_text(json.dumps({'before': base}))
    monkeypatch.chdir(shared)
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'push')
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event))
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    assert scope_module().main() == 0
    assert json.loads(capsys.readouterr().out)['head'] == base
    assert output.read_text().splitlines() == ['profile=incremental', f'head={base}', f'base={base}']
    event.write_text('{broken')
    assert scope_module().main() == 1
    assert 'unavailable' in capsys.readouterr().err
