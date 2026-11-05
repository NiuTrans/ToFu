"""Atomic file publication keeps the destination and closes untransferred FDs."""

import errno
import os
import sys
from contextlib import suppress

import pytest

from lib import json_store

pytestmark = pytest.mark.unit


class WriteInterrupted(BaseException):
    """Cancellation before the stream owns its file descriptor."""


@pytest.mark.parametrize('writer_name,payload,expected', [
    ('write_text_atomic', '你好\nworld\r\n', '你好\nworld\r\n'.encode('utf-8')),
    ('write_text_atomic', '', b''),
    ('write_bytes_atomic', b'\x00\xff\r\n', b'\x00\xff\r\n'),
    ('write_bytes_atomic', bytearray(b'buffer'), b'buffer'),
    ('write_bytes_atomic', memoryview(b'view'), b'view'),
])
@pytest.mark.parametrize('sync_enabled', [True, False])
def test_publication_preserves_payload_mode_and_sync_policy(
        tmp_path, monkeypatch, writer_name, payload, expected, sync_enabled):
    destination = tmp_path / 'nested' / 'published'
    synced_sizes = []
    original_fsync = json_store.os.fsync

    def record_sync(fd):
        # The flush must already have made the complete payload visible.
        synced_sizes.append(os.fstat(fd).st_size)
        original_fsync(fd)

    monkeypatch.setattr(json_store.os, 'fsync', record_sync)
    getattr(json_store, writer_name)(
        str(destination), payload, fsync=sync_enabled, mode=0o600)

    assert destination.read_bytes() == expected
    assert destination.stat().st_mode & 0o777 == 0o600
    assert synced_sizes == ([len(expected)] if sync_enabled else [])
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize('writer_name,payload', [
    ('write_text_atomic', 'new text\n'),
    ('write_bytes_atomic', b'new bytes\x00'),
])
@pytest.mark.parametrize('failure_point', ['chmod', 'fdopen'])
def test_setup_failure_closes_descriptor_and_preserves_destination(
        tmp_path, monkeypatch, writer_name, payload, failure_point):
    destination = tmp_path / 'published'
    destination.write_bytes(b'last good version')
    created = []
    original_mkstemp = json_store.tempfile.mkstemp

    def capture_descriptor(*args, **kwargs):
        result = original_mkstemp(*args, **kwargs)
        created.append(result)
        return result

    failure = WriteInterrupted('cancelled while preparing the output')

    def interrupt(*args, **kwargs):
        raise failure

    monkeypatch.setattr(json_store.tempfile, 'mkstemp', capture_descriptor)
    monkeypatch.setattr(json_store.os, failure_point, interrupt)

    with pytest.raises(WriteInterrupted) as caught:
        getattr(json_store, writer_name)(str(destination), payload)

    assert caught.value is failure
    assert destination.read_bytes() == b'last good version'
    [(fd, staging_path)] = created
    assert not os.path.exists(staging_path)
    try:
        os.fstat(fd)
    except OSError as error:
        assert error.errno == errno.EBADF
    else:
        os.close(fd)  # A failing regression must not itself leak the descriptor.
        pytest.fail('The atomic writer leaked its untransferred descriptor')


@pytest.mark.parametrize('writer_name,payload', [
    ('write_text_atomic', 'new text'),
    ('write_bytes_atomic', b'new bytes'),
])
def test_cancellation_after_stream_creation_cannot_close_a_reused_descriptor(
        tmp_path, monkeypatch, writer_name, payload):
    destination = tmp_path / 'published'
    destination.write_bytes(b'last good version')
    opened = []
    real_fdopen = json_store.os.fdopen
    failure = WriteInterrupted('cancelled after stream creation')

    def capture_fdopen(*args, **kwargs):
        stream = real_fdopen(*args, **kwargs)
        opened.append(stream)
        return stream

    def interrupt_after_open(frame, event, _arg):
        if (event == 'line' and frame.f_globals is vars(json_store)
                and opened and any(value is opened[-1]
                                   for value in frame.f_locals.values())):
            sys.settrace(None)
            raise failure
        return interrupt_after_open

    monkeypatch.setattr(json_store.os, 'fdopen', capture_fdopen)
    previous_trace = sys.gettrace()
    unrelated = None
    try:
        try:
            sys.settrace(interrupt_after_open)
            with pytest.raises(WriteInterrupted) as caught:
                getattr(json_store, writer_name)(str(destination), payload)
        finally:
            sys.settrace(previous_trace)
        assert caught.value is failure
        assert destination.read_bytes() == b'last good version'
        assert list(tmp_path.iterdir()) == [destination]
        unrelated = os.open(tmp_path / 'unrelated', os.O_CREAT | os.O_WRONLY, 0o600)
        # If the descriptor was reused, closing the old stream must not close
        # this file. If it was not reused, close must not fail with EBADF.
        opened[0].close()
        os.fstat(unrelated)
    finally:
        for stream in opened:
            with suppress(OSError):
                stream.close()
        if unrelated is not None:
            with suppress(OSError):
                os.close(unrelated)
