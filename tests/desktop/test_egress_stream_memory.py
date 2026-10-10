"""Consumed desktop response bytes must not accumulate behind the parser."""
import base64
import tracemalloc

import pytest
from lib.desktop import egress

pytestmark = pytest.mark.unit


def test_consumed_desktop_stream_does_not_retain_body_history(monkeypatch):
    chunk_size = 64 * 1024
    encoded = base64.b64encode(b'x' * chunk_size).decode('ascii')
    count = 128
    def frames(command, since_seq=0):
        sequence = since_seq + 1
        return [(sequence, 'body', encoded)], sequence == count
    monkeypatch.setattr('lib.desktop.get_frames', frames)
    monkeypatch.setattr(egress, 'cancel_stream', lambda *args: None)
    monkeypatch.setattr(egress, '_CONSUME_POLL_S', 0)
    reader = egress.EgressStreamReader('command', 'agent', 'owner')
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    try:
        total = 0
        for chunk in reader.iter_content():
            total += len(chunk)
        retained = tracemalloc.get_traced_memory()[0] - before
        assert total == count * chunk_size
        assert retained < 1024 * 1024, retained
    finally:
        reader.close()
        if not tracing:
            tracemalloc.stop()
