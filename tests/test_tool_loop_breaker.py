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

import json
import threading
import pytest

from lib.tasks_pkg.orchestrator._round_state import RoundState
from lib.tasks_pkg.orchestrator._tool_loop_breaker import (
    _failure_fingerprint,
    _round_loop_digest,
    _shell_command_is_observation,
    finish_after_background_task_acceptance,
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
           content='', status='done', results=None,
           tool_result_evidence=None):
    row = {
        'toolName': name, 'toolArgs': args, 'toolContent': content,
        'status': status, 'llmRound': llm_round,
        'roundNum': len(task['toolRounds']),
    }
    if results is not None:
        row['results'] = results
    if tool_result_evidence is not None:
        row['toolResultEvidence'] = tool_result_evidence
    task['toolRounds'].append(row)


def _v2_result(*, evidence, artifact='', summary='bounded preview',
               status='partial', items=(), truncated=True):
    from lib.tools.result_envelope import ToolResultEnvelopeV2
    return ToolResultEnvelopeV2(
        status=status, summary=summary, items=tuple(items),
        artifact_ref=artifact, cursor='0' if artifact else '',
        truncated=truncated, raw_bytes=50_000,
        evidence_id=evidence,
    ).with_visible_bytes().to_envelope_text()


def _single_tool_message_round(
        task, messages, llm_round, *, name='run_command', args=None,
        content=None, status='done'):
    call_id = f'{name}-{llm_round}-{len(task["toolRounds"])}'
    if args is None:
        # An opaque (non-allowlisted) probe: rounds built with this helper
        # exercise the efficiency nudges and must stay invisible to the
        # observation-stall detector, which counts allowlisted read-only
        # shell commands like ``pwd`` toward a stall.
        args = ('{"command":"probe-service --check",'
                '"description":"probe-%d"}') % llm_round
    if content is None:
        content = f'fresh evidence {llm_round}'
    messages.extend([
        {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': call_id,
            'type': 'function',
            'function': {'name': name, 'arguments': args},
        }]},
        {'role': 'tool', 'tool_call_id': call_id, 'content': content},
    ])
    _round(
        task, llm_round, name=name, args=args, content=content,
        status=status,
    )


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

    def test_discarded_provider_attempt_does_not_enter_round_digest(self):
        baseline = _task()
        _round(baseline, 0, args='{"command": "pwd"}', content='real')
        with_transport_artifact = _task()
        with_transport_artifact['toolRounds'].append({
            'toolName': 'run_command', 'toolArgs': '{"command": "true"}',
            'toolContent': None, 'status': 'aborted', 'llmRound': 0,
            'roundNum': 0, '_providerAttemptDiscarded': True,
            'results': [{'badge': 'superseded'}],
        })
        _round(with_transport_artifact, 0, args='{"command": "pwd"}',
               content='real')

        assert _round_loop_digest(with_transport_artifact, 0) == (
            _round_loop_digest(baseline, 0))


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
        assert messages[0]['_isMeta'] is True
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

    def test_productive_serial_reads_get_one_local_ptc_adoption_nudge(self):
        task, rs = _task(), _rs()
        task['_ptc_local'] = {
            'tier': 'program',
            'eligible': ['read_files'],
        }
        task['_toolOrchestrationDecisions'] = [{
            'programmaticBackend': 'local',
        }]
        messages = [{'role': 'user', 'content': 'inspect the repository'}]

        for rn in range(6):
            call_id = f'read-{rn}'
            args = (
                f'{{"path":"lib/file_{rn}.py","start_line":1,'
                '"end_line":20}'
            )
            content = (
                f'File: lib/file_{rn}.py (lines 1-20 of 100)\n'
                f'──\nsource evidence {rn}'
            )
            messages.extend([
                {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': call_id,
                    'type': 'function',
                    'function': {
                        'name': 'read_files',
                        'arguments': args,
                    },
                }]},
                {'role': 'tool', 'tool_call_id': call_id,
                 'content': content},
            ])
            _round(task, rn, name='read_files', args=args, content=content)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False

        nudges = [
            message for message in messages
            if 'SERIAL READ CHAIN DETECTED' in str(message.get('content'))
        ]
        assert len(nudges) == 1
        assert nudges[0]['_isMeta'] is True
        assert len(task['_programmaticAdoptionNudges']) == 1
        assert task['_programmaticAdoptionNudges'][0] == {
            'afterRound': 3,
            'targetRound': 4,
            'reason': 'serial_direct_reads',
            'chainLength': 3,
            'tools': ['read_files', 'read_files', 'read_files'],
            'max': 4,
        }
        from lib.tasks_pkg.manager._persist import build_result_meta
        assert build_result_meta(task)['programmaticAdoptionNudges'] == (
            task['_programmaticAdoptionNudges']
        )
        assert '_toolRoundTripNudges' not in task

    @pytest.mark.parametrize(
        ('damaged_field', 'damaged_value'),
        (
            ('_tool_loop_guard', {
                'programmatic_adoption_nudge_count': 'not-an-integer',
            }),
            ('_programmaticAdoptionNudges', {'not': 'a-list'}),
        ),
    )
    def test_damaged_recovery_state_cannot_break_ptc_adoption_nudge(
            self, damaged_field, damaged_value):
        task, rs = _task(), _rs()
        task['_ptc_local'] = {
            'tier': 'program',
            'eligible': ['read_files'],
        }
        task['_toolOrchestrationDecisions'] = [{
            'programmaticBackend': 'local',
        }]
        task[damaged_field] = damaged_value
        messages = [{'role': 'user', 'content': 'inspect the repository'}]

        for rn in range(3):
            call_id = f'read-{rn}'
            args = f'{{"path":"lib/file_{rn}.py"}}'
            content = f'File: lib/file_{rn}.py (20 lines, 1 KB)\n──\nx'
            messages.extend([
                {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': call_id,
                    'type': 'function',
                    'function': {
                        'name': 'read_files',
                        'arguments': args,
                    },
                }]},
                {'role': 'tool', 'tool_call_id': call_id,
                 'content': content},
            ])
            _round(task, rn, name='read_files', args=args, content=content)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False

        assert task['_tool_loop_guard'][
            'programmatic_adoption_nudge_count'] == 1
        assert len(task['_programmaticAdoptionNudges']) == 1

    def test_native_ptc_projection_does_not_get_local_adoption_nudge(self):
        task, rs = _task(), _rs()
        task['_ptc_local'] = {
            'tier': 'program',
            'eligible': ['read_files'],
        }
        task['_toolOrchestrationDecisions'] = [{
            'programmaticBackend': 'native_openai',
        }]
        messages = [{'role': 'user', 'content': 'inspect the repository'}]
        for rn in range(3):
            call_id = f'read-{rn}'
            args = f'{{"path":"lib/file_{rn}.py"}}'
            content = f'fresh source evidence {rn}'
            messages.extend([
                {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': call_id,
                    'type': 'function',
                    'function': {
                        'name': 'read_files',
                        'arguments': args,
                    },
                }]},
                {'role': 'tool', 'tool_call_id': call_id,
                 'content': content},
            ])
            _round(task, rn, name='read_files', args=args, content=content)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False

        assert '_programmaticAdoptionNudges' not in task

    def test_adoption_nudge_metadata_is_bounded_and_damage_tolerant(self):
        from lib.storage_projection import (
            project_task_result_metadata_for_storage,
        )
        from lib.tasks_pkg.manager._persist import build_result_meta

        task = {
            '_programmaticAdoptionNudges': [
                None,
                {
                    'afterRound': 'damaged',
                    'targetRound': float('inf'),
                    'reason': 'r' * 100,
                    'chainLength': -50,
                    'tools': ['tool-' + ('x' * 200)] * 10,
                    'max': object(),
                    'prompt': 'model-visible content must not persist',
                },
            ],
            '_toolRoundTripNudges': [
                None,
                {
                    'afterRound': '7',
                    'targetRound': 8,
                    'reason': 'q' * 100,
                    'chainLength': 6,
                    'tools': ['run_command'] * 10,
                    'max': 99,
                    'prompt': 'model-visible content must not persist',
                },
            ],
        }

        meta = build_result_meta(task)
        public = meta['programmaticAdoptionNudges']
        assert public == [{
            'afterRound': 0,
            'targetRound': 0,
            'reason': 'r' * 64,
            'chainLength': 0,
            'tools': [('tool-' + ('x' * 200))[:128]] * 6,
            'max': 0,
        }]
        assert meta['toolRoundTripNudges'] == [{
            'afterRound': 7,
            'targetRound': 8,
            'reason': 'q' * 64,
            'chainLength': 6,
            'tools': ['run_command'] * 6,
            'max': 99,
        }]
        assert project_task_result_metadata_for_storage(meta) == meta
        damaged = build_result_meta({
            '_programmaticAdoptionNudges': {'not': 'a-list'},
            '_toolRoundTripNudges': {'not': 'a-list'},
        })
        assert 'programmaticAdoptionNudges' not in damaged
        assert 'toolRoundTripNudges' not in damaged

    def test_safety_correction_preempts_ptc_adoption_nudge_same_round(self):
        task, rs = _task(), _rs()
        task['_ptc_local'] = {
            'tier': 'program',
            'eligible': ['read_files'],
        }
        task['_toolOrchestrationDecisions'] = [{
            'programmaticBackend': 'local',
        }]
        messages = [{'role': 'user', 'content': 'inspect the repository'}]
        ranges = ((1, 100), (10, 20), (30, 40))
        for rn, (start, end) in enumerate(ranges):
            call_id = f'read-{rn}'
            args = (
                f'{{"path":"lib/x.py","start_line":{start},'
                f'"end_line":{end}}}'
            )
            content = f'lines {start}-{end}'
            messages.extend([
                {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': call_id,
                    'type': 'function',
                    'function': {
                        'name': 'read_files',
                        'arguments': args,
                    },
                }]},
                {'role': 'tool', 'tool_call_id': call_id,
                 'content': content},
            ])
            _round(task, rn, name='read_files', args=args, content=content)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12',
                max_redundant_read_rounds=2) is False

        corrections = [
            message for message in messages
            if message.get('_isMeta') is True
        ]
        assert len(corrections) == 1
        assert 'NO SEMANTIC PROGRESS' in corrections[0]['content']
        assert '_programmaticAdoptionNudges' not in task

    def test_serial_reads_do_not_nudge_without_local_ptc_projection(self):
        task, rs = _task(), _rs()
        messages = [{'role': 'user', 'content': 'inspect the repository'}]
        for rn in range(3):
            call_id = f'read-{rn}'
            args = f'{{"path":"lib/file_{rn}.py"}}'
            content = f'File: lib/file_{rn}.py (20 lines, 1 KB)\n──\nx'
            messages.extend([
                {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': call_id,
                    'type': 'function',
                    'function': {
                        'name': 'read_files',
                        'arguments': args,
                    },
                }]},
                {'role': 'tool', 'tool_call_id': call_id,
                 'content': content},
            ])
            _round(task, rn, name='read_files', args=args, content=content)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False

        assert not any(
            'SERIAL READ CHAIN DETECTED' in str(message.get('content'))
            for message in messages
        )
        assert '_programmaticAdoptionNudges' not in task

    def test_six_productive_single_tool_rounds_get_one_efficiency_nudge(self):
        task, rs = _task(), _rs()
        messages = [{'role': 'user', 'content': 'inspect the repository'}]

        for rn in range(9):
            _single_tool_message_round(task, messages, rn)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False
            if rn < 5:
                assert '_toolRoundTripNudges' not in task

        nudges = [
            message for message in messages
            if 'SERIAL SINGLE-TOOL CHAIN DETECTED'
            in str(message.get('content'))
        ]
        assert len(nudges) == 1
        assert nudges[0]['_isMeta'] is True
        assert task['_toolRoundTripNudges'] == [{
            'afterRound': 6,
            'targetRound': 7,
            'reason': 'serial_single_tool_rounds',
            'chainLength': 6,
            'tools': ['run_command'] * 6,
            'max': 4,
        }]
        from lib.tasks_pkg.manager._persist import build_result_meta
        assert build_result_meta(task)['toolRoundTripNudges'] == (
            task['_toolRoundTripNudges'])

    def test_efficiency_nudge_rearms_sparsely_and_caps_at_four(self):
        task, rs = _task(), _rs()
        messages = [{'role': 'user', 'content': 'inspect the repository'}]

        for rn in range(108):
            _single_tool_message_round(task, messages, rn)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False

        evidence = task['_toolRoundTripNudges']
        assert [row['afterRound'] for row in evidence] == [6, 30, 54, 78]
        assert all(row['max'] == 4 for row in evidence)
        assert task['_tool_loop_guard'][
            'round_trip_efficiency_nudge_count'] == 4
        prompts = [
            message for message in messages
            if 'SERIAL SINGLE-TOOL CHAIN DETECTED'
            in str(message.get('content'))
        ]
        assert len(prompts) == 4
        retained_bytes = sum(
            len(message['content'].encode('utf-8')) for message in prompts
        ) + len(json.dumps(
            evidence, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8'))
        assert retained_bytes <= 3 * 1024

    def test_persisted_efficiency_witness_recovers_remaining_budget(self):
        task, rs = _task(), _rs()
        task['_toolRoundTripNudges'] = [{
            'afterRound': 6,
            'targetRound': 7,
            'reason': 'serial_single_tool_rounds',
            'chainLength': 6,
            'tools': ['run_command'] * 6,
            'max': 4,
        }]
        messages = [{'role': 'user', 'content': 'continued task'}]

        for rn in range(6, 30):
            _single_tool_message_round(task, messages, rn)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False

        assert [row['afterRound'] for row in task['_toolRoundTripNudges']] == [
            6, 30,
        ]
        assert task['_tool_loop_guard'][
            'round_trip_efficiency_nudge_count'] == 2

    def test_five_single_tool_rounds_do_not_get_efficiency_nudge(self):
        task, rs = _task(), _rs()
        messages = [{'role': 'user', 'content': 'inspect the repository'}]
        for rn in range(5):
            _single_tool_message_round(task, messages, rn)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False
        assert '_toolRoundTripNudges' not in task

    def test_failed_single_tool_round_does_not_get_efficiency_nudge(self):
        task, rs = _task(), _rs()
        messages = [{'role': 'user', 'content': 'inspect the repository'}]
        for rn in range(6):
            _single_tool_message_round(
                task, messages, rn, status='error' if rn == 5 else 'done')
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False
        assert '_toolRoundTripNudges' not in task

    def test_efficiency_and_local_ptc_nudges_share_one_task_budget(self):
        task, rs = _task(), _rs()
        messages = [{'role': 'user', 'content': 'inspect the repository'}]
        for rn in range(6):
            _single_tool_message_round(task, messages, rn)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False

        task['_ptc_local'] = {'tier': 'program', 'eligible': ['read_files']}
        task['_toolOrchestrationDecisions'] = [{
            'programmaticBackend': 'local',
        }]
        for rn in range(6, 9):
            _single_tool_message_round(
                task, messages, rn, name='read_files',
                args=f'{{"path":"lib/file_{rn}.py"}}',
            )
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False

        assert len(task['_toolRoundTripNudges']) == 1
        assert '_programmaticAdoptionNudges' not in task

    def test_parallel_round_breaks_single_tool_efficiency_chain(self):
        task, rs = _task(), _rs()
        messages = [{'role': 'user', 'content': 'inspect the repository'}]
        for rn in range(5):
            _single_tool_message_round(task, messages, rn)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False

        messages.append({'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'parallel-a', 'type': 'function', 'function': {
                'name': 'read_files', 'arguments': '{"path":"a.py"}'}},
            {'id': 'parallel-b', 'type': 'function', 'function': {
                'name': 'read_files', 'arguments': '{"path":"b.py"}'}},
        ]})
        for call_id, path in (('parallel-a', 'a.py'), ('parallel-b', 'b.py')):
            messages.append({
                'role': 'tool', 'tool_call_id': call_id,
                'content': f'fresh {path}',
            })
            _round(
                task, 5, name='read_files', args=f'{{"path":"{path}"}}',
                content=f'fresh {path}',
            )
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=5, tid='abcdef12') is False

        for rn in range(6, 11):
            _single_tool_message_round(task, messages, rn)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False
        assert '_toolRoundTripNudges' not in task

    def test_safety_correction_preempts_efficiency_nudge_same_round(self):
        task, rs = _task(), _rs()
        messages = [{'role': 'user', 'content': 'inspect the repository'}]
        ranges = ((1, 100), (10, 20), (30, 40), (50, 60), (70, 80),
                  (90, 95))
        for rn, (start, end) in enumerate(ranges):
            args = (
                f'{{"path":"lib/x.py","start_line":{start},'
                f'"end_line":{end}}}'
            )
            _single_tool_message_round(
                task, messages, rn, name='read_files', args=args,
                content=f'lines {start}-{end}',
            )
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages, round_num=rn, tid='abcdef12',
                max_redundant_read_rounds=5) is False

        corrections = [
            message for message in messages if message.get('_isMeta') is True
        ]
        assert len(corrections) == 1
        assert 'NO SEMANTIC PROGRESS' in corrections[0]['content']
        assert '_toolRoundTripNudges' not in task

    @pytest.mark.parametrize(
        ('damaged_field', 'damaged_value'),
        (
            ('_tool_loop_guard', {
                'round_trip_efficiency_nudge_count': 'not-an-integer',
            }),
            ('_toolRoundTripNudges', {'not': 'a-list'}),
        ),
    )
    def test_damaged_state_cannot_break_efficiency_nudge(
            self, damaged_field, damaged_value):
        task, rs = _task(), _rs()
        task[damaged_field] = damaged_value
        messages = [{'role': 'user', 'content': 'inspect the repository'}]
        for rn in range(6):
            _single_tool_message_round(task, messages, rn)
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False

        assert task['_tool_loop_guard'][
            'round_trip_efficiency_nudge_count'] == 1
        assert len(task['_toolRoundTripNudges']) == 1

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

    def test_partial_v2_whole_read_does_not_claim_hidden_file_coverage(self):
        """Incident mtc6xp7kka0hls: hidden artifact bytes are not 'seen'."""
        task, rs, messages = _task(), _rs(), []
        partial = _v2_result(
            evidence='ev_partial_panel', artifact='tool-result:' + 'a' * 64,
            summary='File: panel.ts (1030 lines, 37.5 KB)\npartial prefix')
        _round(task, 0, name='read_files',
               args='{"path":"panel.ts"}', content=partial)
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=0, tid='abcdef12') is False
        assert task['_tool_loop_guard']['read_coverage'] == {}

        ranged = _v2_result(
            evidence='ev_panel_130_260',
            summary=('File: panel.ts (lines 130-260 of 1030)\n──\n'
                     'new ranged evidence'),
            status='ok', truncated=False)
        _round(task, 1, name='read_files',
               args=('{"path":"panel.ts","start_line":130,'
                     '"end_line":260}'), content=ranged)
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=1, tid='abcdef12') is False
        assert task['_tool_loop_guard']['read_coverage']['panel.ts'] == [
            [130, 260]]
        assert task['_tool_loop_guard']['redundant_read_streak'] == 0

    def test_sparse_partial_sidecar_preserves_hidden_evidence_semantics(self):
        from lib.tools.result_envelope import (
            ToolResultEnvelopeV2,
            split_tool_result_delivery,
        )

        task, rs = _task(), _rs()
        envelope = ToolResultEnvelopeV2(
            status='partial',
            summary='File: panel.ts (1030 lines, 37.5 KB)\npartial prefix',
            artifact_ref='tool-result:' + 'z' * 64,
            cursor='0', truncated=True, raw_bytes=50_000,
            evidence_id='ev_sparse_partial',
        ).with_visible_bytes().to_envelope_text()
        delivery = split_tool_result_delivery(envelope)
        _round(
            task, 0, name='read_files', args='{"path":"panel.ts"}',
            content=delivery.model_text,
            tool_result_evidence=dict(delivery.evidence or {}),
        )

        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=0, tid='abcdef12') is False
        state = task['_tool_loop_guard']
        assert state['read_coverage'] == {}
        assert 'ev_sparse_partial' in state['read_evidence_ids']

    def test_same_partial_v2_evidence_under_changed_ranges_is_redundant(self):
        task, rs = _task(), _rs()
        content = _v2_result(
            evidence='ev_same_hidden_file',
            artifact='tool-result:' + 'b' * 64)
        _round(task, 0, name='read_files', args='{"path":"panel.ts"}',
               content=content)
        _round(task, 1, name='read_files',
               args=('{"path":"panel.ts","start_line":130,'
                     '"end_line":400}'), content=content)
        for rn in range(2):
            assert handle_tool_loop_circuit_breaker(
                task, rs, round_num=rn, tid='abcdef12') is False
        assert task['_tool_loop_guard']['read_coverage'] == {}
        assert task['_tool_loop_guard']['redundant_read_streak'] == 1

    def test_truncated_file_projection_item_does_not_claim_requested_range(self):
        task, rs = _task(), _rs()
        content = _v2_result(
            evidence='ev_projection', artifact='tool-result:' + 'c' * 64,
            items=({
                'type': 'file_read/v1', 'index': 1, 'path': 'panel.ts',
                'status': 'ok', 'preview': (
                    'File: panel.ts (1030 lines, 37.5 KB)\npartial'),
                'previewTruncated': True,
            },))
        _round(task, 0, name='read_files', args='{"path":"panel.ts"}',
               content=content)
        assert handle_tool_loop_circuit_breaker(
            task, rs, round_num=0, tid='abcdef12') is False
        assert task['_tool_loop_guard']['read_coverage'] == {}

    def test_unconsumed_partial_artifact_nudges_then_stops_across_tools(self):
        """Incident mtcyxfbwqx03h0: paging/presentation knobs cannot evade."""
        task, rs, messages = _task(), _rs(), []
        fired = False
        variants = (
            (31, True, True, 30),
            (29, False, True, 1),
            (20, True, False, 2),
            (12, True, True, 3),
        )
        for rn, (before, raw, details, limit) in enumerate(variants):
            _round(
                task, rn, name='get_conversation',
                args=json.dumps({
                    'conversation_id': 'history', 'raw': raw,
                    'include_tool_details': details, 'before': before,
                    'limit': limit,
                }),
                content=_v2_result(
                    evidence=f'ev_page_{before}',
                    artifact='tool-result:' + str(rn) * 64,
                    summary='same settings prefix; requested page is hidden'),
            )
            fired = handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12')
            if rn < 3:
                assert fired is False
        assert fired is True
        assert len(messages) == 1
        assert 'read_tool_artifact' in messages[0]['content']
        assert rs.exit_reason == 'semantic_unresolved_artifact_loop'

    def test_distinct_visible_partial_pages_are_productive(self):
        """Paging through real new evidence must not trip the recovery guard."""
        task, rs, messages = _task(), _rs(), []
        for rn, before in enumerate((60, 50, 40, 30, 20, 10)):
            _round(
                task, rn, name='get_conversation',
                args=json.dumps({
                    'conversation_id': 'history', 'raw': True,
                    'before': before, 'limit': 10,
                }),
                content=_v2_result(
                    evidence=f'ev_page_{before}',
                    artifact='tool-result:' + str(rn) * 64,
                    summary=f'messages {before - 9}-{before}: SENTINEL_{before}'),
            )
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False
        assert messages == []
        pending = task['_tool_loop_guard']['pending_artifacts_by_scope']
        assert next(iter(pending.values()))['retryCount'] == 0

    @pytest.mark.parametrize(
        ('tool_name', 'base_args', 'window_variants'), (
            (
                'grep_search', {'pattern': 'needle', 'path': 'lib'},
                (
                    {'max_results': 50, 'context_lines': 0},
                    {'max_results': 10, 'context_lines': 3},
                    {'max_results': 5, 'context_lines': 1},
                    {'max_results': 20, 'context_lines': 5},
                ),
            ),
            (
                'read_skill_resource',
                {'skill_id': 'skill-a', 'resource': 'references/large.md'},
                (
                    {'cursor': 0, 'max_chars': 6000},
                    {'cursor': 6000, 'max_chars': 3000},
                    {'cursor': 9000, 'max_chars': 12000},
                    {'cursor': 21000, 'max_chars': 1000},
                ),
            ),
            (
                'search_memories', {'query': 'deployment decision'},
                (
                    {'top_k': 50}, {'top_k': 20},
                    {'top_k': 5}, {'top_k': 10},
                ),
            ),
        ),
    )
    def test_window_knobs_cannot_evade_unresolved_artifact_guard(
            self, tool_name, base_args, window_variants):
        task, rs, messages = _task(), _rs(), []
        fired = False
        for rn, window_args in enumerate(window_variants):
            args = dict(base_args)
            args.update(window_args)
            _round(
                task, rn, name=tool_name, args=json.dumps(args),
                content=_v2_result(
                    evidence=f'ev_{tool_name}_{rn}',
                    artifact='tool-result:' + str(rn) * 64),
            )
            fired = handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12')
            if rn < 3:
                assert fired is False
        assert fired is True
        assert len(messages) == 1
        assert rs.exit_reason == 'semantic_unresolved_artifact_loop'

    def test_artifact_continuation_resolves_source_retry_obligation(self):
        task, rs, messages = _task(), _rs(), []
        artifact = 'tool-result:' + 'd' * 64
        _round(
            task, 0, name='get_conversation',
            args='{"conversation_id":"history","raw":true,"before":30}',
            content=_v2_result(evidence='ev_page', artifact=artifact))
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=0, tid='abcdef12') is False

        _round(
            task, 1, name='read_tool_artifact',
            args=json.dumps({'artifact_ref': artifact, 'cursor': '0'}),
            content=_v2_result(
                evidence='ev_chunk', summary='recovered page content',
                status='ok', truncated=False))
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=1, tid='abcdef12') is False

        _round(
            task, 2, name='get_conversation',
            args='{"conversation_id":"history","raw":true,"before":20}',
            content=_v2_result(
                evidence='ev_next_page',
                artifact='tool-result:' + 'e' * 64))
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=2, tid='abcdef12') is False
        assert messages == []
        pending = task['_tool_loop_guard']['pending_artifacts_by_scope']
        assert len(pending) == 1
        assert next(iter(pending.values()))['retryCount'] == 0

    def test_parallel_partial_pages_count_as_one_prior_round_retry(self):
        task, rs, messages = _task(), _rs(), []
        _round(
            task, 0, name='get_conversation',
            args='{"conversation_id":"history","raw":true,"before":30}',
            content=_v2_result(
                evidence='ev_initial', artifact='tool-result:' + 'f' * 64))
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=0, tid='abcdef12') is False
        for before in (25, 20, 15):
            _round(
                task, 1, name='get_conversation',
                args=json.dumps({
                    'conversation_id': 'history', 'raw': True,
                    'before': before, 'limit': 1,
                }),
                content=_v2_result(
                    evidence=f'ev_{before}',
                    artifact='tool-result:' + str(before)[0] * 64))
        assert handle_tool_loop_circuit_breaker(
            task, rs, messages=messages,
            round_num=1, tid='abcdef12') is False
        assert messages == []
        pending = task['_tool_loop_guard']['pending_artifacts_by_scope']
        assert next(iter(pending.values()))['retryCount'] == 1

    def test_different_search_queries_have_independent_artifact_scopes(self):
        task, rs, messages = _task(), _rs(), []
        for rn, query in enumerate(('alpha', 'beta', 'gamma', 'delta')):
            _round(
                task, rn, name='web_search',
                args=json.dumps({'query': query, 'limit': rn + 1}),
                content=_v2_result(
                    evidence=f'ev_{query}',
                    artifact='tool-result:' + query[0] * 64))
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages,
                round_num=rn, tid='abcdef12') is False
        assert messages == []

    def test_receipt_bounds_override_requested_range(self):
        """Legacy/checkpointed receipts override stale requested bounds."""
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


