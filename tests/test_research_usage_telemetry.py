"""Auto-research accounts every call and converges adaptively."""

import pytest

pytestmark = pytest.mark.unit


def test_meter_accumulates_vendor_shapes_and_uses_dispatch_pricing(monkeypatch):
    import lib.research.telemetry as tm

    priced = []

    def fake_cost(usage, model_id='', provider_id=None):
        priced.append((model_id, provider_id))
        return {'costUsd': 0.01, 'costCny': 0.07,
                'pricingSource': 'resolved_price'}

    monkeypatch.setattr(tm, 'compute_cost', fake_cost)
    meter = tm.ResearchUsageMeter('survey')
    meter.record({'prompt_tokens': 100, 'completion_tokens': 10,
                  'cache_read_tokens': 40,
                  '_dispatch': {'model': 'm-openai', 'provider_id': 'p1'}})
    meter.record({'input_tokens': 20, 'output_tokens': 5,
                  'cache_read_input_tokens': 80,
                  '_dispatch': {'model': 'm-anthropic', 'provider_id': 'p2'}})

    got = meter.snapshot()
    assert got['calls'] == 2 and got['priced_calls'] == 2
    assert got['prompt_tokens'] == 120 and got['completion_tokens'] == 15
    assert got['cache_read_tokens'] == 120
    # OpenAI: prompt already includes cache (100); Anthropic: residual+cache
    # (20+80). Add output once on both calls.
    assert got['total_tokens'] == 215
    assert got['agent_tokens'] == 0  # direct finite calls are not loop pressure
    assert priced == [('m-openai', 'p1'), ('m-anthropic', 'p2')]
    assert got['cost_cny'] == 0.14


def test_token_envelope_forces_next_dispatch_to_finalize(monkeypatch):
    import lib.research.telemetry as tm

    monkeypatch.setattr(tm, 'compute_cost', lambda *a, **k: None)
    meter = tm.ResearchUsageMeter('ideate', token_budget=100)
    tools = [{'type': 'function'}]
    message = {'tool_calls': [{'function': {'name': 'web_search',
                                             'arguments': '{"q":"x"}'}}]}
    meter.observe_agent_round({'prompt_tokens': 95, 'completion_tokens': 8}, message)

    assert meter.allowed_tools(tools) is None
    got = meter.snapshot()
    assert got['forced_final'] is True
    assert got['forced_final_reason'] == 'token_budget'
    assert got['agent_token_budget'] == 100


def test_repeated_tool_calls_converge_without_a_global_round_ceiling(monkeypatch):
    import lib.research.telemetry as tm

    monkeypatch.setattr(tm, 'compute_cost', lambda *a, **k: None)
    meter = tm.ResearchUsageMeter('survey', repeat_limit=1)
    message = {'tool_calls': [{'function': {'name': 'fetch_url',
                                             'arguments': '{"url":"u"}'}}]}
    meter.observe_agent_round({'prompt_tokens': 1}, message)
    assert meter.allowed_tools(['tool']) == ['tool']
    meter.observe_agent_round({'prompt_tokens': 1}, message)
    assert meter.allowed_tools(['tool']) is None
    assert meter.snapshot()['forced_final_reason'] == 'repeated_tool_calls'


def test_stage_snapshots_fold_into_one_run_total():
    from lib.research.telemetry import aggregate_research_usage

    got = aggregate_research_usage(
        {'calls': 2, 'prompt_tokens': 100, 'completion_tokens': 10,
         'cost_cny': 0.2, 'models': ['m1']},
        {'calls': 4, 'prompt_tokens': 80, 'completion_tokens': 30,
         'cost_cny': 0.4, 'models': ['m1', 'm2'], 'forced_final': True})
    assert got['total']['calls'] == 6
    assert got['total']['prompt_tokens'] == 180
    assert got['total']['completion_tokens'] == 40
    assert got['total']['cost_cny'] == 0.6
    assert got['total']['models'] == ['m1', 'm2']
    assert got['total']['forced_final'] is True


def test_llm_evaluation_usage_is_part_of_the_run_total():
    from lib.research.telemetry import aggregate_research_usage

    got = aggregate_research_usage(
        {'calls': 1, 'total_tokens': 100, 'cost_cny': 0.1},
        {'calls': 2, 'total_tokens': 200, 'cost_cny': 0.2},
        {'calls': 3, 'total_tokens': 300, 'cost_cny': 0.3,
         'models': ['judge']})
    assert got['total']['calls'] == 6
    assert got['total']['total_tokens'] == 600
    assert got['total']['cost_cny'] == 0.6
    assert got['stages']['evaluate']['calls'] == 3
    assert got['total']['models'] == ['judge']


def test_usage_survives_task_ttl_via_research_artifact_store(
        tmp_path, monkeypatch):
    from lib.database import reset_sqlite_for_tests, restore_db_state
    import lib.research.persistence as persistence
    from lib.research.persistence import (load_research_artifacts,
                                          persist_ideate, persist_survey)
    from lib.storage import StorageSupervisor

    snapshot = reset_sqlite_for_tests(str(tmp_path / 'usage.db'))
    supervisor = StorageSupervisor(
        project_root=tmp_path / 'sidecar', backend='sqlite', startup_timeout=20)
    supervisor.start()
    monkeypatch.setattr(
        persistence, '_storage', lambda **_kwargs: supervisor.client)
    try:
        persist_survey('direction', 'en', '# survey', {'open_gaps': []},
                       usage={'calls': 2, 'prompt_tokens': 100,
                              'completion_tokens': 10, 'cost_cny': 0.2})
        persist_ideate('direction', 'en', {
            'accepted': [], 'rejected': [{'reject_stage': 'rubric'}],
            'threshold': 4.0, 'gate_reached': 'rubric',
            'usage': {'calls': 3, 'prompt_tokens': 80,
                      'completion_tokens': 20, 'cost_cny': 0.3}})
        got = load_research_artifacts('direction', 'en')['usage']
        assert got['total']['calls'] == 5
        assert got['total']['prompt_tokens'] == 180
        assert got['total']['cost_cny'] == 0.5
        assert got['stages']['survey']['calls'] == 2
        assert got['stages']['ideate']['calls'] == 3
    finally:
        supervisor.stop()
        restore_db_state(snapshot)
