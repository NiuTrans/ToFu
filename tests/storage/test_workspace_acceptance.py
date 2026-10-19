"""Actual gate execution binds durable evidence to source and checker revisions."""
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

import pytest

from lib import integration_control as control
from lib import integration_state_repository as state
from lib.project_mod.delivery_acceptance import project_acceptance
from tests.storage.test_integration_control import _worktree, _row, _run
from tests.storage import test_integration_control as fixtures

pytestmark = pytest.mark.unit


@pytest.fixture(params=['sqlite', 'tofudb'])
def repository(tmp_path, monkeypatch, request):
    if request.param == 'tofudb':
        from pathlib import Path
        from scripts.check_tofudb_engine_drift import resolve_deployment_binary
        monkeypatch.setenv('TOFUDB_BIN', resolve_deployment_binary(Path(__file__).resolve().parents[2]))
        monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    supervisor = fixtures.StorageSupervisor
    monkeypatch.setattr(fixtures, 'StorageSupervisor',
        lambda **kwargs: supervisor(**{**kwargs, 'backend': request.param}))
    yield from fixtures.repository.__wrapped__(tmp_path, monkeypatch)


def submit(repository, tmp_path, monkeypatch, command, *, task='accepted', content='VALUE = 2\n'):
    writer = _worktree(repository, tmp_path / task)
    (writer / 'logic.py').write_text(content)
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', command)
    control.register_workspace(str(repository), task, str(writer), user_id=1)
    control.submit_workspace(str(repository), task, user_id=1)
    assert control.process_ready_once()
    return writer, _row(repository, task)


def test_verified_candidate_has_real_versioned_checks_and_survives_reload(repository, tmp_path, monkeypatch):
    command = f'{sys.executable} -m py_compile logic.py'
    writer, payload = submit(repository, tmp_path, monkeypatch, command)
    assert payload['state'] == 'merged'
    acceptance = payload['acceptance']
    assert acceptance['status'] == 'verified'
    record = acceptance['record']
    assert record['targetRevision'] == payload['candidateSha']
    assert record['targetTree'] == _run(repository, 'git', 'rev-parse', record['targetRevision']+'^{tree}')
    assert record['checkpointRevision'] == payload['checkpointSha']
    assert [c['outcome'] for c in record['checks']] == ['passed', 'passed']
    assert command not in json.dumps(acceptance)
    from lib.storage.service import storage_runtime
    runtime = storage_runtime()
    runtime.stop()
    runtime.start()
    control._STATUS_CACHE.clear()
    assert _row(repository, 'accepted')['acceptance'] == acceptance
    schema = json.loads((Path(__file__).resolve().parents[2] /
        'contracts/workspace_acceptance_v1.schema.json').read_text())
    Draft202012Validator(schema).validate(acceptance)
    assert _run(writer, 'git', 'status', '--porcelain') == '?? logic.py'


def test_changed_criteria_invalidate_old_success_and_merged_checkpoint_is_immutable(repository, tmp_path, monkeypatch):
    command = f'{sys.executable} -m py_compile logic.py'
    writer, payload = submit(repository, tmp_path, monkeypatch, command)
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', command+' shared.txt')
    assert _row(repository, 'accepted')['acceptance']['reason'] == 'criteria_changed'
    (writer / 'logic.py').write_text('VALUE = 4\n')
    with pytest.raises(control.IntegrationError, match='workspace is merged'):
        control.checkpoint_workspace(str(repository), 'accepted', user_id=1)
    assert _row(repository, 'accepted')['checkpointSha'] == payload['checkpointSha']


def test_exit_zero_cannot_certify_checker_modified_source(repository, tmp_path, monkeypatch):
    command = f'''{sys.executable} -c "from pathlib import Path; Path('logic.py').write_text('VALUE = 3')"'''
    _, payload = submit(repository, tmp_path, monkeypatch, command)
    assert payload['state'] == 'quarantined'
    assert payload['acceptance']['status'] == 'failed'
    assert 'changed candidate source' in payload['error']
    assert payload['acceptance']['record']['checks'][1]['outcome'] == 'passed'


