"""Streaming result bytes are decoded once, never lossy display fragments."""
from __future__ import annotations

import hashlib

import pytest

from lib.tool_result_artifact_writer import ToolResultArtifactWriter

pytestmark = pytest.mark.unit


class Repository:
    def __init__(self):
        self.content = None

    def put(self, *, user_id, content, media_type):
        self.content = content
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        return {'artifactRef': 'tool-result:' + digest}


def writer(tmp_path, repository=None):
    return ToolResultArtifactWriter(
        user_id=7, threshold_chars=8, artifact_quota_bytes=4096,
        task_quota_bytes=4096, spool_dir=str(tmp_path),
        repository=repository or Repository())


def test_utf8_split_at_every_byte_retains_exact_received_text(tmp_path):
    source = '汉字 café 🙂 Straße\n' * 9
    repo = Repository()
    output = writer(tmp_path, repo)
    for byte in source.encode('utf-8'):
        assert output.write(bytes([byte]))
    result = output.finalize()
    assert not result.degraded
    assert repo.content == source
    assert result.sha256 == hashlib.sha256(source.encode('utf-8')).hexdigest()
    assert result.size_bytes == len(source.encode('utf-8'))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('chunk', [b'\x89PNG\r\n\x1a\n', {'__screenshot__': True, 'dataUrl': 'private-bytes'}])
def test_media_or_invalid_bytes_cannot_be_persisted_as_replacement_text(tmp_path, chunk):
    repo = Repository()
    output = writer(tmp_path, repo)
    assert output.write('safe text')
    assert output.write(chunk) is False
    result = output.finalize()
    assert result.degraded
    assert result.degraded_reason == 'encoding'
    assert result.artifact_ref is None
    assert repo.content is None
    assert 'private-bytes' not in result.text
    assert '\ufffd' not in result.text


def test_partial_codepoint_at_finalize_is_explicit_retention_failure(tmp_path):
    output = writer(tmp_path)
    assert output.write('safe')
    assert output.write(b'\xf0\x9f')
    result = output.finalize(complete=False)
    assert result.degraded and result.degraded_reason == 'encoding'
    assert result.complete is False
    assert result.text == 'safe'
    assert result.artifact_ref is None


def test_text_cannot_interleave_an_unfinished_byte_codepoint(tmp_path):
    output = writer(tmp_path)
    assert output.write(b'\xe6')
    assert output.write('other') is False
    assert output.finalize().degraded_reason == 'encoding'


def test_explicit_quota_cannot_bypass_storage_hard_ceiling(tmp_path):
    output = ToolResultArtifactWriter(
        user_id=7, artifact_quota_bytes=10**12, task_quota_bytes=10**12,
        spool_dir=str(tmp_path))
    assert output.artifact_quota_bytes <= 16 * 1024 * 1024
    assert output.task_quota_bytes <= 4096 * 1024 * 1024
    output.discard()
