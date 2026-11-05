"""Optional L2 defers on unverified main-route pricing before diff/route work.

Without verified main-route rates for the current prompt, every candidate's
payback is infinite. An optional (proactive) attempt therefore defers without
building the turn diff or selecting a summary route. Parity with the full path:
the same verdict, return message, retry floor/witness, result metadata and
decline log line. ``route_reason`` names why no route was consulted, and the
diagnostic ``plannedPromptTokens`` reserves no turn-diff text for a
continuation that is never built. Priced attempts and mandatory
(forced/hard-window) attempts keep the full path.
"""
from __future__ import annotations

import copy
import math
from types import SimpleNamespace

import pytest

import lib.tasks_pkg.commit_round._turn_diff as turn_diff
import lib.tasks_pkg.compaction._layer2._compact as layer2
from lib.tasks_pkg.compaction import _summary_routing
from lib.tasks_pkg.compaction._cache_economics import (
    _proactive_cache_economics, main_route_pricing_verified,
)

pytestmark = pytest.mark.unit

_DEFERRED = ('Context compaction deferred — verified pricing evidence is '
             'incomplete for the input or summary route. Messages preserved as-is.')
_UNREPAID = ('Context compaction skipped — foldable history cannot repay the '
             'warm-cache rewrite. Messages preserved as-is.')
_RESOURCE_ROUTE = {'reason': 'authorized_auxiliary_resource_policy',
                   'modelId': 'luna', 'providerId': 'oauth-luna',
                   'requestOwned': True, 'projectedCostUsd': None}


def _messages():
    return [
        {'role': 'system', 'content': 'you are a coding assistant'},
        {'role': 'user', 'content': 'Objective: finish the implementation.'},
        {'role': 'assistant', 'content': 'old state ' + ('x' * 20_000)},
        {'role': 'user', 'content': 'continue'},
        {'role': 'assistant', 'content': 'current state'},
    ]


# Neither a provider rate card nor a maintained model price: pricing stays
# unverified. A provider without its own card still verifies through the
# model's maintained price (owner decision 2026-11-02).
_UNPRICED_MODEL = 'unpriced-test-model'


def _task(provider_id, *, warm=0, model='kimi-k3'):
    task = {'convId': f'pricing-{provider_id}', 'id': 't',
            'provider_id': provider_id, 'config': {'model': model}}
    if warm:
        task['_compactionCacheObservation'] = {
            'taskId': 't', 'model': model, 'providerId': provider_id,
            'cacheReadTokens': warm}
    return task


@pytest.fixture
def priced_provider(monkeypatch):
    from lib.pricing import _provider
    monkeypatch.setitem(_provider.PROVIDER_PRICING, 'economics-test', {
        'kimi-k3': {'input': 1.0, 'output': 1.0, 'currency': 'USD',
                    'cacheWriteMul': 1.0, 'cacheReadMul': 1.0}})


@pytest.fixture
def spies(monkeypatch):
    seen = SimpleNamespace(diffs=0, routes=[], summaries=[], probes=[], lines=[],
                           route=dict(_RESOURCE_ROUTE))

    def diff(*_args, **_kwargs):
        seen.diffs += 1
        return '### Files Modified This Turn (net diff)\n1 file(s) changed this turn: +1/−1'

    def select(*_args, **kwargs):
        seen.routes.append(kwargs)
        return dict(seen.route)

    def summary(*_args, **kwargs):
        seen.summaries.append(kwargs)
        kwargs['usage_out'].update({
            'prompt_tokens': 800, 'completion_tokens': 200, 'finish_reason': 'stop',
            '_dispatch': {'model': 'kimi-k3', 'provider_id': 'economics-test'}})
        return 'Objective preserved; continue the current implementation.'

    monkeypatch.setattr(turn_diff, 'build_turn_diff_block', diff)
    monkeypatch.setattr(_summary_routing, 'select_summary_route', select)
    monkeypatch.setattr(layer2, '_generate_query_aware_summary', summary)
    monkeypatch.setattr(layer2, '_archive_transcript', lambda *a, **k: None)
    monkeypatch.setattr(layer2.logger, 'info',
                        lambda message, *args: seen.lines.append(message % args))
    observe_probe(monkeypatch, seen, main_route_pricing_verified)
    return seen


