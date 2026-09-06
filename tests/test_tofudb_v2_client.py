"""Hermetic storage.v2 client streaming contract tests."""

from __future__ import annotations

import json
import socket
import threading

import pytest

from scripts.tofudb_v2_client import (
    FIELD_CHUNK_INDEX,
    FIELD_COMMAND_ID,
    FIELD_CORRELATION_ID,
    FIELD_ERROR_CODE,
    FIELD_ERROR_MESSAGE,
    FIELD_FINAL,
    FIELD_KIND,
    FIELD_OPERATION,
    FIELD_PAYLOAD,
    FIELD_PROTOCOL_VERSION,
    FIELD_RETRYABLE,
    FIELD_SCHEMA_ID,
    FIELD_STATUS,
    FIELD_STREAM_ID,
    FIELD_TOTAL_PAYLOAD_BYTES,
    KIND_BLOB_CHUNK,
    KIND_REQUEST,
    KIND_RESPONSE,
    KIND_RESPONSE_CHUNK,
    MAX_BLOB_CHUNK_BYTES,
    MAX_PAYLOAD_BYTES,
    PROTOCOL_VERSION,
    STORAGE_SCHEMA_VERSION,
    StorageV2Session,
    encode_message,
    pack_frame,
    recv_frame,
)

pytestmark = pytest.mark.unit


def test_client_streams_large_request_and_reassembles_large_response() -> None:
    client_socket, peer_socket = socket.socketpair()
    client_socket.settimeout(10)
    peer_socket.settimeout(10)
    session = object.__new__(StorageV2Session)
    session._owner_id = 11
    session._tenant_id = 7
    session._counter = 1
    session._sock = client_socket
    session.schema_id = STORAGE_SCHEMA_VERSION

    expected_content = "x" * MAX_PAYLOAD_BYTES
    expected_response = b"y" * (MAX_BLOB_CHUNK_BYTES * 2 + 7)
    peer_failures: list[BaseException] = []

    def peer() -> None:
        try:
            chunks: list[bytes] = []
            correlation: bytes | None = None
            stream_id: bytes | None = None
            saw_final = False
            while True:
                message = recv_frame(peer_socket)
                if message[FIELD_KIND] == KIND_BLOB_CHUNK:
                    assert not saw_final
                    if correlation is None:
                        correlation = message[FIELD_CORRELATION_ID]
                        stream_id = message[FIELD_STREAM_ID]
                    assert message[FIELD_CORRELATION_ID] == correlation
                    assert message[FIELD_STREAM_ID] == stream_id
                    assert message[FIELD_CHUNK_INDEX] == len(chunks)
                    assert message[FIELD_TOTAL_PAYLOAD_BYTES] > MAX_PAYLOAD_BYTES
                    saw_final = bool(message[FIELD_FINAL])
                    chunks.append(message[FIELD_PAYLOAD])
                    continue
                assert message[FIELD_KIND] == KIND_REQUEST
                assert saw_final
                assert message[FIELD_CORRELATION_ID] == correlation
                assert message[FIELD_PAYLOAD] == b""
                assert message[FIELD_OPERATION] == "tool_result_artifact.put"
                assert message[FIELD_COMMAND_ID] == "large-tool-result"
                request_payload = json.loads(b"".join(chunks).decode("utf-8"))
                assert request_payload["content"] == expected_content
                break

            response_chunks = [
                expected_response[offset : offset + MAX_BLOB_CHUNK_BYTES]
                for offset in range(0, len(expected_response), MAX_BLOB_CHUNK_BYTES)
            ]
            for index, chunk in enumerate(response_chunks):
                body = encode_message({
                    FIELD_KIND: KIND_RESPONSE_CHUNK,
                    FIELD_PROTOCOL_VERSION: PROTOCOL_VERSION,
                    FIELD_CORRELATION_ID: correlation,
                    FIELD_SCHEMA_ID: STORAGE_SCHEMA_VERSION,
                    FIELD_PAYLOAD: chunk,
                    FIELD_STREAM_ID: correlation,
                    FIELD_CHUNK_INDEX: index,
                    FIELD_FINAL: index + 1 == len(response_chunks),
                })
                peer_socket.sendall(pack_frame(body))
            terminator = encode_message({
                FIELD_KIND: KIND_RESPONSE,
                FIELD_PROTOCOL_VERSION: PROTOCOL_VERSION,
                FIELD_CORRELATION_ID: correlation,
                FIELD_SCHEMA_ID: STORAGE_SCHEMA_VERSION,
                FIELD_PAYLOAD: b"",
                FIELD_STATUS: 0,
                FIELD_ERROR_CODE: None,
                FIELD_ERROR_MESSAGE: None,
                FIELD_RETRYABLE: False,
            })
            peer_socket.sendall(pack_frame(terminator))
        except BaseException as error:  # surface peer-thread assertions
            peer_failures.append(error)
        finally:
            peer_socket.close()

    peer_thread = threading.Thread(target=peer, daemon=True)
    peer_thread.start()
    try:
        response = session.request(
            "tool_result_artifact.put",
            {
                "user_id": 11,
                "content": expected_content,
                "media_type": "text/plain",
                "created_at_ms": 1,
                "expires_at_ms": 2,
            },
            command_id="large-tool-result",
            deadline_unix_ms=10_000,
        )
        assert response.status == 0
        assert response.payload == expected_response
    finally:
        session.close()
        peer_thread.join(timeout=10)
    assert not peer_thread.is_alive()
    assert not peer_failures
