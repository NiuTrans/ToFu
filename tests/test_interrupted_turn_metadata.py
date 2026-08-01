"""P0 observability + P1a finish-bar root-fix tests for the interrupted-turn
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
  P1a   ``_sync_partial_to_conversation`` carries the computed terminal metadata
        (finishReason/usage/apiRounds) onto the trailing assistant message, not
        just the 15-char content — so a partial DB sync that survives a crash
        already has a populated finish-bar. GATED (2026-07-31) on finalize being
        genuinely underway (terminal status, or the ``_finalize_started_at``
        latch): carrying the verdict inside the L843→L954 finalize window marked
        a still-generating turn as settled and minted a duplicate agent bubble.
        When carried, ``_taskId`` lands WITH it (a terminal field without its
        identity anchor cannot be recognised as its own completed turn).
  NEUTER P1a: with the finishReason-carry stripped, the partial sync leaves the
        message finishReason empty (proving the carry is load-bearing).

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_interrupted_turn_metadata.py -v
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

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
        # Neutralize the downstream conversation fan-out (not under test here).
        import lib.tasks_pkg.manager._sync as S
        monkeypatch.setattr(S, '_sync_result_to_conversation', lambda *a, **k: None)
        monkeypatch.setattr(S, '_update_proactive_execution_status', lambda *a, **k: None)
        monkeypatch.setattr(S, '_dispatch_queued_message', lambda *a, **k: None)
        monkeypatch.setattr(S, '_maybe_refresh_project_summary', lambda *a, **k: None)

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
        monkeypatch.setattr(S, '_sync_result_to_conversation', lambda *a, **k: None)
        monkeypatch.setattr(S, '_update_proactive_execution_status', lambda *a, **k: None)
        monkeypatch.setattr(S, '_dispatch_queued_message', lambda *a, **k: None)
        monkeypatch.setattr(S, '_maybe_refresh_project_summary', lambda *a, **k: None)
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
        # Skip the conversation partial sync (own DB path, not under test).
        monkeypatch.setattr(S, '_sync_partial_to_conversation', lambda *a, **k: None)

        task = _sample_task()  # has finishReason
        with caplog.at_level(logging.WARNING):
            S.checkpoint_task_partial(task)
        blob = '\n'.join(r.getMessage() for r in caplog.records)
        assert 'CHECKPOINT NOT PERSISTED' in blob
        assert 'finishReason=stop' in blob


@pytest.mark.unit
class TestPartialSyncCarriesTerminalMeta:
    """P1a: when a checkpoint fires late in a turn, the partial sync onto the
    conversation message must carry the already-computed finishReason / usage /
    apiRounds, not only the content — so a crash-recovered partial already has a
    populated finish-bar."""

    def _fake_conv_db(self, messages):
        """A minimal db double with the two queries _sync_partial_to_conversation
        uses (SELECT messages+updated_at+rev; UPDATE ... WHERE rev=? RETURNING
        rowcount). The row MUST carry the third ``rev`` column — the Phase-4
        W-partial CAS (ec6a2865) reads row[2]; a 2-column row raises IndexError
        inside the CAS retry, which is swallowed and retried 3x, so NO write is
        ever captured (the red-baseline root cause)."""
        state = {'messages': json.dumps(messages), 'updated_at': 111, 'rev': 7}
        captured = {}

        class _Cur:
            def __init__(self, rowcount=1):
                self.rowcount = rowcount

        class _DB:
            def execute(self, sql, params=()):
                s = ' '.join(sql.split())
                if s.startswith('SELECT messages, updated_at'):
                    return _FetchRow([state['messages'], state['updated_at'], state['rev']])
                if s.startswith('UPDATE conversations SET messages'):
                    captured['messages'] = params[0]
                    return _Cur(1)
                return _FetchRow(None)

            def commit(self):
                pass

        class _FetchRow:
            def __init__(self, v):
                self._v = v

            def fetchone(self):
                return self._v

        return _DB(), captured

    def test_partial_sync_writes_finish_metadata(self, monkeypatch):
        """★ FIXTURE CORRECTED 2026-07-31 (duplicate-bubble root fix).

        This test used to drive `_sample_task()` verbatim — `status='running'`
        WITH `finishReason='stop'` and NO `_finalize_started_at` — and assert
        the verdict was persisted onto the conversation message. That input is
        precisely the ~110-line window in `orchestrator/_finalize.py` where the
        verdict is stamped (L843) but the terminal flip has not happened
        (L954), a span holding the BLOCKING `_generate_tool_summary` call.
        Persisting there marks a STILL-GENERATING turn as settled, and the
        frontend answers that with a duplicate assistant bubble that survives a
        reload (the row is in the DB). So the old fixture certified the defect
        as correct behaviour.

        P1a's actual purpose — a checkpoint that outlives a FAILED terminal
        persist must still leave a populated finish-bar — is unchanged, and is
        what this test now pins: the `_finalize_started_at` latch (stamped at
        L953, one line before the terminal flip) marks finalize genuinely
        underway. The withholding half is pinned by the complement below and by
        tests/test_partial_checkpoint_terminal_identity.py.
        """
        import lib.tasks_pkg.manager._sync as S

        messages = [
            {'role': 'user', 'content': 'q'},
            {'role': 'assistant', 'content': '', 'thinking': '', '_taskId': 'abc1234567-dead-beef-0000-000000000001'},
        ]
        db, captured = self._fake_conv_db(messages)
        monkeypatch.setattr(S, 'get_thread_db', lambda *a, **k: db)
        # Latest-task guard must pass.
        monkeypatch.setattr(S, '_latest_task_for_conv', lambda cid: None, raising=False)

        task = _sample_task()
        task['content'] = 'partial'
        # Finalize is genuinely underway — the latch the orchestrator stamps
        # immediately before flipping status to 'done'.
        task['_finalize_started_at'] = time.time()
        S._sync_partial_to_conversation(task)

        assert 'messages' in captured, 'partial sync should have written the messages column'
        out = json.loads(captured['messages'])
        am = out[-1]
        assert am['finishReason'] == 'stop', 'finishReason must be carried onto the message'
        assert am.get('usage'), 'usage must be carried onto the message'
        assert am.get('apiRounds'), 'apiRounds must be carried onto the message'
        assert am.get('_taskId'), (
            'the identity anchor must land WITH the verdict — a terminal field '
            'without _taskId is a row that cannot be recognised as its own '
            'completed turn, so a reload mints a duplicate assistant bubble')

    def test_partial_sync_withholds_verdict_inside_the_finalize_window(self, monkeypatch):
        """Complement of the test above, kept adjacent so the pair reads as one
        contract: the SAME task WITHOUT the finalize latch (still inside the
        L843→L954 window) must NOT have its verdict persisted."""
        import lib.tasks_pkg.manager._sync as S

        messages = [
            {'role': 'user', 'content': 'q'},
            {'role': 'assistant', 'content': '', 'thinking': ''},
        ]
        db, captured = self._fake_conv_db(messages)
        monkeypatch.setattr(S, 'get_thread_db', lambda *a, **k: db)
        monkeypatch.setattr(S, '_latest_task_for_conv', lambda cid: None, raising=False)

        task = _sample_task()          # status='running', finishReason='stop'
        task['content'] = 'partial'
        task.pop('_finalize_started_at', None)
        S._sync_partial_to_conversation(task)

        if 'messages' in captured:
            am = json.loads(captured['messages'])[-1]
            assert not am.get('finishReason'), (
                'the verdict was persisted while the turn was still generating '
                f'(status=running, finalize not started): {am}')

    def test_neuter_finishreason_carry_is_load_bearing(self, monkeypatch):
        """Prove the finishReason carry is what populates the finish-bar: with a
        task that has NO finishReason yet (mid-stream), the partial sync leaves
        the message finishReason empty."""
        import lib.tasks_pkg.manager._sync as S
        messages = [
            {'role': 'user', 'content': 'q'},
            {'role': 'assistant', 'content': '', 'thinking': ''},
        ]
        db, captured = self._fake_conv_db(messages)
        monkeypatch.setattr(S, 'get_thread_db', lambda *a, **k: db)
        monkeypatch.setattr(S, '_latest_task_for_conv', lambda cid: None, raising=False)

        task = _sample_task()
        task['content'] = 'partial'
        task.pop('finishReason', None)   # mid-stream: verdict not computed yet
        S._sync_partial_to_conversation(task)

        out = json.loads(captured['messages'])
        assert not out[-1].get('finishReason'), 'no verdict yet → no finishReason on the message'
