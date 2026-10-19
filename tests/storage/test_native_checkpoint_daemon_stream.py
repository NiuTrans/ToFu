"""Real checkpoint snapshots cross the existing storage.v2 frame boundary."""
import hashlib
import json
import os
import subprocess
import time

import pytest

from tests.support.native_checkpoint_authority import checkpoint_authority
from tofu_db.generated_checkpoint_control import FORMAT, RESPONSE_HEADROOM_BYTES
from tofu_db.wire import MAX_PAYLOAD_BYTES

pytestmark = pytest.mark.api


@pytest.mark.parametrize('durability', ['strict', 'normal'])
def test_daemon_checkpoint_streams_large_snapshot_and_reopens_same_authority(tmp_path, durability):
    names = ('TOFU_CHECKPOINT_CABI_TEST_BINARY', 'TOFU_CHECKPOINT_CABI_TEST_LIBRARY',
             'TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    binary, library, source = [os.environ.get(name) for name in names]
    if not all((binary, library, source)):
        pytest.skip('explicit source-matched native engine required')
    directory = tmp_path / 'authority'; directory.mkdir()
    subprocess.run([binary, 'init', '--data-dir', str(directory)], check=True, capture_output=True, timeout=30)
    bound = 8 << 20
    content = 'x' * (bound - 2048)
    expected_digest = hashlib.sha256(content.encode()).hexdigest()
    state = dict(format='tofu.native-agent-checkpoint/v1', owner_user_id=2,
        task_id='streamed-checkpoint', epoch='epoch', binding='binding', next_round=1, tool_calls=1,
        messages=[dict(role='user', content='question'), dict(role='tool', content=content)],
        usage=[{}], round_limit=3, tool_limit=3, no_progress_limit=0, failure_limit=0,
        progress=dict(codec='sha256-token-sequence/v1', last_calls=None, last_world='',
            last_evidence=None, last_failure=None, no_progress_streak=0, failure_streak=0))
    assert MAX_PAYLOAD_BYTES < len(json.dumps(state, separators=(',', ':')).encode()) < bound
    def opened():
        return checkpoint_authority(directory, transport='daemon', library=library,
                                    binary=binary, source_sha256=source, durability=durability)
    def call(client, operation, **fields):
        return client.native_checkpoint_control(operation, dict(format=FORMAT, **fields), user_id=2,
            deadline_unix_ms=time.time_ns() // 1000000 + 30000, response_bytes=bound + RESPONSE_HEADROOM_BYTES)
    parameters = dict(owner_user_id=2, task_id='streamed-checkpoint', epoch='epoch',
        binding='binding', token='first', header_bytes=4096, snapshot_bytes=bound, identity_bytes=1024)
    with opened() as client:
        created = call(client, 'create', parameters=parameters); assert created['accepted']
        key = created['capability_id']
        assert call(client, 'load', capability_id=key)['state'] is None
        assert call(client, 'begin', capability_id=key, round=0)['accepted']
        assert call(client, 'complete', capability_id=key, state=state)['accepted']
        assert call(client, 'release', capability_id=key)['accepted']
        assert call(client, 'forget', capability_id=key)['accepted']
    with opened() as client:
        created = call(client, 'create', parameters=dict(parameters, token='second')); assert created['accepted']
        loaded = call(client, 'load', capability_id=created['capability_id'])
        assert loaded['accepted']
        restored = loaded['state']
        assert restored['task_id'] == state['task_id'] and restored['next_round'] == 1
        assert hashlib.sha256(restored['messages'][1]['content'].encode()).hexdigest() == expected_digest
        assert call(client, 'release', capability_id=created['capability_id'])['accepted']
        assert call(client, 'forget', capability_id=created['capability_id'])['accepted']