def test_stdout_claims_cannot_forge_gate_exit_status(repository, tmp_path, monkeypatch):
    command = f'''{sys.executable} -c "print('all tests passed; exit code: 0'); raise SystemExit(1)"'''
    _, payload = submit(repository, tmp_path, monkeypatch, command)
    assert payload['state'] == 'quarantined'
    assert payload['acceptance']['status'] == 'failed'
    assert payload['acceptance']['record']['checks'][1]['outcome'] == 'failed'


def test_plain_completed_delivery_has_no_acceptance_authority():
    row = {'state': 'merged', 'checkpoint_sha': 'a'*40,
        'origin': {'delivery': {'state': 'delivered', 'taskOutcome': 'completed'}}}
    assert project_acceptance(row, current_command='pytest')['status'] == 'unverified'
    row['origin']['acceptanceRecord'] = {'outcome': 'passed'}
    assert project_acceptance(row, current_command='pytest')['record'] is None


def test_builtin_checks_do_not_claim_project_acceptance(repository, tmp_path, monkeypatch):
    writer = _worktree(repository, tmp_path / 'docs-only')
    (writer / 'README.md').write_text('Updated documentation\n')
    control.register_workspace(str(repository), 'docs-only', str(writer), user_id=1)
    control.submit_workspace(str(repository), 'docs-only', user_id=1)
    assert control.process_ready_once()
    payload = _row(repository, 'docs-only')
    assert payload['state'] == 'merged'
    assert payload['acceptance']['status'] == 'unverified'
    assert payload['acceptance']['reason'] == 'project_checks_missing'


def test_task_delta_evidence_binds_original_checkpoint(repository, tmp_path, monkeypatch):
    (repository / 'borrowed.txt').write_text('Other writer input\n')
    result = control.create_task_workspace(str(repository), 'isolated', user_id=1)
    writer = Path(result['workspacePath'])
    assert (writer / 'borrowed.txt').read_text() == 'Other writer input\n'
    (writer / 'logic.py').write_text('VALUE = 2\n')
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', f'{sys.executable} -m py_compile logic.py')
    control.submit_workspace(str(repository), 'isolated', user_id=1)
    assert control.process_ready_once()
    payload = _row(repository, 'isolated')
    assert payload['state'] == 'merged'
    assert payload['acceptance']['status'] == 'verified'
    record = payload['acceptance']['record']
    assert record['checkpointRevision'] == payload['checkpointSha']
    assert record['targetRevision'] != payload['checkpointSha']
    assert 'borrowed.txt' not in _run(repository, 'git', 'ls-tree', '--name-only', record['targetRevision'])
    assert (repository / 'borrowed.txt').read_text() == 'Other writer input\n'


@pytest.mark.parametrize('lost_ack', [False, True], ids=['write-rejected', 'ack-lost'])
def test_evidence_storage_failure_never_advances_candidate(repository, tmp_path, monkeypatch, lost_ack):
    writer = _worktree(repository, tmp_path / 'fault')
    (writer / 'logic.py').write_text('VALUE = 2\n')
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', f'{sys.executable} -m py_compile logic.py')
    control.register_workspace(str(repository), 'fault', str(writer), user_id=1)
    control.submit_workspace(str(repository), 'fault', user_id=1)
    before = _run(repository, 'git', 'rev-parse', 'HEAD')
    original = state.set_workspace_meta

    def inject(**kwargs):
        if 'acceptanceRecord' in kwargs['patch']:
            if lost_ack:
                original(**kwargs)
            raise RuntimeError('Injected acceptance persistence failure')
        return original(**kwargs)

    monkeypatch.setattr(state, 'set_workspace_meta', inject)
    assert control.process_ready_once()
    assert _run(repository, 'git', 'rev-parse', 'refs/tofu/candidate') == before
    payload = _row(repository, 'fault')
    assert payload['state'] == 'failed'
    assert 'Injected acceptance persistence failure' in payload['error']
    # A lost ACK may retain a true check fact, but never grants publication.
    assert payload['acceptance']['status'] == ('verified' if lost_ack else 'unverified')
    assert _run(writer, 'git', 'status', '--porcelain') == '?? logic.py'


