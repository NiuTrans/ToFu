"""Background launch receipts must not trigger a duplicate-execution nudge."""

import pytest

from lib.tasks_pkg.stream_handler._intent_stall import should_nudge_intent_stall

pytestmark = pytest.mark.unit


def _round(tool='run_command', **result_overrides):
    return {
        'roundNum': 1,
        'llmRound': 1,
        'toolName': tool,
        'status': 'done',
        'results': [{
            'exitCode': 'background',
            'backgrounded': True,
            'timedOut': False,
            'badge': 'background',
            **result_overrides,
        }],
    }


def _decision(*rounds):
    return should_nudge_intent_stall(
        {'toolRounds': list(rounds)},
        {'role': 'assistant', 'tool_calls': []},
        'The command is running in the background.',
    )


@pytest.mark.parametrize('tool', ['run_command', 'code_exec'])
def test_background_launch_does_not_nudge_duplicate_execution(tool):
    assert _decision(_round(tool)) == (False, 'prev_tool_ok')


def test_background_exit_sentinel_without_optional_flag_is_not_failure():
    entry = _round()
    entry['results'][0].pop('backgrounded')
    assert _decision(entry) == (False, 'prev_tool_ok')


@pytest.mark.parametrize('failure', [
    {'exitCode': 1},
    {'exitCode': '2'},
    {'exitCode': 'timeout'},
    {'exitCode': 'not-run'},
    {'notRun': True},
    {'badge': 'error'},
    {'type': 'error'},
])
def test_background_flag_does_not_hide_structural_failure(failure):
    assert _decision(_round(**failure)) == (True, 'intent_stall')


def test_background_receipt_does_not_hide_failed_round_status():
    entry = _round()
    entry['status'] = 'failed'
    assert _decision(entry) == (True, 'intent_stall')


@pytest.mark.parametrize('background_last', [False, True])
def test_background_sibling_does_not_hide_failure_in_same_batch(background_last):
    failed = _round(exitCode=1, backgrounded=False, badge='exit 1')
    background = _round('code_exec')
    rounds = [failed, background] if background_last else [background, failed]
    for round_num, entry in enumerate(rounds, 1):
        entry['roundNum'] = round_num
    assert _decision(*rounds) == (True, 'intent_stall')


def test_background_result_does_not_hide_failure_in_same_round():
    entry = _round()
    entry['results'].append({'exitCode': 1})
    assert _decision(entry) == (True, 'intent_stall')
