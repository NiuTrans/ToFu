"""Exact and semantic tool-progress guards for the main orchestrator.

Incident anchor (2026-08-21, conv mt1mgza3h7ixje / task ee7d564e): kimi-k3
degenerated into a sacrificial-noop loop — ``run_command(command='true',
description='noop')`` for TEN consecutive rounds while its prose narrated
edit_file/write_file intentions. No guard recognised the loop; the turn
burned ~192K cached tokens/round (≈¥9.4) and settled with a give-up message.
Same disease family as the 2026-08-17 ``noop ping placeholder`` incident
(JOURNAL.md), whose fix was the receipts guard — this suite pins the OTHER
half: even when the calls EXECUTE, a no-progress loop must be force-stopped.

Second incident anchor (conv mt2hn4018vm5rh / task 8ddae577): the model
alternated ``true`` with changed ``read_files`` ranges, bypassing the exact
digest for 43 provider rounds.  The semantic suite below pins that shape and
the anti-false-positive boundaries for new read coverage and changed results.

Anti-false-positive contract: a polling loop repeats the same CALL but the
RESULT changes (a growing log tail) — that is productive and must reset the
counter. Only call AND outcome both byte-identical round-over-round counts.
"""

import threading
import pytest

from lib.tasks_pkg.orchestrator._round_state import RoundState
from lib.tasks_pkg.orchestrator._tool_loop_breaker import (
    _round_loop_digest,
    handle_tool_loop_circuit_breaker,
)
from tests._registered_chat_task import registered_chat_task

pytestmark = pytest.mark.unit


def _task(conv='convtest'):
    return {'id': 'abcdef1234567890', 'convId': conv, 'toolRounds': [],
            'events': [], 'events_lock': threading.Lock(),
            '_tool_schema': [
                {'type': 'function', 'function': {'name': name}}
                for name in ('read_files', 'edit_file', 'write_file',
                             'run_command')
            ]}


def _rs():
    return RoundState(model='kimi-k3', preset='p', thinking_enabled=False)


def _round(task, llm_round, name='run_command', args='{"command": "pwd"}',
           content='', status='done', results=None):
    row = {
        'toolName': name, 'toolArgs': args, 'toolContent': content,
        'status': status, 'llmRound': llm_round,
        'roundNum': len(task['toolRounds']),
    }
    if results is not None:
        row['results'] = results
    task['toolRounds'].append(row)


class TestDigest:
    def test_empty_round_has_empty_digest(self):
        task = _task()
        assert _round_loop_digest(task, 0) == ''

    def test_digest_ignores_other_rounds_rows(self):
        task = _task()
        _round(task, 0)
        _round(task, 1, args='{"command": "ls"}')
        assert _round_loop_digest(task, 0) != _round_loop_digest(task, 1)

    def test_digest_covers_result_content(self):
        t1, t2 = _task(), _task()
        _round(t1, 0, content='out A')
        _round(t2, 0, content='out B')
        assert _round_loop_digest(t1, 0) != _round_loop_digest(t2, 0)

    def test_digest_covers_rejection_results(self):
        t1, t2 = _task(), _task()
        t1['toolRounds'].append({'toolName': 'nope', 'toolArgs': '{}',
                                 'toolContent': None, 'status': 'rejected',
                                 'llmRound': 0, 'roundNum': 0,
                                 'results': [{'type': 'error', 'content': 'x'}]})
        t2['toolRounds'].append({'toolName': 'nope', 'toolArgs': '{}',
                                 'toolContent': None, 'status': 'rejected',
                                 'llmRound': 0, 'roundNum': 0,
                                 'results': [{'type': 'error', 'content': 'y'}]})
        assert _round_loop_digest(t1, 0) != _round_loop_digest(t2, 0)