def observe_probe(monkeypatch, seen, probe):
    def observed(task, *, prompt_tokens):
        verdict = probe(task, prompt_tokens=prompt_tokens)
        seen.probes.append(verdict)
        return verdict
    monkeypatch.setattr(layer2, '_main_route_pricing_verified', observed)


def _attempt(task, messages, **overrides):
    options = {'preserve_budget_tokens': 1, '_compaction_trigger': 'working_set',
               '_compaction_round': 7, '_proactive_economic': True,
               '_compaction_skip_archive': True, **overrides}
    meta = {}
    result = layer2.execute_compact_tool(messages, task=task, _result_meta=meta, **options)
    return result, meta


def _decline_lines(seen):
    return [line for line in seen.lines if 'Proactive uneconomic decline' in line]


@pytest.mark.parametrize('warm', [0, 100_000], ids=['cold-cache', 'warm-cache'])
def test_unverified_pricing_defers_without_route_selection_at_full_path_parity(
        monkeypatch, spies, warm):
    def attempt(probe):
        observe_probe(monkeypatch, spies, probe)
        spies.diffs = 0
        spies.routes.clear()
        spies.lines.clear()
        task, messages = _task('unpriced-test', warm=warm, model=_UNPRICED_MODEL), _messages()
        original = copy.deepcopy(messages)
        result, meta = _attempt(task, messages)
        assert messages == original
        assert spies.summaries == []
        return SimpleNamespace(
            result=result, meta=meta, lines=_decline_lines(spies),
            diffs=spies.diffs, routes=len(spies.routes),
            floor=task.get('_autoCompactRetryAfterTokens'),
            witness=task.get('_autoCompactRetryWitness'))

    early = attempt(main_route_pricing_verified)
    # Forcing the probe open replays the pre-hoist path: turn diff and route,
    # then the same economics rejects the unpriced input.
    full = attempt(lambda *_a, **_k: True)

    assert (early.diffs, early.routes) == (0, 0)
    assert (full.diffs, full.routes) == (1, 1)
    assert early.result == full.result == _DEFERRED
    assert early.meta.pop('plannedPromptTokens') < full.meta.pop('plannedPromptTokens')
    assert early.meta == full.meta
    assert early.meta['reason'] == 'cache_negative'
    assert early.meta['economics']['pricing_known'] is False
    assert early.meta['economics']['payback_rounds'] == math.inf
    assert early.floor == full.floor > early.meta['tokens_before']
    assert early.witness == full.witness
    assert (early.witness or {}).get('warmPrefixTokens') == (warm or None)
    assert len(early.lines) == len(full.lines) == 1
    assert 'route_reason=main_route_pricing_unverified' in early.lines[0]
    assert 'route_reason=authorized_auxiliary_resource_policy' in full.lines[0]
    assert (early.lines[0].replace('main_route_pricing_unverified', '<route>')
            == full.lines[0].replace('authorized_auxiliary_resource_policy', '<route>'))


@pytest.mark.parametrize(('provider_id', 'model', 'verified'), [
    ('economics-test', 'kimi-k3', True), ('unpriced-test', 'kimi-k3', True),
    ('unpriced-test', _UNPRICED_MODEL, False), ('', 'kimi-k3', True)])
def test_pricing_probe_predicts_economics_for_every_candidate(
        priced_provider, provider_id, model, verified):
    """The early verdict is sound only if it agrees with the economics owner."""
    task = _task(provider_id, warm=50_000, model=model) if provider_id else {
        'id': 't', 'config': {'model': model}}
    prompt_tokens = 120_000
    assert main_route_pricing_verified(task, prompt_tokens=prompt_tokens) is verified
    for candidate_tokens in (0, 1, 30_000, prompt_tokens):
        economics = _proactive_cache_economics(
            task, tokens_before=prompt_tokens, candidate_tokens=candidate_tokens,
            summary_cost={'costUsd': 0.0})
        assert economics['pricing_known'] is verified
        if not verified:
            assert economics['payback_rounds'] == math.inf