def _stagnation_thresholds(**over):
    base = {
        'fail_nudge': 4, 'fail_grace': 2,
        'poll_nudge': 4, 'poll_grace': 2,
    }
    base.update(over)
    return base


def _shell_ok(task, llm_round, cmd='npm test'):
    # The round number in the output keeps exact-repetition digests distinct;
    # the poll detector keys on the command set, not the output.
    _round(task, llm_round, args=json.dumps({'command': cmd}),
           content=f'12 passed ({llm_round})\n[exit code: 0]')


def _shell_fail(task, llm_round, cmd='npm test',
                err='AssertionError: expected 1 got 2'):
    _round(task, llm_round, args=json.dumps({'command': cmd}),
           content=f'{err}\n[exit code: 1]')


def _edit(task, llm_round, path='src/x.py'):
    _round(task, llm_round, name='edit_file',
           args=json.dumps(
               {'path': path, 'old_string': 'a', 'new_string': 'b'}),
           content=f'updated {path}', results=[{'writeOk': True}])


class TestShellObservationClassifier:
    @pytest.mark.parametrize('command', [
        'grep -rn foo .',
        'cat a.txt | head -5',
        'ls -la src/ 2>/dev/null',
        'git status',
        'git -C repo log --oneline',
        'find . -name "*.py" | wc -l',
        'true',
        'pwd && ls',
    ])
    def test_read_only_commands(self, command):
        assert _shell_command_is_observation(command) is True

    @pytest.mark.parametrize('command', [
        'npm test',
        'git checkout main',
        'echo hi > f.txt',
        "grep 'a > b' f.txt",
        'sed -i s/a/b/ f.txt',
        'python -c "print(1)"',
        'ls > /tmp/listing.txt',
        'make build 2>&1 | tail -3',
    ])
    def test_potential_mutations(self, command):
        assert _shell_command_is_observation(command) is False


