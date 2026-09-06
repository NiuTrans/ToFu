"""Executable cost and terminal-honesty contract for Paper agent loops."""

from pathlib import Path

import pytest

from lib.agent_loop import AbortSignal
from lib.llm_errors import AbortedError
from lib.paper.agent_loop_policy import (
    PaperAgentLoopHalted,
    run_guarded_paper_agent_loop,
)
from lib.paper.agent_usage import (
    PAPER_AGENT_BUDGET_SPECS,
    PAPER_AGENT_DISPATCH_BUDGET_HARD_MAX,
    PAPER_AGENT_TOKEN_BUDGET_HARD_MAX,
    PaperAgentUsageMeter,
    paper_agent_dispatch_budget,
    paper_agent_token_budget,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
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


def test_unique_tool_wandering_reserves_last_dispatch_for_final_answer():
    calls = {'dispatch': 0, 'execute': 0}
    admitted_tools = []
    meter = PaperAgentUsageMeter(
        'test', token_budget=1_000_000, dispatch_budget=4, repeat_limit=0)

    def dispatch(round_index, tools):
        calls['dispatch'] += 1
        admitted_tools.append(tools)
        if tools is None:
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
    assert admitted_tools[:3] == [[{'type': 'function'}]] * 3
    assert admitted_tools[3] is None
    snapshot = meter.snapshot()
    assert snapshot['agent_dispatches'] == 4
    assert snapshot['unmetered_calls'] == 4
    assert snapshot['forced_final_reason'] == 'dispatch_budget'


def test_token_envelope_forces_next_round_to_synthesize_without_tools():
    admitted_tools = []
    executed = []
    meter = PaperAgentUsageMeter(
        'test', token_budget=100, dispatch_budget=8, repeat_limit=0)

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

    outcome = run_guarded_paper_agent_loop(
        context='token-bounded Paper agent',
        usage_meter=meter,
        abort=AbortSignal.never(),
        round_tools=['tool'],
        dispatch=dispatch,
        execute_tool=lambda _rnd, call: executed.append(call['id']),
    )

    assert outcome.completed is True
    assert admitted_tools == [['tool'], None]
    assert executed == ['first']
    assert meter.snapshot()['forced_final_reason'] == 'token_budget'


def test_provider_cannot_execute_tools_after_budget_authority_is_removed():
    calls = {'dispatch': 0, 'execute': 0}
    admitted_tools = []
    meter = PaperAgentUsageMeter(
        'test', token_budget=1_000_000, dispatch_budget=2, repeat_limit=0)

    def dispatch(round_index, tools):
        calls['dispatch'] += 1
        admitted_tools.append(tools)
        return ({
            'role': 'assistant', 'content': '',
            'tool_calls': [{
                'id': f'ignored-{round_index}',
                'function': {
                    'name': 'web_search',
                    'arguments': f'{{"query":"q-{round_index}"}}',
                },
            }],
        }, 'tool_calls', None)

    with pytest.raises(PaperAgentLoopHalted) as raised:
        run_guarded_paper_agent_loop(
            context='authority-ignoring Paper agent',
            usage_meter=meter,
            abort=AbortSignal.never(),
            round_tools=['tool'],
            dispatch=dispatch,
            execute_tool=lambda _rnd, _call: calls.__setitem__(
                'execute', calls['execute'] + 1),
        )

    assert admitted_tools == [['tool'], None]
    assert calls == {'dispatch': 2, 'execute': 1}
    assert raised.value.reason == 'agent_budget_ignored'
    assert meter.snapshot()['budget_ignored'] is True


def test_budget_overrides_cannot_disable_or_escape_hard_envelopes():
    assert paper_agent_token_budget(
        'qa', {'TOFU_PAPER_QA_AGENT_TOKEN_BUDGET': '0'}) == 240_000
    assert paper_agent_token_budget(
        'qa', {'TOFU_PAPER_QA_AGENT_TOKEN_BUDGET': '999999999'}) \
        == PAPER_AGENT_TOKEN_BUDGET_HARD_MAX
    assert paper_agent_dispatch_budget(
        'qa', {'TOFU_PAPER_QA_AGENT_DISPATCH_BUDGET': '0'}) == 8
    assert paper_agent_dispatch_budget(
        'qa', {'TOFU_PAPER_QA_AGENT_DISPATCH_BUDGET': '999999'}) \
        == PAPER_AGENT_DISPATCH_BUDGET_HARD_MAX


def test_all_seven_stage_defaults_are_finite_and_environment_names_are_unique():
    expected = {
        'report': (480_000, 10),
        'qa': (240_000, 8),
        'deepen': (320_000, 8),
        'insight': (240_000, 8),
        'recommend': (160_000, 8),
        'survey': (240_000, 10),
        'ideate': (160_000, 10),
    }
    assert {
        stage: (spec.token_default, spec.dispatch_default)
        for stage, spec in PAPER_AGENT_BUDGET_SPECS.items()
    } == expected
    env_names = [
        name
        for spec in PAPER_AGENT_BUDGET_SPECS.values()
        for name in (spec.token_env, spec.dispatch_env)
    ]
    assert len(env_names) == len(set(env_names)) == 14


def test_every_agentic_paper_owner_uses_the_guarded_chassis():
    for relative_path in PAPER_AGENT_CALLERS:
        source = (ROOT / relative_path).read_text(encoding='utf-8')
        assert 'run_guarded_paper_agent_loop(' in source, relative_path
        assert 'run_agent_loop(' not in source, relative_path
        assert 'usage_meter=' in source, relative_path
        assert '.allowed_tools(' not in source, relative_path
        assert '.observe_agent_round(' not in source, relative_path