class TestBreaker:
    def test_first_round_never_fires(self):
        task, rs = _task(), _rs()
        _round(task, 0)
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=0, tid='abcdef12') is False
        assert task['_tool_loop_guard']['identical_repeat_count'] == 0

    def test_fires_on_third_consecutive_repeat(self):
        """A caller without a model-message lane still stops on the fourth."""
        task, rs = _task(), _rs()
        with registered_chat_task(task):
            for rn in range(3):
                _round(task, rn)
                assert handle_tool_loop_circuit_breaker(
                    task, rs, round_num=rn, tid='abcdef12') is False, rn
            _round(task, 3)
            assert handle_tool_loop_circuit_breaker(
                task, rs, round_num=3, tid='abcdef12') is True
        assert task['_tool_loop_guard']['identical_repeat_count'] == 3
        assert rs.exit_reason == 'consecutive_identical_rounds_3'
        env = task['error']
        assert env['kind'] == 'tool_loop'
        assert env['severity'] == 'warning'
        assert 'pwd' in env['detail']
        # RENDER_CONTRACT Phase 3: the force-stop pairs ROUND_START.
        assert any(e.get('type') == 'round_end' and e.get('reason') == 'tool_loop'
                   for e in task['events'])

    def test_production_lane_corrects_once_then_stops_if_repeat_continues(self):
        """The real orchestrator gives an exact loop one bounded recovery."""
        task, rs, messages = _task(), _rs(), []
        for rn in range(4):
            _round(task, rn, name='edit_file',
                   args='{"path":"x.py","old_string":"missing","new_string":"b"}',
                   content='Error: anchor not found', status='error')
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False, rn
        assert len(messages) == 1
        assert 'IDENTICAL TOOL LOOP DETECTED' in messages[0]['content']
        assert task['_toolLoopNudges'][0]['reason'] == 'exact_repetition'
        assert 'error' not in task

        _round(task, 4, name='edit_file',
               args='{"path":"x.py","old_string":"missing","new_string":"b"}',
               content='Error: anchor not found', status='error')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=4, tid='abcdef12') is True
        assert len(messages) == 1
        assert task['error']['kind'] == 'tool_loop'
        assert rs.exit_reason == 'consecutive_identical_rounds_4'

    def test_production_lane_accepts_a_changed_strategy_after_correction(self):
        task, rs, messages = _task(), _rs(), []
        for rn in range(4):
            _round(task, rn, name='edit_file',
                   args='{"path":"x.py","old_string":"missing","new_string":"b"}',
                   content='Error: anchor not found', status='error')
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False, rn

        _round(task, 4, name='read_file',
               args='{"path":"x.py","start":1,"end":40}',
               content='fresh source evidence', status='ok')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=4, tid='abcdef12') is False
        assert 'error' not in task
        assert task['_tool_loop_guard']['identical_repeat_count'] == 0
        assert task['_tool_loop_guard']['exact_nudge_digest'] == ''

    def test_changed_args_reset_the_counter(self):
        task, rs = _task(), _rs()
        _round(task, 0, args='{"command": "pwd"}')
        _round(task, 1, args='{"command": "ls"}')
        _round(task, 2, args='{"command": "ls"}')
        for rn in range(3):
            assert handle_tool_loop_circuit_breaker(
                task, rs, round_num=rn, tid='abcdef12') is False
        assert task['_tool_loop_guard']['identical_repeat_count'] == 1

    def test_changed_result_resets_the_counter(self):
        """Anti-false-positive pin: a poll whose output CHANGES is productive."""
        task, rs = _task(), _rs()
        _round(task, 0, args='{"command": "tail build.log"}', content='line1')
        _round(task, 1, args='{"command": "tail build.log"}', content='line1\nline2')
        _round(task, 2, args='{"command": "tail build.log"}',
               content='line1\nline2\nline3')
        for rn in range(3):
            assert handle_tool_loop_circuit_breaker(
                task, rs, round_num=rn, tid='abcdef12') is False
        assert task['_tool_loop_guard']['identical_repeat_count'] == 0

    def test_round_without_tool_rows_resets(self):
        task, rs = _task(), _rs()
        _round(task, 0)
        _round(task, 1)
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=0, tid='abcdef12') is False
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=1, tid='abcdef12') is False
        assert task['_tool_loop_guard']['identical_repeat_count'] == 1
        # Round 2 produced no tool rows (e.g. aborted before dispatch) —
        # the streak must not survive a gap.
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=2, tid='abcdef12') is False
        assert task['_tool_loop_guard']['identical_repeat_count'] == 0

    def test_interrupted_streak_does_not_fire(self):
        task, rs = _task(), _rs()
        _round(task, 0)
        _round(task, 1)
        _round(task, 2, args='{"command": "ls"}')  # broke the streak
        _round(task, 3)
        for rn in range(4):
            assert handle_tool_loop_circuit_breaker(
                task, rs, round_num=rn, tid='abcdef12') is False
        assert task['_tool_loop_guard']['identical_repeat_count'] == 0

    def test_identical_rejections_also_count(self):
        """A repeated identical REJECTED call is the same degenerate shape."""
        task, rs = _task(), _rs()
        for rn in range(4):
            task['toolRounds'].append({
                'toolName': 'search_web', 'toolArgs': '{"q": "x"}',
                'toolContent': None, 'status': 'rejected',
                'llmRound': rn, 'roundNum': rn,
                'results': [{'type': 'error',
                             'content': 'not a real tool'}]})
            fired = handle_tool_loop_circuit_breaker(
                task, rs, round_num=rn, tid='abcdef12')
        assert fired is True
        assert task['error']['kind'] == 'tool_loop'

    def test_custom_threshold(self):
        task, rs = _task(), _rs()
        for rn in range(2):
            _round(task, rn)
            assert handle_tool_loop_circuit_breaker(
                task, rs, round_num=rn, tid='abcdef12',
                max_consecutive_identical=1) is (rn == 1)


