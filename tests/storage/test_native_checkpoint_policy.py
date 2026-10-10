"""Selected embedded storage and application policy over a real private library."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from lib.agent_core.native_process import CheckpointRequest, CheckpointTerminalRequest, TerminalRequest
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
from tofu_db.embedded import EmbeddedRuntime, EmbeddedTransportError


@pytest.mark.unit
def test_selected_checkpoint_client_preserves_owner_and_transport_failure():
    class Runtime:
        closed = False
        checkpoint_supported = True
        calls = []
        def checkpoint_control(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise EmbeddedTransportError('lost checkpoint response')
    runtime = Runtime()
    client = EmbeddedStorageV2Client(runtime, b'x' * 32, owner_id=7, tenant_id=3)
    for owner in [8, True]:
        with pytest.raises(StorageError, match='owner differs'):
            client.native_checkpoint_available(user_id=owner)
    assert runtime.calls == []
    with pytest.raises(StorageError) as caught:
        client.native_checkpoint_control('finish', {}, user_id=7, deadline_unix_ms=1, response_bytes=4096)
    assert isinstance(caught.value.__cause__, EmbeddedTransportError)
    assert len(runtime.calls) == 1
    client.close()
    with pytest.raises(StorageError, match='client closed'):
        client.native_checkpoint_available(user_id=7)
    assert len(runtime.calls) == 1


@pytest.mark.api
@pytest.mark.parametrize('terminal_saved', [False, True])
def test_real_application_checkpoint_pause_requires_terminal_ack_before_resume(tmp_path, terminal_saved):
    library = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    binary = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    expected = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not library or not binary or not expected:
        pytest.skip('explicit private checkpoint library, binary and source identity required')
    directory = tmp_path / 'authority'
    directory.mkdir()
    subprocess.run([binary, 'init', '--data-dir', str(directory)], check=True, capture_output=True)
    principal = PrincipalContext.user(subject_id='checkpoint-app-contract', owner_user_id=7)
    task = {'id': 'task', '_userId': 7}
    request = CheckpointRequest(principal, 'task', 'load', 0, 'epoch', 'binding', threading.Event())
    state = dict(format='tofu.native-agent-checkpoint/v1', owner_user_id=7,
        task_id='task', epoch='epoch', binding='binding', next_round=1, tool_calls=1,
        messages=[dict(role='user', content='question'), dict(role='tool', content='result' * 10000)],
        usage=[{}], round_limit=3, tool_limit=3, no_progress_limit=0, failure_limit=0,
        progress=dict(codec='sha256-token-sequence/v1', last_calls=None, last_world='',
            last_evidence=None, last_failure=None, no_progress_streak=0, failure_streak=0))
    state['deadline_unix_ms'] = 18446744073709551615
    restored = []
    async def restore(callback, snapshot):
        assert callback.principal == principal
        restored.append(snapshot)
    def opened():
        return EmbeddedRuntime(Path(library), directory, owner_id=7, tenant_id=3, secret=b'x' * 32)
    def policy(client, executor, token):
        return NativeCheckpointPolicy(principal=principal, task=task, storage=client,
            executor=executor, epoch='epoch', binding='binding', token=token,
            header_bytes=4096, snapshot_bytes=1048576, identity_bytes=1024,
            io_timeout_ms=30000, restore=restore, suspend=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with opened() as runtime:
            assert runtime.build_identity()['source_sha256'] == expected
            client = EmbeddedStorageV2Client(runtime, b'x' * 32, owner_id=7, tenant_id=3)
            async def pause():
                checkpoint = policy(client, executor, 'first')
                assert await checkpoint.load(request) is None
                await checkpoint.begin(replace(request, action='begin'))
                assert await checkpoint.complete(replace(request, action='complete', state=state)) == 'suspend'
                terminal = TerminalRequest(principal, 'task', 'commit', 1,
                    json.dumps({'status': 'aborted'}).encode(), b'{}')
                callback = CheckpointTerminalRequest(principal, 'task', 'epoch', 'binding',
                    terminal, True, request.cancelled)
                assert await checkpoint.seal(callback) == 'accepted'
                assert await checkpoint.seal(callback) == 'accepted'
                with pytest.raises(ValueError, match='invalid'):
                    await checkpoint.seal(replace(callback, suspended=False))
                await checkpoint.close(terminal_saved=terminal_saved)
            asyncio.run(pause())
            client.close()
        with opened() as runtime:
            client = EmbeddedStorageV2Client(runtime, b'x' * 32, owner_id=7, tenant_id=3)
            async def resume():
                checkpoint = policy(client, executor, 'second')
                try:
                    if terminal_saved:
                        assert await checkpoint.load(request) == state
                        assert restored == [state]
                    else:
                        with pytest.raises(ValueError, match='ineligible'):
                            await checkpoint.load(request)
                        assert restored == []
                finally:
                    await checkpoint.close()
            asyncio.run(resume())
            client.close()
