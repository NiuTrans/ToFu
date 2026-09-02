"""Paper insight persistence stays behind the storage.v1 semantic boundary."""

from __future__ import annotations

import pytest

from lib.paper.insight_engine._run import _persist_insight


pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


class _RecordingClient:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = []

    def command(self, operation, payload, command_id):
        self.calls.append((operation, payload, command_id))
        if self.error is not None:
            raise self.error
        return {'saved': True}


def test_insight_uses_semantic_paper_report_operation(monkeypatch):
    client = _RecordingClient()
    monkeypatch.setattr(
        'lib.storage.get_storage_client',
        lambda *, write=False: client if write else None,
    )

    assert _persist_insight(
        'paper-hash', 'zh', '# 洞察', 'model-a',
        user_id=TEST_OWNER_USER_ID,
        items={'connections': []}, usage={'prompt_tokens': 4}, baseline=3.5,
    ) is True

    operation, payload, command_id = client.calls[0]
    assert operation == 'paper.report.upsert'
    assert payload == {
        'paper_hash': 'paper-hash', 'lang': 'insight:zh',
        'user_id': TEST_OWNER_USER_ID,
        'report': '# 洞察', 'model': 'model-a',
        'meta': {
            'kind': 'insight', 'v': 2, 'items': {'connections': []},
            'baseline': 3.5, 'usage': {'prompt_tokens': 4},
        },
        'created_at': payload['created_at'],
    }
    assert command_id.startswith('paper.insight.upsert:')


def test_insight_storage_failure_preserves_best_effort_contract(monkeypatch):
    client = _RecordingClient(RuntimeError('sidecar unavailable'))
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: client)

    assert _persist_insight(
        'paper-hash', 'en', 'body', 'model-a',
        user_id=TEST_OWNER_USER_ID,
    ) is False