class TestFailureFingerprint:
    def test_timestamps_normalize_away(self):
        row_a = {'toolContent':
                 '2026-09-01T10:00:00 build failed: boom\n[exit code: 1]'}
        row_b = {'toolContent':
                 '2026-09-01T11:22:33 build failed: boom\n[exit code: 1]'}
        assert _failure_fingerprint(row_a) == _failure_fingerprint(row_b)

    def test_different_error_differs(self):
        row_a = {'toolContent': 'build failed: E1\n[exit code: 1]'}
        row_b = {'toolContent': 'build failed: E2\n[exit code: 1]'}
        assert _failure_fingerprint(row_a) != _failure_fingerprint(row_b)

    def test_different_exit_code_differs(self):
        row_a = {'toolContent': 'build failed: E1\n[exit code: 1]'}
        row_b = {'toolContent': 'build failed: E1\n[exit code: 2]'}
        assert _failure_fingerprint(row_a) != _failure_fingerprint(row_b)

    def test_different_command_differs_even_when_error_is_generic(self):
        row_a = {
            'toolName': 'run_command',
            'toolArgs': {'command': 'pytest tests/a.py'},
            'toolContent': 'failed\n[exit code: 1]',
        }
        row_b = {
            'toolName': 'run_command',
            'toolArgs': {'command': 'pytest tests/b.py'},
            'toolContent': 'failed\n[exit code: 1]',
        }
        assert _failure_fingerprint(row_a) != _failure_fingerprint(row_b)


