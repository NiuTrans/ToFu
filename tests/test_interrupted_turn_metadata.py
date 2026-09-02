"""Terminal task persistence observability tests.
robustness epic (pt_24187d62ba9e4295).

Backstory (conv mrnee15nzqnoej): a task ran while the DB connection pool was
exhausted (400/400). It streamed a first token but EVERY checkpoint / persist
write threw, so ``task_results`` was never written and the trailing assistant
message kept ``finishReason/usage/apiRounds/cost = null``. The finish-bar then
renders only the model name, and the empty metadata was invisible in the logs
(only discoverable by a post-hoc DB query).

Covered here (backend):
  P0.1  ``terminal_state_log_summary`` renders the in-memory finish verdict as a
        single line (finishReason/usage/apiRounds/cost + persisted flag).
  P0.2  ``persist_task_result`` emits that summary at ERROR level when the
        ``task_results`` write throws (pool-exhaustion simulation) — so the
        metadata is recoverable from error.log even though the row is absent.
  P0.3  ``checkpoint_task_partial`` emits the summary when its checkpoint write
        throws AND a finish verdict already exists in memory.
Conversation projection durability is covered by turn-event and settlement
contract tests. The task manager deliberately has no transcript-array writer:
partial executor checkpoints and visible turn projections have separate owners.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_interrupted_turn_metadata.py -v
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sample_task():
    return {
        'id': 'abc1234567-dead-beef-0000-000000000001',
        'convId': 'convtestxyz',
        'content': 'partial answer so far',
        'thinking': 'x',
        'status': 'running',
        'finishReason': 'stop',
        'model': 'aws.claude-opus-4.8',
        'provider_id': 'sankuai',
        'usage': {'inputTokens': 14, 'outputTokens': 7812},
        'apiRounds': [{'usage': {'outputTokens': 100}}, {'usage': {'outputTokens': 200}}],
        'cost': {'costCny': 76.5498, 'costUsd': 10.57},
    }


@pytest.mark.unit
class TestTerminalStateLogSummary:
    def test_summary_carries_finish_verdict(self):
        from lib.tasks_pkg.manager._persist import terminal_state_log_summary
        s = terminal_state_log_summary(_sample_task(), persisted=False)
        assert 'finishReason=stop' in s
        assert 'model=aws.claude-opus-4.8' in s
        assert 'apiRounds=2' in s
        assert 'cost=76.5498' in s
        assert 'persisted=False' in s
        # Cheap: must NOT dump the content/thinking blobs verbatim.
        assert 'partial answer so far' not in s

    def test_summary_never_raises_on_junk(self):
        from lib.tasks_pkg.manager._persist import terminal_state_log_summary
        s = terminal_state_log_summary({'id': 'x'}, persisted=True)
        assert 'persisted=True' in s


@pytest.mark.unit
class TestPersistFailureAlwaysLogs:
    def test_persist_failure_logs_terminal_metadata(self, monkeypatch, caplog):
        """When the task_results write throws (pool exhausted), the terminal
        metadata is emitted at ERROR level with persisted=False."""
        import lib.tasks_pkg.manager._persist as P

        # Make the row write throw like a pool-exhaustion failure.
        def _boom(*a, **k):
            raise RuntimeError('Database connection pool exhausted (400/400)')
        monkeypatch.setattr(P, '_upsert_task_row', _boom)
        # Neutralize terminal bookkeeping outside this persistence assertion.
        import lib.tasks_pkg.manager._sync as S
        monkeypatch.setattr(S, '_update_proactive_execution_status', lambda *a, **k: None)
        monkeypatch.setattr(P, '_stamp_conv_provider_id', lambda *a, **k: None)

        task = _sample_task()
        task['status'] = 'done'
        with caplog.at_level(logging.ERROR):
            P.persist_task_result(task)

        blob = '\n'.join(r.getMessage() for r in caplog.records)
        assert 'TERMINAL METADATA NOT PERSISTED' in blob
        assert 'finishReason=stop' in blob
        assert 'persisted=False' in blob

    def test_persist_success_does_not_emit_not_persisted(self, monkeypatch, caplog):
        import lib.tasks_pkg.manager._persist as P
        monkeypatch.setattr(P, '_upsert_task_row', lambda *a, **k: None)
        import lib.tasks_pkg.manager._sync as S
        monkeypatch.setattr(S, '_update_proactive_execution_status', lambda *a, **k: None)
        monkeypatch.setattr(P, '_stamp_conv_provider_id', lambda *a, **k: None)
        task = _sample_task()
        task['status'] = 'done'
        with caplog.at_level(logging.ERROR):
            P.persist_task_result(task)
        blob = '\n'.join(r.getMessage() for r in caplog.records)
        assert 'TERMINAL METADATA NOT PERSISTED' not in blob


@pytest.mark.unit
class TestCheckpointFailureAlwaysLogs:
    def test_checkpoint_failure_logs_when_verdict_known(self, monkeypatch, caplog):
        import lib.tasks_pkg.manager._sync as S

        def _boom(*a, **k):
            raise RuntimeError('Database connection pool exhausted (400/400)')
        monkeypatch.setattr(S, '_upsert_task_row', _boom)
        task = _sample_task()  # has finishReason
        with caplog.at_level(logging.WARNING):
            S.checkpoint_task_partial(task)
        blob = '\n'.join(r.getMessage() for r in caplog.records)
        assert 'Terminal metadata was not persisted' in blob
        assert 'finishReason=stop' in blob
