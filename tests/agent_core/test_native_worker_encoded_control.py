"""Validated encoded controls preserve their envelope in a reused worker."""
import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind', ['terminal_ack', 'model_dispatch_ack'])
def test_encoded_control_reaches_the_owned_worker_without_redecoding(kind):
    from tests.agent_core.test_native_worker_frames import encode
    from lib.agent_core.native_worker_pool import _Input
    packets = []
    worker = SimpleNamespace(owner=7, budget=SimpleNamespace(
        configuration_bytes=65536, frame_bytes=65536),
        process=SimpleNamespace(stdin=SimpleNamespace(write=packets.append)))
    stream = _Input(SimpleNamespace(worker=worker, sequence=3,
                                   done=SimpleNamespace(done=lambda: False)))
    stream.write(b'{"type":"configure","owner_user_id":7}\n')
    value = {'type': kind, 'accepted': True}
    stream.write(encode(value))
    envelope = json.loads(packets[-1])
    assert envelope['type'] == 'worker_control'
    assert envelope['owner_user_id'] == 7
    assert envelope['sequence'] == 3
    assert envelope['frame'] == value