class TestSuccessPollDetector:
    def test_nudges_then_clean_finishes(self):
        task, rs, messages = _task(), _rs(), []
        thresholds = _stagnation_thresholds(poll_nudge=3, poll_grace=2)
        with registered_chat_task(task):
            result = False
            for rn in range(5):
                _round(
                    task, rn,
                    args=json.dumps({'command': 'npm test'}),
                    content='12 passed\n[exit code: 0]',
                )
                result = handle_tool_loop_circuit_breaker(
                    task, rs, messages=messages, round_num=rn,
                    tid='abcdef12', stagnation_thresholds=thresholds,
                    max_consecutive_identical=99)
                if rn < 4:
                    assert result is False, rn
        assert result is True
        assert rs.exit_reason == 'success_poll_finish'
        # The registered runtime adoption adds an ``error: None`` key; the
        # production invariant is that no envelope was ever attached.
        assert not task.get('error')
        finish = task['_toolLoopCleanFinish']
        assert finish['reason'] == 'success_poll_finish'
        nudges = [
            message for message in messages
            if 'REPEATED IDENTICAL VERIFICATION'
            in str(message.get('content'))
        ]
        assert len(nudges) == 1
        assert nudges[0]['_isMeta'] is True
        audit = task['_toolLoopBreakerAudit']
        assert [row['action'] for row in audit] == ['nudge', 'finish']
        assert all(row['detector'] == 'success_poll' for row in audit)
        assert any(e.get('type') == 'round_end'
                   and e.get('reason') == 'success_poll'
                   for e in task['events'])

    def test_edit_resets_the_poll_streak(self):
        task, rs, messages = _task(), _rs(), []
        thresholds = _stagnation_thresholds(poll_nudge=3, poll_grace=1)
        _shell_ok(task, 0)
        _shell_ok(task, 1)
        _edit(task, 2)
        _shell_ok(task, 3)
        _shell_ok(task, 4)
        for rn in range(5):
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages, round_num=rn,
                tid='abcdef12', stagnation_thresholds=thresholds) is False
        assert '_toolLoopNudges' not in task

    def test_observation_commands_do_not_poll(self):
        """Read-only commands exiting 0 are inspection, not verification."""
        task, rs, messages = _task(), _rs(), []
        thresholds = _stagnation_thresholds(poll_nudge=2, poll_grace=1)
        for rn in range(5):
            _shell_ok(task, rn, cmd='cat build.log')
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages, round_num=rn,
                tid='abcdef12', stagnation_thresholds=thresholds) is False
        assert '_toolLoopNudges' not in task

    def test_same_command_with_advancing_output_is_progress(self):
        """Polling a changing external/build state must never be auto-finished."""
        task, rs, messages = _task(), _rs(), []
        thresholds = _stagnation_thresholds(poll_nudge=2, poll_grace=1)
        for rn in range(8):
            _shell_ok(task, rn, cmd='npm test')
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages, round_num=rn,
                tid='abcdef12', stagnation_thresholds=thresholds) is False
        assert '_toolLoopNudges' not in task
        assert '_toolLoopCleanFinish' not in task

    def test_isolated_caller_finishes_at_first_threshold(self):
        """No message lane -> the breaker ends the turn immediately."""
        task, rs = _task(), _rs()
        thresholds = _stagnation_thresholds(poll_nudge=2, poll_grace=5)
        with registered_chat_task(task):
            _round(task, 0, args=json.dumps({'command': 'npm test'}),
                   content='12 passed\n[exit code: 0]')
            assert handle_tool_loop_circuit_breaker(
                task, rs, round_num=0, tid='abcdef12',
                stagnation_thresholds=thresholds,
                max_consecutive_identical=99) is False
            _round(task, 1, args=json.dumps({'command': 'npm test'}),
                   content='12 passed\n[exit code: 0]')
            assert handle_tool_loop_circuit_breaker(
                task, rs, round_num=1, tid='abcdef12',
                stagnation_thresholds=thresholds,
                max_consecutive_identical=99) is True
        assert rs.exit_reason == 'success_poll_finish'
        # The registered runtime adoption adds an ``error: None`` key; the
        # production invariant is that no envelope was ever attached.
        assert not task.get('error')


