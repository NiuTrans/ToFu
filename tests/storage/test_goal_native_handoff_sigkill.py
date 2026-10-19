"""An acknowledged-on-disk Goal handoff survives death before child admission."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.support.native_goal_restart import accept_recovery, claim_recovery, file_provider, kill_at_ready

pytestmark = pytest.mark.api


@pytest.mark.parametrize('actors,phase,handoffs', [(1, 'file_not_published', 1), (1, 'file_published', 1),
    (2, 'file_not_published', 1), (2, 'file_published', 1), (2, 'file_published', 2)])
def test_committed_handoff_without_dispatch_recovers_exact_original_effects(tmp_path, monkeypatch, actors, phase, handoffs):
    from lib.billing import get_balance
    from lib.billing.native_settlement import freeze, recover
    from lib.storage import service
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.tasks_pkg.manager import persist_task_result
    from lib.tasks_pkg.manager._terminal import stamp_chat_task_terminal
    from lib.turn_lifecycle import get_attempt
    from tests.support.native_goal_resume_probe import resume
    from tofu_db.embedded import EmbeddedRuntime

    names = ('TOFU_CHECKPOINT_CABI_TEST_BINARY', 'TOFU_CHECKPOINT_CABI_TEST_LIBRARY',
        'TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256', 'TOFU_NATIVE_TERMINAL_TEST_ARCHIVE', 'TOFU_NATIVE_FLOW_TEST_LIBRARY')
    binary, library, source_hash, archive, flow = [os.environ.get(name) for name in names]
    if sys.platform != 'linux' or not all((binary, library, source_hash, archive, flow)):
        pytest.skip('explicit native engine/ROOT/Flow and Linux SIGKILL required')
    directory = tmp_path/'authority'; directory.mkdir()
    subprocess.run([binary, 'init', '--data-dir', str(directory)], check=True, capture_output=True)
    values = dict(directory=str(tmp_path), library=library, source_sha256=source_hash, actors=actors)
    first = kill_at_ready('tests.support.native_goal_crash_probe', dict(values, phase=phase),
        directory=tmp_path, name='first')
    second = kill_at_ready('tests.support.native_goal_handoff_crash_probe', dict(values, source=first,
        archive=archive, flow=flow), directory=tmp_path, name='handoff')
    assert second['provider_calls'] == 0 and second['handoff_response']['accepted'] is True
    assert second['balance'] == second['initial_balance']+first['held_micro']-28*actors-100
    paid_source_balance = second['initial_balance']+first['held_micro']-28*actors
    for number in range(1, handoffs):
        second = kill_at_ready('tests.support.native_goal_handoff_crash_probe', dict(values, source=second,
            target_id=f'handoff-root-{number}', archive=archive, flow=flow), directory=tmp_path, name=f'handoff-{number}')
        assert second['provider_calls'] == 0
        assert second['source_claim_request']['invoice']['componentsMicro']['totalMicro'] == 0
        assert second['balance'] == paid_source_balance-100
    files = list(tmp_path.glob('actor-*.txt'))
    assert len(files) == (actors if phase == 'file_published' else 0)
    assert all(path.read_text() == 'once\n' for path in files)
    with EmbeddedRuntime(Path(library), directory, owner_id=2, tenant_id=1, secret=b'x'*32) as runtime:
        assert runtime.build_identity()['source_sha256'] == source_hash
        storage = EmbeddedStorageV2Client(runtime, b'x'*32, owner_id=2, tenant_id=1)
        monkeypatch.setattr(service, 'storage_runtime', lambda: SimpleNamespace(client=lambda **kwargs: storage))
        monkeypatch.setattr('lib.project_mod.modifications.SESSIONS_DIR', str(tmp_path/'file-sessions'))
        monkeypatch.setattr('lib.relay_config.billing_enabled', lambda: True)
        try:
            context = accept_recovery(storage, second, target_id='final-root')
            assert len(context.seed.pending_actors.actors) == actors
            principal, claim = claim_recovery(context, storage, second)
            assert claim.request['invoice']['roundCount'] == 0
            assert claim.request['invoice']['componentsMicro']['totalMicro'] == 0
            assert get_balance(first['wallet_id']) == paid_source_balance
            with file_provider() as (endpoint, bodies):
                result = asyncio.run(resume(task=context.task, seed=context.seed, inputs=context.inputs,
                    claim=claim, storage=storage, runs=context.runs, principal=principal,
                    directory=tmp_path, endpoint=endpoint, archive=archive, flow_library=flow))
            fresh = actors*(2 if phase == 'file_not_published' else 1)+1
            assert len(bodies) == fresh and len(result['restored']) == actors
            files = list(tmp_path.glob('actor-*.txt'))
            assert len(files) == actors and all(path.read_text() == 'once\n' for path in files)
            task = context.task
            assert task['_executionBudget'].consumption_snapshot() == (
                (actors+fresh)*5, actors*(2 if phase == 'file_not_published' else 1), True)
            task['content'] = result['outcome'].result['final']
            assert context.goal_service.complete(task, result['outcome'].terminal_outcome)['status'] == 'completed'
            invoice = freeze(task)
            assert invoice['roundCount'] == fresh and invoice['componentsMicro']['totalMicro'] == 55*fresh
            assert stamp_chat_task_terminal(task, status='done', finish_reason='stop')
            assert persist_task_result(task) is not False
            assert get_attempt(task['_attemptId'], user_id=2)['status'] == 'completed'
            for _ in range(2):
                assert recover(wallet_user_id=first['wallet_id'], task_id=task['id'], held_micro=100)
            assert get_balance(first['wallet_id']) == paid_source_balance-55*fresh
        finally:
            storage.close()