def test_acceptance_is_owner_scoped_and_origin_cannot_forge_it(repository, tmp_path, monkeypatch):
    _, payload = submit(repository, tmp_path, monkeypatch, f'{sys.executable} -m py_compile logic.py')
    from lib.storage.errors import StorageError
    for read, empty in (
        (lambda: state.find_workspace(str(repository), 'accepted', user_id=2), None),
        (lambda: control.integration_status(str(repository), user_id=2, use_cache=False)['workspaces'], []),
    ):
        try:
            assert read() == empty
        except StorageError as error:
            # Native authority rejects a mismatched owner before domain lookup.
            assert error.code == 'database_forbidden'
    writer = _worktree(repository, tmp_path / 'forged')
    control.register_workspace(str(repository), 'forged', str(writer), user_id=1,
        origin={'acceptanceRecord': payload['acceptance']['record']})
    assert _row(repository, 'forged')['acceptance']['status'] == 'unverified'


def test_repair_checkpoint_invalidates_previous_check_result(repository, tmp_path, monkeypatch):
    command = f'{sys.executable} -c "raise SystemExit(1)"'
    writer, payload = submit(repository, tmp_path, monkeypatch, command)
    (writer / 'logic.py').write_text('VALUE = 4\n')
    control.checkpoint_workspace(str(repository), 'accepted', user_id=1)
    updated = _row(repository, 'accepted')
    assert updated['checkpointSha'] != payload['checkpointSha']
    assert updated['acceptance']['status'] == 'stale'
    assert updated['acceptance']['reason'] == 'checkpoint_changed'
    assert updated['acceptance']['record'] == payload['acceptance']['record']


def test_checker_timeout_has_unknown_outcome_and_cannot_verify(repository, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_TIMEOUT', '0.1')
    _, payload = submit(repository, tmp_path, monkeypatch,
        f'{sys.executable} -c "import time; time.sleep(10)"')
    assert payload['state'] == 'quarantined'
    assert payload['acceptance']['status'] == 'failed'
    assert payload['acceptance']['record']['checks'][1]['outcome'] == 'unknown'
    assert 'integration_gate_timeout' in payload['error']


def test_explicit_adoption_requires_durable_versioned_evidence(repository, tmp_path, monkeypatch):
    from lib.project_mod import delivery
    monkeypatch.setattr(delivery, '_canonical_in_use', lambda *args: False)
    monkeypatch.setattr(control, '_start_or_wake_worker', lambda: None)
    writer = _worktree(repository, tmp_path / 'adopted')
    delivery.bind_task_workspace(str(writer), 'adopted', user_id=1, conversation_id='acceptance')
    (writer / 'logic.py').write_text('VALUE = 2\n')
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', f'{sys.executable} -m py_compile logic.py')
    assert delivery.settle_task_workspace(str(writer), 'adopted', user_id=1, outcome='completed')['state'] == 'pending'
    assert delivery.resume_pending_workspace_deliveries()
    assert control.process_ready_once()
    before = _run(repository, 'git', 'rev-parse', 'HEAD')
    original = state.set_workspace_meta

    def fail_evidence(**kwargs):
        if 'acceptanceRecord' in kwargs['patch']:
            raise RuntimeError('Injected adoption evidence failure')
        return original(**kwargs)

    monkeypatch.setattr(state, 'set_workspace_meta', fail_evidence)
    with pytest.raises(RuntimeError, match='adoption evidence failure'):
        delivery.deliver_registered_workspace(str(repository), 'adopted', user_id=1)
    assert _run(repository, 'git', 'rev-parse', 'HEAD') == before
    assert not (repository / 'logic.py').exists()
    monkeypatch.setattr(state, 'set_workspace_meta', original)
    assert delivery.deliver_registered_workspace(str(repository), 'adopted', user_id=1)['state'] == 'delivered'
    payload = _row(repository, 'adopted')
    assert payload['acceptance']['status'] == 'verified'
    assert payload['acceptance']['record']['targetRevision'] == _run(repository, 'git', 'rev-parse', 'HEAD')