class TestPersistentFailureDetector:
    def test_nudges_then_stops(self):
        task, rs, messages = _task(), _rs(), []
        thresholds = _stagnation_thresholds(fail_nudge=3, fail_grace=2)
        for rn in range(5):
            _shell_fail(
                task, rn,
                err=f'2026-09-01T10:00:0{rn} '
                    'AssertionError: expected 1 got 2')
        with registered_chat_task(task):
            results = [
                handle_tool_loop_circuit_breaker(
                    task, rs, messages=messages, round_num=rn,
                    tid='abcdef12', stagnation_thresholds=thresholds,
                    max_consecutive_identical=99)
                for rn in range(5)
            ]
        assert results == [False] * 4 + [True]
        assert task['error']['kind'] == 'tool_loop'
        assert rs.exit_reason == 'semantic_persistent_failure_loop'
        nudges = [
            message for message in messages
            if 'PERSISTENT IDENTICAL FAILURE' in str(message.get('content'))
        ]
        assert len(nudges) == 1
        audit = task['_toolLoopBreakerAudit']
        assert [row['action'] for row in audit] == ['nudge', 'stop']
        assert all(row['detector'] == 'persistent_identical_failure'
                   for row in audit)

    def test_changed_error_is_progress_and_resets(self):
        task, rs, messages = _task(), _rs(), []
        thresholds = _stagnation_thresholds(fail_nudge=3, fail_grace=1)
        for rn, err in enumerate((
                'AssertionError: expected 1 got 2',
                'AssertionError: expected 1 got 2',
                'TypeError: None has no attribute x',
                'TypeError: None has no attribute x')):
            _shell_fail(task, rn, err=err)
        for rn in range(4):
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages, round_num=rn,
                tid='abcdef12', stagnation_thresholds=thresholds,
                max_consecutive_identical=99) is False
        assert '_toolLoopNudges' not in task

    def test_confirmed_write_resets_identical_failure(self):
        task, rs, messages = _task(), _rs(), []
        thresholds = _stagnation_thresholds(fail_nudge=3, fail_grace=1)
        _shell_fail(task, 0)
        _shell_fail(task, 1)
        _edit(task, 2)
        _shell_fail(task, 3)
        _shell_fail(task, 4)
        for rn in range(5):
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages, round_num=rn,
                tid='abcdef12', stagnation_thresholds=thresholds,
                max_consecutive_identical=99) is False
        assert '_toolLoopNudges' not in task

    def test_success_resets_the_failure_tracker(self):
        task, rs, messages = _task(), _rs(), []
        thresholds = _stagnation_thresholds(fail_nudge=3, fail_grace=1)
        _shell_fail(task, 0)
        _edit(task, 1)
        _shell_fail(task, 2)
        _shell_ok(task, 3, cmd='npm run build')
        _shell_fail(task, 4)
        for rn in range(5):
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages, round_num=rn,
                tid='abcdef12', stagnation_thresholds=thresholds) is False
        assert '_toolLoopNudges' not in task