def test_unverified_pricing_defers_where_turn_diff_would_exhaust_the_plan(
        monkeypatch, spies):
    """The skipped diff only sizes a continuation the deferral never builds.

    Here the diff alone overflows the dispatch target, so the full path halves
    its tail to an irreducible floor and refuses; the early path plans without
    it and defers. Neither buys a summary nor changes the source history.
    """
    def diff(*_args, **_kwargs):
        spies.diffs += 1
        return 'DIFF-MARKER'

    def measure(candidate):
        return 10_000 if any('DIFF-MARKER' in str(row.get('content')) for row in candidate) else 100

    monkeypatch.setattr(turn_diff, 'build_turn_diff_block', diff)
    outcomes = {}
    for name, probe in (('early', main_route_pricing_verified),
                        ('full', lambda *_a, **_k: True)):
        observe_probe(monkeypatch, spies, probe)
        messages = _messages()
        original = copy.deepcopy(messages)
        result, meta = _attempt(_task('unpriced-test', model=_UNPRICED_MODEL), messages,
                                _dispatch_prompt_measure=measure,
                                _dispatch_prompt_target=5_000)
        assert messages == original
        outcomes[name] = (result, meta.get('reason'), meta.get('summaryFailureReason'))

    assert spies.diffs == 1 and spies.summaries == []
    assert outcomes['early'] == (_DEFERRED, 'cache_negative', None)
    assert outcomes['full'][1:] == (None, 'irreducible_prompt_floor')


def test_priced_uneconomic_attempt_keeps_route_selection(priced_provider, spies):
    spies.route = {**_RESOURCE_ROUTE, 'projectedCostUsd': 0.012}
    task = _task('economics-test', warm=100_000)
    result, meta = _attempt(task, _messages())

    assert spies.probes == [True]
    assert spies.diffs == 1
    assert len(spies.routes) == 1 and spies.summaries == []
    assert result == _UNREPAID
    assert meta['reason'] == 'cache_negative'
    assert meta['economics']['pricing_known'] is True
    assert meta['economics']['summary_cost_known'] is True
    assert 'route_reason=authorized_auxiliary_resource_policy' in _decline_lines(spies)[0]


def test_priced_input_with_unpriced_summary_route_still_selects_route(
        priced_provider, spies):
    """Only the selected route can reveal unknown summary cost; keep it."""
    task = _task('economics-test', warm=100_000)
    result, meta = _attempt(task, _messages())

    assert spies.probes == [True]
    assert spies.diffs == 1
    assert len(spies.routes) == 1 and spies.summaries == []
    assert result == _DEFERRED
    assert meta['economics']['pricing_known'] is True
    assert meta['economics']['summary_cost_known'] is False
    assert 'route_reason=authorized_auxiliary_resource_policy' in _decline_lines(spies)[0]


def test_priced_economic_attempt_still_compacts(monkeypatch, priced_provider, spies):
    spies.route = {**_RESOURCE_ROUTE, 'projectedCostUsd': 0.000001}
    monkeypatch.setattr(layer2, '_projected_summary_usage_tokens', lambda *a, **k: 0)
    task = _task('economics-test', warm=100_000)
    result, meta = _attempt(task, _messages())

    assert spies.probes == [True]
    assert spies.diffs == 1
    assert len(spies.routes) == 1
    assert [call['expected_route'] for call in spies.summaries] == [spies.route]
    assert meta['compacted'] is True, result
    assert _decline_lines(spies) == []


@pytest.mark.parametrize('mode', ['forced', 'hard_window'])
def test_mandatory_compaction_with_unverified_pricing_is_never_deferred(spies, mode):
    """Forced and over-window attempts summarize; unknown money gates only optional work."""
    task = _task('unpriced-test', model=_UNPRICED_MODEL)
    messages = _messages()
    overrides = ({'_proactive_economic': False} if mode == 'forced'
                 else {'_message_tokens_before': 10_000_000})
    result, meta = _attempt(task, messages, **overrides)

    assert meta['compacted'] is True, result
    assert len(spies.summaries) == 1
    assert spies.diffs == 1
    assert spies.probes == [] and spies.routes == []
    assert _decline_lines(spies) == []
