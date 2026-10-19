"""Killed native Goals resume through the ordinary chat runner and terminal owner."""
import asyncio
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tests.support.native_goal_restart import accept_recovery, file_provider, kill_at_ready

pytestmark = pytest.mark.api


@pytest.mark.parametrize('transport,durability', [('embedded', 'strict'), ('daemon', 'strict'), ('daemon', 'normal')])
@pytest.mark.parametrize('actors,phase,fault', [(1, 'file_not_published', None),
    (2, 'file_published', None), (2, 'file_published', 'lost_ack'), (2, 'file_published', 'changed_binding')])
def test_ordinary_goal_runner_claims_and_completes_pending_actors(tmp_path, monkeypatch, actors, phase, fault, transport, durability):
    from lib.billing import get_balance
    from lib.billing.native_settlement import recover
    from lib.conversations.repository import get_conversation
    from lib.identity import PrincipalContext
    from lib.storage import service
    from lib.turn_lifecycle import get_attempt
    from tests.support.native_goal_resume_probe import resume
    from tests.support.native_checkpoint_authority import checkpoint_authority

    names = ('TOFU_CHECKPOINT_CABI_TEST_BINARY', 'TOFU_CHECKPOINT_CABI_TEST_LIBRARY',
        'TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256', 'TOFU_NATIVE_TERMINAL_TEST_ARCHIVE', 'TOFU_NATIVE_FLOW_TEST_LIBRARY')
    binary, library, source_hash, archive, flow = [os.environ.get(name) for name in names]
    if sys.platform != 'linux' or not all((binary, library, source_hash, archive, flow)):
        pytest.skip('explicit native engine/ROOT/Flow and Linux SIGKILL required')
    directory = tmp_path/'authority'; directory.mkdir()
    subprocess.run([binary, 'init', '--data-dir', str(directory)], check=True, capture_output=True)
    source = kill_at_ready('tests.support.native_goal_crash_probe', dict(directory=str(tmp_path),
        library=library, binary=binary, transport=transport, durability=durability, source_sha256=source_hash, actors=actors, phase=phase), directory=tmp_path, name='source')
    with checkpoint_authority(directory, transport=transport, library=library,
            binary=binary, source_sha256=source_hash, durability=durability) as storage:
        monkeypatch.setattr(service, 'storage_runtime', lambda: SimpleNamespace(client=lambda **kwargs: storage))
        monkeypatch.setattr('lib.project_mod.modifications.SESSIONS_DIR', str(tmp_path/'file-sessions'))
        monkeypatch.setattr('lib.relay_config.billing_enabled', lambda: True)
        try:
            context = accept_recovery(storage, source, target_id='ordinary-resume')
            principal = PrincipalContext.user(subject_id=source['wallet_id'], owner_user_id=2)
            before = get_balance(source['wallet_id'])
            control = storage.native_checkpoint_control
            claims = []
            def observe(operation, *args, **kwargs):
                response = control(operation, *args, **kwargs)
                if operation == 'goal_claim':
                    claims.append(response)
                    if fault == 'lost_ack':
                        raise OSError('injected lost Goal claim acknowledgement')
                return response
            monkeypatch.setattr(storage, 'native_checkpoint_control', observe)
            with file_provider() as (endpoint, bodies):
                started = time.perf_counter()
                result = asyncio.run(resume(task=context.task, seed=context.seed, inputs=context.inputs,
                    claim=None, storage=storage, runs=context.runs, principal=principal,
                    directory=tmp_path, endpoint=endpoint, archive=archive, flow_library=flow,
                    production=True, wallet_id=source['wallet_id'],
                    checkpoint_binding='incompatible-policy' if fault == 'changed_binding' else 'registered-flow-actor'))
                elapsed_ms = (time.perf_counter()-started)*1000
            (tmp_path/'recovery-observation.json').write_text(json.dumps(dict(
                actors=actors, phase=phase, fault=fault, transport=transport, durability=durability, resume_wall_ms=elapsed_ms,
                provider_calls=len(bodies), claim_submissions=len(claims),
                restored_actors=len(result['restored'])), indent=2)+'\n')
            task = context.task
            assert len(claims) == 1 and claims[0]['accepted'] is True
            if fault is not None:
                assert task['status'] == 'error' and task['error']
                assert not bodies and not result['restored']
                assert all(path.read_text() == 'once\n' for path in tmp_path.glob('actor-*.txt'))
                return
            assert task['status'] == 'done', task.get('error')
            fresh = actors*(2 if phase == 'file_not_published' else 1)+1
            assert len(bodies) == fresh and len(result['restored']) == actors
            assert len(list(tmp_path.glob('actor-*.txt'))) == actors
            assert all(path.read_text() == 'once\n' for path in tmp_path.glob('actor-*.txt'))
            assert task['_executionBudget'].consumption_snapshot() == (
                (actors+fresh)*5, actors*(2 if phase == 'file_not_published' else 1), True)
            assert get_attempt(task['_attemptId'], user_id=2)['status'] == 'completed'
            transcript = get_conversation(task['convId'], user_id=2).messages
            assert any('File publication verified.' in str(message.get('content') or '')
                       for message in transcript), [dict(role=m.get('role'), content=m.get('content')) for m in transcript]
            assert context.runs.get_execution_snapshot(task['_goalRunId'])['status'] == 'done'
            for _ in range(2):
                assert recover(wallet_user_id=source['wallet_id'], task_id=task['id'], held_micro=100)
            assert get_balance(source['wallet_id']) == before+source['held_micro']-28*actors-55*fresh
        finally:
            storage.close()
