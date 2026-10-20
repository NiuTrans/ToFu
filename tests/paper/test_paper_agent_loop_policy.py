"""Executable cost and terminal-honesty contract for Paper agent loops."""

from pathlib import Path

import pytest

from lib.agent_loop.agent_loop import AbortSignal
from lib.llm.llm_errors import AbortedError
from lib.paper.agent_loop_policy import (
    PaperAgentLoopHalted,
    run_guarded_paper_agent_loop,
)
from lib.paper.agent_usage import (
    PaperAgentBudgetExceeded,
    PaperAgentUsageMeter,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
PAPER_AGENT_CALLERS = (
    'lib/paper/qa_engine.py',
    'lib/paper/report_engine/worker.py',
    'lib/paper/deepen_engine.py',
    'lib/paper/insight_engine/_synthesize.py',
    'lib/paper/recommend_engine/_research.py',
    'lib/paper/survey.py',
    'lib/paper/ideate.py',
)


def test_identical_call_and_visible_result_loop_halts_after_fourth_execution():
    calls = {'dispatch': 0, 'execute': 0}

    def dispatch(round_index, _tools):
        calls['dispatch'] += 1
        return (
            {
                'role': 'assistant',
                'content': '',
                'tool_calls': [{
                    'id': f'call-{round_index}',
                    'function': {
                        'name': 'web_search',
                        'arguments': '{"query":"same"}',
                    },
                }],
            },
            'tool_calls',
            {},
        )

    def execute_tool(_round_index, _tool_call):
        calls['execute'] += 1
        return 'same model-visible result'

    with pytest.raises(PaperAgentLoopHalted) as raised:
        run_guarded_paper_agent_loop(
            context='test Paper agent',
            abort=AbortSignal.never(),
            round_tools=[{'type': 'function'}],
            dispatch=dispatch,
            execute_tool=execute_tool,
        )

    # The fourth occurrence executes before the detector can prove its visible
    # result is unchanged. This fail-open placement prevents legal polling
    # from being killed merely because the call arguments repeat.
    assert calls == {'dispatch': 4, 'execute': 4}
    assert raised.value.reason == 'no_progress'
    assert raised.value.no_progress_streak == 3


def test_completion_and_explicit_partial_abort_modes_remain_distinct():
    completed = run_guarded_paper_agent_loop(
        context='complete Paper agent',
        abort=AbortSignal.never(),
        round_tools=[],
        dispatch=lambda _round, _tools: (
            {'role': 'assistant', 'content': 'done'}, 'stop', {}),
        execute_tool=lambda _round, _call: None,
    )
    assert completed.completed is True

    aborted = AbortSignal.from_callback(lambda: True)
    with pytest.raises(AbortedError):
        run_guarded_paper_agent_loop(
            context='sync Paper agent',
            abort=aborted,
            round_tools=[],
            dispatch=lambda *_args: pytest.fail('aborted loop dispatched'),
            execute_tool=lambda *_args: None,
        )
    outcome = run_guarded_paper_agent_loop(
        context='background Paper agent',
        allow_aborted_outcome=True,
        abort=aborted,
        round_tools=[],
        dispatch=lambda *_args: pytest.fail('aborted loop dispatched'),
        execute_tool=lambda *_args: None,
    )
    assert outcome.aborted is True
    assert outcome.completed is False


def test_unique_tool_wandering_keeps_every_tool_and_its_answer():
    calls = {'dispatch': 0, 'execute': 0}
    admitted_tools = []
    meter = PaperAgentUsageMeter('test', token_budget=1_000_000, dispatch_budget=4)

    def dispatch(round_index, tools):
        calls['dispatch'] += 1
        admitted_tools.append(tools)
        if round_index == 3:
            return ({'role': 'assistant', 'content': 'bounded answer'},
                    'stop', None)
        return ({
            'role': 'assistant', 'content': '',
            'tool_calls': [{
                'id': f'unique-{round_index}',
                'function': {
                    'name': 'web_search',
                    'arguments': f'{{"query":"unique-{round_index}"}}',
                },
            }],
        }, 'tool_calls', None)

    outcome = run_guarded_paper_agent_loop(
        context='unique wandering Paper agent',
        usage_meter=meter,
        abort=AbortSignal.never(),
        round_tools=[{'type': 'function'}],
        dispatch=dispatch,
        execute_tool=lambda _rnd, _call: calls.__setitem__(
            'execute', calls['execute'] + 1),
    )

    assert outcome.completed is True
    assert calls == {'dispatch': 4, 'execute': 3}
    # Tools are never withdrawn to make a loop finish.
    assert admitted_tools == [[{'type': 'function'}]] * 4
    snapshot = meter.snapshot()
    assert snapshot['agent_dispatches'] == 4
    assert snapshot['forced_final'] is False


def test_spent_token_envelope_refuses_rather_than_stripping_tools():
    admitted_tools = []
    executed = []
    meter = PaperAgentUsageMeter('test', token_budget=100, dispatch_budget=8)

    def dispatch(round_index, tools):
        admitted_tools.append(tools)
        if round_index == 0:
            return ({
                'role': 'assistant', 'content': '',
                'tool_calls': [{
                    'id': 'first',
                    'function': {'name': 'fetch_url', 'arguments': '{"url":"u"}'},
                }],
            }, 'tool_calls', {'prompt_tokens': 95, 'completion_tokens': 8})
        return ({'role': 'assistant', 'content': 'answer'}, 'stop',
                {'prompt_tokens': 20, 'completion_tokens': 5})

    with pytest.raises(PaperAgentBudgetExceeded) as raised:
        run_guarded_paper_agent_loop(
            context='token-bounded Paper agent',
            usage_meter=meter,
            abort=AbortSignal.never(),
            round_tools=['tool'],
            dispatch=dispatch,
            execute_tool=lambda _rnd, call: executed.append(call['id']),
        )

    assert raised.value.reason == 'token_budget'
    # The first round ran with its tools; the refused round never dispatched.
    assert admitted_tools == [['tool']]
    assert executed == ['first']
    assert meter.snapshot()['forced_final'] is False


def test_spent_dispatch_envelope_refuses_before_the_paid_request():
    calls = {'dispatch': 0, 'execute': 0}
    meter = PaperAgentUsageMeter('test', token_budget=1_000_000, dispatch_budget=2)

    def dispatch(round_index, _tools):
        calls['dispatch'] += 1
        return ({
            'role': 'assistant', 'content': '',
            'tool_calls': [{
                'id': f'call-{round_index}',
                'function': {
                    'name': 'web_search',
                    'arguments': f'{{"query":"q-{round_index}"}}',
                },
            }],
        }, 'tool_calls', None)

    with pytest.raises(PaperAgentBudgetExceeded) as raised:
        run_guarded_paper_agent_loop(
            context='dispatch-bounded Paper agent',
            usage_meter=meter,
            abort=AbortSignal.never(),
            round_tools=['tool'],
            dispatch=dispatch,
            execute_tool=lambda _rnd, _call: calls.__setitem__(
                'execute', calls['execute'] + 1),
        )

    assert calls == {'dispatch': 2, 'execute': 2}
    assert raised.value.reason == 'dispatch_budget'
    assert meter.snapshot()['agent_dispatches'] == 3


def test_every_stage_defaults_to_no_declared_envelope():
    for stage in ('report', 'qa', 'deepen', 'insight', 'recommend',
                  'survey', 'ideate', 'research-action'):
        snapshot = PaperAgentUsageMeter.for_stage(
            stage, fallback_model='m').snapshot()
        assert snapshot['agent_token_budget'] == 0, stage
        assert snapshot['agent_dispatch_budget'] == 0, stage
        assert snapshot['forced_final'] is False, stage


def test_every_agentic_paper_owner_uses_the_guarded_chassis():
    for relative_path in PAPER_AGENT_CALLERS:
        source = (ROOT / relative_path).read_text(encoding='utf-8')
        assert 'run_guarded_paper_agent_loop(' in source, relative_path
        assert 'run_agent_loop(' not in source, relative_path
        assert 'usage_meter=' in source, relative_path
        assert '.allowed_tools(' not in source, relative_path
        # Accounting happens once, in the chassis. A workflow that accounts its
        # own extra paid attempt would be running a second provider dispatch
        # outside the loop's authority, which no Paper stage does.
        assert '.observe_agent_round(' not in source, relative_path