class TestReadOnlyExplorationIsProductive:
    def test_many_unique_read_rounds_never_nudge_or_stop(self):
        """Regression for mtjka09o7g8mit: novelty is progress, not mutation."""
        task, rs, messages = _task(), _rs(), []
        with registered_chat_task(task):
            results = []
            for rn in range(24):
                _round(task, rn, name='read_files',
                       args=json.dumps({'path': f'src/file_{rn}.py'}),
                       content=f'contents of file {rn}')
                results.append(handle_tool_loop_circuit_breaker(
                    task, rs, messages=messages, round_num=rn,
                    tid='abcdef12'))
        assert results == [False] * 24
        assert not task.get('error')
        assert '_toolLoopNudges' not in task
        assert '_toolLoopBreakerAudit' not in task


class TestStagnationEnvControls:
    def test_extended_kill_switch_disables_all_detectors(self, monkeypatch):
        monkeypatch.setenv('TOFU_LOOP_EXTENDED', '0')
        task, rs, messages = _task(), _rs(), []
        thresholds = _stagnation_thresholds(fail_nudge=2, fail_grace=1)
        for rn in range(6):
            _shell_fail(task, rn, err=(
                f'2026-09-01T10:00:0{rn} failure'))
            assert handle_tool_loop_circuit_breaker(
                task, rs, messages=messages, round_num=rn,
                tid='abcdef12', stagnation_thresholds=thresholds,
                max_consecutive_identical=99) is False
        assert '_toolLoopNudges' not in task
        assert '_toolLoopBreakerAudit' not in task

    def test_thresholds_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv('TOFU_LOOP_FAIL_NUDGE', '2')
        monkeypatch.setenv('TOFU_LOOP_FAIL_GRACE', '1')
        task, rs, messages = _task(), _rs(), []
        with registered_chat_task(task):
            results = []
            for rn in range(3):
                _shell_fail(task, rn, err=(
                    f'2026-09-01T10:00:0{rn} failure'))
                results.append(handle_tool_loop_circuit_breaker(
                    task, rs, messages=messages, round_num=rn,
                    tid='abcdef12', max_consecutive_identical=99))
        assert results == [False, False, True]