class TestSemanticProgressGuard:
    def test_run_command_schema_proactively_forbids_placeholder_noops(self):
        from lib.tools.project import PROJECT_TOOL_RUN_COMMAND

        description = PROJECT_TOOL_RUN_COMMAND['function']['description']
        assert 'Never use a shell no-op as a placeholder' in description
        assert '`true`, `:`, or `exit 0`' in description
        assert 'tool loop' in description

    def test_true_read_true_bypass_is_corrected_then_stopped(self):
        """Regression for mt2hn4018vm5rh: changed reads cannot mask noops."""
        task, rs, messages = _task('mt2hn4018vm5rh'), _rs(), []
        _round(task, 0, name='code_exec',
               args='{"command":"true","description":"占位：无"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=0, tid='abcdef12') is False
        assert len(messages) == 1
        assert messages[0]['role'] == 'user'
        assert 'NO SEMANTIC PROGRESS' in messages[0]['content']
        assert 'edit_file' in messages[0]['content']

        _round(task, 1, name='read_files',
               args='{"path":"lib/x.py","start_line":40,"end_line":100}',
               content='new evidence')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=1, tid='abcdef12') is False

        # Different tool name + different args defeats a byte digest, but it
        # is still the second explicit no-op in the same no-progress episode.
        _round(task, 2, name='run_command',
               args='{"command":": # noop","description":"different"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=2, tid='abcdef12') is True
        assert rs.exit_reason == 'semantic_noop_tool_loop'
        assert task['error']['kind'] == 'tool_loop'
        assert len(messages) == 1  # exactly one bounded correction

    def test_real_command_does_not_erase_the_noop_warning(self):
        task, rs, messages = _task(), _rs(), []
        _round(task, 0, args='{"command":"true"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=0, tid='abcdef12') is False

        _round(task, 1, args='{"command":"true && pytest -q"}', content='ok')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=1, tid='abcdef12') is False

        _round(task, 2, args='{"command":"true"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=2, tid='abcdef12') is True
        assert len(messages) == 1
        assert rs.exit_reason == 'semantic_noop_tool_loop'

    def test_confirmed_write_starts_a_fresh_semantic_episode(self):
        task, rs, messages = _task(), _rs(), []
        _round(task, 0, args='{"command":"true"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=0, tid='abcdef12') is False
        _round(task, 1, name='edit_file',
               args='{"path":"x.py","old_string":"a","new_string":"b"}',
               content='Applied 1/1 edits\n[1] OK x.py [replace]: changed')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=1, tid='abcdef12') is False
        _round(task, 2, args='{"command":"true"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=2, tid='abcdef12') is False
        assert len(messages) == 2
        assert 'error' not in task

    def test_failed_write_does_not_erase_the_noop_warning(self):
        task, rs, messages = _task(), _rs(), []
        _round(task, 0, args='{"command":"true"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=0, tid='abcdef12') is False
        _round(task, 1, name='edit_file',
               args='{"path":"x.py","old_string":"missing","new_string":"b"}',
               content='Error: anchor not found', status='error')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=1, tid='abcdef12') is False
        _round(task, 2, args='{"command":"true"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=2, tid='abcdef12') is True
        assert len(messages) == 1

    def test_done_but_zero_applied_write_does_not_erase_warning(self):
        """Project writes report content failure inside a done tool round."""
        task, rs, messages = _task(), _rs(), []
        _round(task, 0, args='{"command":"true"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=0, tid='abcdef12') is False
        _round(
            task, 1, name='edit_file',
            args='{"edits":[{"path":"x.py","operation":"replace"}]}',
            content=('Applied 0/1 edits (1 failed)\n'
                     '[1] FAIL x.py [replace]: anchor not found'),
            status='done',
            results=[{
                'writeOk': False,
                'editSummaries': [{'path': 'x.py', 'status': 'fail'}],
            }],
        )
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=1, tid='abcdef12') is False
        _round(task, 2, args='{"command":"true"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=2, tid='abcdef12') is True
        assert len(messages) == 1

    def test_partial_batch_write_resets_episode_when_one_edit_applied(self):
        task, rs, messages = _task(), _rs(), []
        _round(task, 0, args='{"command":"true"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=0, tid='abcdef12') is False
        _round(
            task, 1, name='edit_file', args='{"edits":[]}',
            content=('Applied 1/2 edits (1 failed)\n'
                     '[1] OK x.py [replace]: changed\n'
                     '[2] FAIL y.py [replace]: anchor not found'),
            results=[{
                'writeOk': False,
                'editSummaries': [
                    {'path': 'x.py', 'status': 'ok'},
                    {'path': 'y.py', 'status': 'fail'},
                ],
            }],
        )
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=1, tid='abcdef12') is False
        _round(task, 2, args='{"command":"true"}')
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=2, tid='abcdef12') is False
        assert len(messages) == 2
        assert 'error' not in task

    def test_varying_subset_reads_nudge_then_stop(self):
        task, rs, messages = _task(), _rs(), []
        reads = [
            (40, 100, 'lines 40-100'),  # establishes coverage
            (45, 60, 'lines 45-60'),
            (50, 65, 'lines 50-65'),
            (70, 80, 'lines 70-80'),   # third redundant read → correction
            (44, 55, 'lines 44-55'),   # one more → force-stop
        ]
        fired = False
        for rn, (start, end, content) in enumerate(reads):
            _round(
                task, rn, name='read_files',
                args=(f'{{"path":"lib/x.py","start_line":{start},'
                      f'"end_line":{end}}}'),
                content=content,
            )
            fired = handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12')
            if rn < 4:
                assert fired is False, rn
        assert fired is True
        assert len(messages) == 1
        assert 'already returned' in messages[0]['content']
        assert rs.exit_reason == 'semantic_redundant_read_loop'

    def test_adjacent_read_ranges_are_progress(self):
        task, rs, messages = _task(), _rs(), []
        for rn, (start, end) in enumerate(((1, 20), (21, 40), (41, 60))):
            _round(
                task, rn, name='read_files',
                args=(f'{{"path":"lib/x.py","start_line":{start},'
                      f'"end_line":{end}}}'),
                content=f'lines {start}-{end}',
            )
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False
        assert messages == []
        assert task['_tool_loop_guard']['redundant_read_streak'] == 0

    def test_changed_result_for_same_read_is_new_evidence(self):
        task, rs, messages = _task(), _rs(), []
        args = '{"path":"build.log","start_line":1,"end_line":20}'
        _round(task, 0, name='read_files', args=args, content='line 1')
        _round(task, 1, name='read_files', args=args,
               content='line 1\nline 2')
        for rn in range(2):
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False
        assert task['_tool_loop_guard']['redundant_read_streak'] == 0
        assert messages == []

    def test_receipt_bounds_override_requested_range(self):
        """Small files auto-expand; the guard records what the model saw."""
        task, rs = _task(), _rs()
        _round(
            task, 0, name='read_files',
            args='{"path":"small.py","start_line":40,"end_line":50}',
            content='File: small.py (100 lines, 2.0 KB)\n──\nwhole file',
        )
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=0, tid='abcdef12') is False
        assert task['_tool_loop_guard']['read_coverage']['small.py'] == [[1, 100]]

        _round(task, 1, name='read_files',
               args='{"path":"small.py","start_line":60,"end_line":70}',
               content='File: small.py (100 lines, 2.0 KB)\n──\nwhole file')
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=1, tid='abcdef12') is False
        assert task['_tool_loop_guard']['redundant_read_streak'] == 1

    def test_receipt_total_does_not_claim_future_growing_lines(self):
        task, rs = _task(), _rs()
        _round(task, 0, name='read_files',
               args='{"path":"build.log"}',
               content='File: build.log (100 lines, 2.0 KB)\n──\nold')
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=0, tid='abcdef12') is False
        _round(task, 1, name='read_files',
               args='{"path":"build.log","start_line":101,"end_line":120}',
               content='File: build.log (lines 101-120 of 120)\n──\nnew')
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=1, tid='abcdef12') is False
        assert task['_tool_loop_guard']['redundant_read_streak'] == 0
        assert task['_tool_loop_guard']['read_coverage']['build.log'] == [[1, 120]]

    def test_large_file_refusal_does_not_poison_later_ranged_read(self):
        task, rs = _task(), _rs()
        _round(task, 0, name='read_files', args='{"path":"huge.log"}',
               content=('File too large (9.0 MB). Use grep_search to find '
                        'specific content, or read_files with a range.'))
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=0, tid='abcdef12') is False
        assert task['_tool_loop_guard']['read_coverage'] == {}

        _round(task, 1, name='read_files',
               args='{"path":"huge.log","start_line":500,"end_line":550}',
               content='File: huge.log (lines 500-550 of 10000)\n──\ndata')
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=1, tid='abcdef12') is False
        assert task['_tool_loop_guard']['redundant_read_streak'] == 0
        assert task['_tool_loop_guard']['read_coverage']['huge.log'] == [[500, 550]]

    def test_batch_read_records_coverage_for_every_path(self):
        task, rs = _task(), _rs()
        _round(
            task, 0, name='read_files',
            args=('{"reads":['
                  '{"path":"a.py","start_line":1,"end_line":100},'
                  '{"path":"b.py","start_line":1,"end_line":100}]}'),
            content='a and b',
        )
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=0, tid='abcdef12') is False
        coverage = task['_tool_loop_guard']['read_coverage']
        assert set(coverage) == {'a.py', 'b.py'}

        _round(task, 1, name='read_files',
               args='{"path":"b.py","start_line":20,"end_line":30}',
               content='b subset')
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=1, tid='abcdef12') is False
        assert task['_tool_loop_guard']['redundant_read_streak'] == 1

    def test_write_invalidates_old_read_coverage(self):
        task, rs = _task(), _rs()
        read_args = '{"path":"lib/x.py","start_line":1,"end_line":50}'
        _round(task, 0, name='read_files', args=read_args, content='before')
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=0, tid='abcdef12') is False
        _round(task, 1, name='edit_file',
               args='{"path":"lib/x.py","old_string":"a","new_string":"b"}',
               content='edited')
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=1, tid='abcdef12') is False
        _round(task, 2, name='read_files', args=read_args, content='after')
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=2, tid='abcdef12') is False
        assert task['_tool_loop_guard']['redundant_read_streak'] == 0