class TestCleanFinishSettlement:
    def test_background_acceptance_finishes_without_polling_round(self):
        task, rs = _task(), _rs()
        task['_backgroundTaskAccepted'] = {
            'tool': 'produce_slides',
            'taskId': 'slides_abc123',
            'message': 'PPT 已在后台开始生成。',
        }
        with registered_chat_task(task):
            assert finish_after_background_task_acceptance(
                task, rs, round_num=0, tid='abcdef12') is True

        assert rs.exit_reason == 'background_task_accepted'
        assert task['content'] == 'PPT 已在后台开始生成。'
        assert task['_toolLoopCleanFinish']['reason'] \
            == 'background_task_accepted'
        assert task['_toolLoopBreakerAudit'][-1]['detector'] \
            == 'background_task'
        assert any(event.get('reason') == 'background_task_accepted'
                   for event in task['events'])

    def test_clean_finish_flag_settles_to_stop(self):
        from lib.tasks_pkg.orchestrator._finalize import (
            _settle_post_loop_finish_reason)
        task = {
            'aborted': False,
            '_toolLoopCleanFinish': {'reason': 'success_poll_finish',
                                     'round': 5},
        }
        assert _settle_post_loop_finish_reason(
            task, 'tool_use', loop_exit_reason='success_poll_finish',
            abort_detected_phase=None, model='m', tid='t') == 'stop'
        assert 'error' not in task

    def test_dangling_tool_use_without_flag_still_errors(self):
        from lib.tasks_pkg.orchestrator._finalize import (
            _settle_post_loop_finish_reason)
        task = {'aborted': False}
        assert _settle_post_loop_finish_reason(
            task, 'tool_use', loop_exit_reason='length',
            abort_detected_phase=None, model='m', tid='t') == 'error'
        assert task['error']['kind'] == 'internal'
