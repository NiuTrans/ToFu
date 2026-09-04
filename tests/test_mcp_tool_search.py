"""Pre-request MCP catalog search, cache, sticky state, and workflows."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import lib.mcp.tool_search as tool_search
from lib.mcp.client._bridge import MCPBridge, _MCPServerHandle
from lib.mcp.tool_search import (
    build_catalog_index,
    canonical_schema_hash,
    freeze_wire_definitions,
    frozen_wire_tool_names,
    invalidate_server_catalog,
    mcp_selection_scope_id,
    recent_conversation_mcp_tool_names,
    record_mcp_tool_used,
    select_active_mcp_tools,
)


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_tool_search_state():
    tool_search.clear_tool_search_caches()
    yield
    tool_search.clear_tool_search_caches()


def _definition(server, name, description=''):
    return {
        'type': 'function',
        'function': {
            'name': f'mcp__{server}__{name}',
            'description': f'[MCP:{server}] {description or name}',
            'parameters': {'type': 'object', 'properties': {}},
        },
    }


def _row(server, name, *, description='', meta=None, version='v1'):
    definition = _definition(server, name, description)
    return {
        'server_id': server, 'tool_name': name,
        'namespaced_name': f'mcp__{server}__{name}',
        'openai_def': definition, 'meta': meta or {},
        'schema_hash': canonical_schema_hash(
            definition['function']['parameters']),
        'catalog_version': version,
    }


def _names(definitions):
    return [tool['function']['name'] for tool in definitions]


def _large_snapshot():
    rows = [
        _row('xuecheng', 'prepare_doc_edit', description='prepare document edit',
             meta={'bundle': 'edit_document', 'intents': ['编辑学城文档'],
                   'aliases': ['学城', 'Xuecheng'], 'risk': 'read'}),
        _row('xuecheng', 'update_doc', description='update edit document',
             meta={'bundle': 'edit_document',
                   'requires': ['prepare_doc_edit'],
                   'intents': ['编辑学城文档'], 'risk': 'write'}),
        _row('hope', 'search', description='search hope courses',
             meta={'aliases': ['hope'], 'risk': 'read'}),
        _row('hope', 'login', description='login hope',
             meta={'risk': 'write'}),
    ]
    rows.extend(_row('hope', f'tool_{i}', description=f'utility {i}',
                     meta={'risk': 'read'}) for i in range(8))
    return rows


def _confirmation_snapshot():
    """Model the catalog order involved in mtbebkmkanwpp4."""
    rows = [
        _row('xuecheng', 'read_doc', description='read xuecheng document',
             meta={'risk': 'read'}),
        _row('xuecheng', 'list_comments',
             description='list xuecheng document comments',
             meta={'risk': 'read'}),
        _row('xuecheng', 'login', description='authorize xuecheng access',
             meta={'risk': 'auth'}),
        _row('12306-train', 'get-current-date'),
        _row('12306-train', 'get-interline-tickets'),
        _row('12306-train', 'get-station-by-telecode'),
        _row('12306-train', 'get-station-code-by-names'),
    ]
    rows.extend(
        _row('unrelated', f'mutate_{i}', meta={'risk': 'destructive'})
        for i in range(4))
    return rows


def test_catalog_index_is_content_addressed_and_private_meta_never_leaks():
    snapshot = _large_snapshot()
    first = build_catalog_index(snapshot)
    second = build_catalog_index(list(reversed(snapshot)))
    assert first is second
    assert first.fingerprint == second.fingerprint
    assert all('_meta' not in row['openai_def'] and 'meta' not in row['openai_def']
               for row in snapshot)

    invalidate_server_catalog('xuecheng')
    rebuilt = build_catalog_index(snapshot)
    assert rebuilt is not first
    assert rebuilt.fingerprint == first.fingerprint


def test_catalog_index_accepts_valid_precomputed_fingerprint(monkeypatch):
    snapshot = _large_snapshot()
    fingerprint = tool_search.catalog_snapshot_fingerprint(snapshot)

    def unexpected_rehash(_snapshot):
        pytest.fail('a valid generation fingerprint must bypass snapshot hashing')

    monkeypatch.setattr(
        tool_search, 'catalog_snapshot_fingerprint', unexpected_rehash)
    index = build_catalog_index(snapshot, fingerprint_hint=fingerprint)

    assert index.fingerprint == fingerprint


def test_catalog_index_cache_is_lru_bounded(monkeypatch):
    monkeypatch.setattr(tool_search, '_catalog_index_capacity', lambda: 2)

    def versioned(version):
        rows = _large_snapshot()
        for row in rows:
            row['catalog_version'] = version
        return rows

    first = build_catalog_index(versioned('v1'))
    second = build_catalog_index(versioned('v2'))
    assert build_catalog_index(versioned('v1')) is first
    build_catalog_index(versioned('v3'))

    assert tool_search.catalog_cache_stats()['indexes'] == 2
    assert build_catalog_index(versioned('v2')) is not second


def test_ambiguous_fallback_order_is_precomputed(monkeypatch):
    snapshot = _large_snapshot()
    index = build_catalog_index(snapshot)
    expected_names = list(index.fallback_names[:4])
    assert len(index.fallback_names) == len(index.tools)
    assert all(
        name is index.by_name[name].name for name in index.fallback_names)

    def unexpected_risk_reclassification(_risk):
        pytest.fail('stable catalog fallback must not be re-sorted per request')

    monkeypatch.setattr(
        tool_search, '_risk_fallback_priority',
        unexpected_risk_reclassification)
    selected = select_active_mcp_tools(
        snapshot, task_id='precomputed-fallback', query='', limit=4)

    assert _names(selected) == expected_names


def test_long_query_skips_impossible_phrase_boost_scan(monkeypatch):
    snapshot = _large_snapshot()
    index = build_catalog_index(snapshot)
    query = 'z' * (index.max_query_boost_chars + 1)

    def unexpected_phrase_scan(*_args):
        pytest.fail('an overlong query cannot equal or fit inside metadata')

    monkeypatch.setattr(
        tool_search, '_apply_query_phrase_boosts', unexpected_phrase_scan)
    selected = select_active_mcp_tools(
        snapshot, task_id='overlong-phrase-boost', query=query, limit=4)

    assert selected


def test_phrase_boost_only_visits_posting_candidates_with_terms():
    candidate = SimpleNamespace(
        name='candidate', short_name='candidate', aliases=(), intents=())

    class UnexpectedString(str):
        def casefold(self):
            pytest.fail('a non-posting tool must not be visited')

    unrelated = SimpleNamespace(
        name='unrelated', short_name=UnexpectedString('unrelated'),
        aliases=(), intents=())
    index = SimpleNamespace(
        tools=(candidate, unrelated), by_name={'candidate': candidate})
    scores = {'candidate': 1.0}

    tool_search._apply_query_phrase_boosts(
        index, scores, 'request', query_has_terms=True)

    assert scores == {'candidate': 1.0}


def test_dense_phrase_boost_candidates_keep_tuple_scan():
    tools = tuple(
        SimpleNamespace(
            name=f'tool-{index}', short_name=f'tool-{index}',
            aliases=(), intents=())
        for index in range(8)
    )

    class UnexpectedLookup(dict):
        def __getitem__(self, key):
            pytest.fail(f'a dense posting set must not look up {key}')

    index = SimpleNamespace(tools=tools, by_name=UnexpectedLookup())
    scores = {tool.name: 1.0 for tool in tools[:7]}

    tool_search._apply_query_phrase_boosts(
        index, scores, 'request', query_has_terms=True)

    assert len(scores) == 7


def test_punctuation_only_query_preserves_intent_substring_boost():
    snapshot = [
        _row('compiler', 'compile', description='build source', meta={
            'intents': ['compile C++ source'], 'risk': 'write'}),
    ]
    snapshot.extend(
        _row('safe', f'read_{index}', meta={'risk': 'read'})
        for index in range(8))

    selected = select_active_mcp_tools(
        snapshot, task_id='punctuation-intent', query='++', limit=4)

    assert _names(selected) == ['mcp__compiler__compile']


def test_small_catalog_record_path_enforces_state_capacity(monkeypatch):
    monkeypatch.setattr(tool_search, '_selection_state_capacity', lambda: 2)

    for index in range(3):
        record_mcp_tool_used(
            f'small-catalog-task-{index}', 'mcp__docs__read')

    assert tuple(tool_search._SELECTION_STATE) == (
        'small-catalog-task-1',
        'small-catalog-task-2',
    )
    assert tool_search.catalog_cache_stats()['tasks'] == 2


def test_selection_state_expiration_only_reads_ordered_prefix(monkeypatch):
    now = 100_000.0
    touched_reads = 0

    class CountingState(dict):
        def get(self, key, default=None):
            nonlocal touched_reads
            if key == 'touched':
                touched_reads += 1
            return super().get(key, default)

    monkeypatch.setattr(tool_search, '_selection_state_capacity', lambda: 4096)
    for index in range(4096):
        tool_search._SELECTION_STATE[str(index)] = CountingState(
            touched=now - 4095 + index)

    tool_search._prune_states(now)

    assert len(tool_search._SELECTION_STATE) == 4096
    assert touched_reads == 1


def test_selection_state_expiration_removes_only_expired_prefix(monkeypatch):
    now = 100_000.0
    ttl = tool_search._STATE_TTL_SECONDS
    expiration_count = tool_search.catalog_cache_stats()['stateExpirations']
    monkeypatch.setattr(tool_search, '_selection_state_capacity', lambda: 8)
    tool_search._SELECTION_STATE.update({
        'expired-oldest': {'touched': now - ttl - 2},
        'expired-newer': {'touched': now - ttl - 1},
        'live-boundary': {'touched': now - ttl},
        'live-newest': {'touched': now},
    })

    tool_search._prune_states(now)

    assert tuple(tool_search._SELECTION_STATE) == (
        'live-boundary', 'live-newest')
    assert (tool_search.catalog_cache_stats()['stateExpirations']
            == expiration_count + 2)


def test_selection_state_uses_process_monotonic_clock(monkeypatch):
    ticks = iter((10_000.0, 20_000.0))
    monkeypatch.setattr(tool_search.time, 'monotonic', lambda: next(ticks))

    record_mcp_tool_used('record-clock', 'mcp__docs__read')
    select_active_mcp_tools(
        _large_snapshot(), task_id='select-clock', query='search', limit=4)

    assert tool_search._SELECTION_STATE['record-clock']['touched'] == 10_000.0
    assert tool_search._SELECTION_STATE['select-clock']['touched'] == 20_000.0


def test_selection_state_retains_no_query_content():
    secret = 'private-longform-source-sentence'
    select_active_mcp_tools(
        _large_snapshot(), task_id='digest-task',
        query=(secret + ' ') * 200, limit=4)

    state = tool_search._SELECTION_STATE['digest-task']
    assert 'query' not in state
    assert secret not in repr(state)


def test_identical_query_skips_repeated_normalized_profile(monkeypatch):
    profile_calls = 0
    original_query_profile = tool_search._query_profile

    def counted_query_profile(value):
        nonlocal profile_calls
        profile_calls += 1
        return original_query_profile(value)

    monkeypatch.setattr(tool_search, '_query_profile', counted_query_profile)
    first = select_active_mcp_tools(
        _large_snapshot(), task_id='raw-digest-fast-path',
        query='long repeated coding context ' * 400, limit=4)
    second = select_active_mcp_tools(
        _large_snapshot(), task_id='raw-digest-fast-path',
        query='long repeated coding context ' * 400, limit=4)

    assert second == first
    assert profile_calls == 1


def test_punctuation_variant_query_cannot_rotate_frozen_wire(monkeypatch):
    profile_calls = 0
    original_query_profile = tool_search._query_profile

    def counted_query_profile(value):
        nonlocal profile_calls
        profile_calls += 1
        return original_query_profile(value)

    monkeypatch.setattr(tool_search, '_query_profile', counted_query_profile)
    first = select_active_mcp_tools(
        _large_snapshot(), task_id='normalized-digest-compat',
        query='hope search', limit=4)
    second = select_active_mcp_tools(
        _large_snapshot(), task_id='normalized-digest-compat',
        query='hope, search', limit=4)

    assert second == first
    # The frozen wire short-circuits before any re-profiling.
    assert profile_calls == 1


def test_recorded_tool_names_are_content_bounded():
    for index in range(80):
        record_mcp_tool_used('bounded-tools', f'mcp__docs__tool_{index}')

    state = tool_search._SELECTION_STATE['bounded-tools']
    assert len(state['used']) == tool_search._MAX_STICKY_USED_TOOLS
    assert state['used'][-1] == 'mcp__docs__tool_79'


def test_tool_search_term_budget_is_resolved_once_per_process(monkeypatch):
    from lib.tools import resource_policy

    calls = 0

    def resolve_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return 512

    resource_policy.tool_search_term_cache_capacity.cache_clear()
    monkeypatch.setattr(resource_policy, 'resolve_resource_budget', resolve_once)
    try:
        assert resource_policy.tool_search_term_cache_capacity() == 512
        assert resource_policy.tool_search_catalog_index_capacity() == 4
        assert resource_policy.tool_search_selection_state_capacity() == 1024
        assert resource_policy.tool_search_selection_state_capacity() == 1024
        assert calls == 1
    finally:
        resource_policy.tool_search_term_cache_capacity.cache_clear()


def test_tool_search_cache_capacities_derive_from_term_budget(monkeypatch):
    from lib.tools import resource_policy

    monkeypatch.setattr(
        resource_policy, 'tool_search_term_cache_capacity', lambda: 512)
    assert resource_policy.tool_search_catalog_index_capacity() == 4
    assert resource_policy.tool_search_selection_state_capacity() == 1024

    monkeypatch.setattr(
        resource_policy, 'tool_search_term_cache_capacity', lambda: 4096)
    assert resource_policy.tool_search_catalog_index_capacity() == 32
    assert resource_policy.tool_search_selection_state_capacity() == 4096


def test_selection_is_stable_bounded_and_expands_workflow_dependencies():
    snapshot = _large_snapshot()
    first = select_active_mcp_tools(
        snapshot, task_id='mcp-edit-task', query='请编辑学城文档', limit=4)
    second = select_active_mcp_tools(
        snapshot, task_id='mcp-edit-task', query='请编辑学城文档', limit=4)
    assert first == second
    names = _names(first)
    assert 'mcp__xuecheng__update_doc' in names
    assert names.index('mcp__xuecheng__prepare_doc_edit') \
        < names.index('mcp__xuecheng__update_doc')
    # Bundle members consume base slots; hard requirements may exceed them.
    assert len(names) <= 5


def test_selection_is_independent_of_fresh_snapshot_order():
    snapshot = _large_snapshot()
    forward = select_active_mcp_tools(
        snapshot, task_id='forward-order', query='请编辑学城文档', limit=4)
    tool_search.clear_tool_search_caches()
    reversed_result = select_active_mcp_tools(
        list(reversed(snapshot)), task_id='reverse-order',
        query='请编辑学城文档', limit=4)

    assert _names(reversed_result) == _names(forward)


def test_dependency_moves_before_owner_when_both_were_already_selected():
    snapshot = [
        _row('xuecheng', 'prepare_doc_edit', description='load editable source',
             meta={'bundle': 'edit_document', 'risk': 'read'}),
        _row('xuecheng', 'update_doc', description='update document',
             meta={'bundle': 'edit_document',
                   'requires': ['prepare_doc_edit'],
                   'intents': ['编辑学城文档'], 'risk': 'write'}),
    ]
    snapshot.extend(
        _row('hope', f'unrelated_{i}', description=f'unrelated tool {i}')
        for i in range(8))
    selected = select_active_mcp_tools(
        snapshot, task_id='mcp-dependency-order-task',
        query='编辑学城文档', limit=4)
    names = _names(selected)
    assert names.index('mcp__xuecheng__prepare_doc_edit') \
        < names.index('mcp__xuecheng__update_doc')


def test_wire_freezes_after_first_selection_and_survives_intent_change():
    """The wire freezes at the scope's first selection: a later query-intent
    change must NOT rotate tools. The tools array opens every provider
    request, so any mid-conversation drift invalidates the whole prefix
    cache; late tools surface via the composer tail delta block instead."""
    snapshot = _large_snapshot()
    initial = select_active_mcp_tools(
        snapshot, task_id='mcp-sticky-task', query='hope search', limit=4)
    used = 'mcp__hope__search'
    assert used in _names(initial)
    record_mcp_tool_used('mcp-sticky-task', used)

    changed = select_active_mcp_tools(
        snapshot, task_id='mcp-sticky-task', query='编辑学城文档', limit=4)
    assert changed == initial


def test_frozen_wire_survives_catalog_change_and_disconnect():
    """A server that disconnects mid-conversation must not shrink the wire;
    one that connects must not grow it. Both drift classes go to the tail
    delta block, never the tools array."""
    snapshot = _large_snapshot()
    initial = select_active_mcp_tools(
        snapshot, task_id='mcp-freeze-task', query='hope search', limit=4)

    reduced = [row for row in snapshot if row['server_id'] != 'xuecheng']
    assert reduced != snapshot
    after_disconnect = select_active_mcp_tools(
        reduced, task_id='mcp-freeze-task', query='hope search', limit=4)
    assert after_disconnect == initial

    grown = snapshot + [_row('late', 'late_tool', description='late arrival')]
    after_connect = select_active_mcp_tools(
        grown, task_id='mcp-freeze-task', query='late arrival', limit=4)
    assert after_connect == initial


def test_empty_wire_freeze_keeps_late_catalog_off_the_wire():
    """Freezing an empty wire (registry path when no server is connected at
    the conversation's first assembly) keeps a server that appears later out
    of the tools array — it is surfaced in the tail delta block instead."""
    scope = mcp_selection_scope_id(
        task_id='late-connect-task', conv_id='late-connect-conv',
        owner_user_id=7)
    assert freeze_wire_definitions(scope, []) == []
    names, is_frozen = frozen_wire_tool_names(scope)
    assert is_frozen and names == ()

    selected = select_active_mcp_tools(
        _large_snapshot(), task_id='late-connect-task',
        selection_scope_id=scope, query='hope search', limit=4)
    assert selected == []


def test_frozen_wire_tool_names_reflects_first_selection():
    scope = mcp_selection_scope_id(
        task_id='wire-names-task', conv_id='wire-names-conv',
        owner_user_id=7)
    assert frozen_wire_tool_names(scope) == ((), False)
    selected = select_active_mcp_tools(
        _large_snapshot(), task_id='wire-names-task',
        selection_scope_id=scope, query='hope search', limit=4)
    names, is_frozen = frozen_wire_tool_names(scope)
    assert is_frozen
    assert set(names) == set(_names(selected))
    # Unknown scope: read-only, does not create state.
    unknown = mcp_selection_scope_id(
        task_id='wire-names-never', conv_id='wire-names-never',
        owner_user_id=7)
    assert frozen_wire_tool_names(unknown) == ((), False)


def test_registry_freezes_empty_wire_when_no_server_connected(monkeypatch):
    class _Bridge:
        connected = False
        server_count = 0

    import lib.mcp as mcp_module
    from lib.tools.registry import _build

    monkeypatch.setattr(mcp_module, 'get_bridge', lambda: _Bridge())
    cfg = {'mcpEnabled': True}
    ctx = SimpleNamespace(
        cfg=cfg, tid='empty-freeze', task_id='empty-freeze',
        messages=[{'role': 'user', 'content': 'hi'}],
        conv_id='empty-freeze-conv', owner_user_id=7)

    assert _build._build_mcp(ctx) == []
    scope = mcp_selection_scope_id(
        task_id='empty-freeze', conv_id='empty-freeze-conv', owner_user_id=7)
    assert cfg['_mcpSelectionScopeId'] == scope
    names, is_frozen = frozen_wire_tool_names(scope)
    assert is_frozen and names == ()


def test_marking_an_active_tool_used_does_not_reorder_the_wire_schema():
    snapshot = _large_snapshot()
    initial = select_active_mcp_tools(
        snapshot, task_id='mcp-order-task', query='hope utility', limit=4)
    initial_names = _names(initial)
    assert len(initial_names) >= 2
    record_mcp_tool_used('mcp-order-task', initial_names[-1])
    after = select_active_mcp_tools(
        snapshot, task_id='mcp-order-task', query='hope utility', limit=4)
    assert _names(after) == initial_names


def test_low_signal_confirmation_keeps_owner_scoped_conversation_surface():
    snapshot = _confirmation_snapshot()
    scope = mcp_selection_scope_id(
        task_id='confirmation-first', conv_id='conv-confirmation',
        owner_user_id=7)
    initial = select_active_mcp_tools(
        snapshot, task_id='confirmation-first',
        selection_scope_id=scope, query='read xuecheng document', limit=8)
    initial_names = _names(initial)
    assert initial_names
    assert all(name.startswith('mcp__xuecheng__') for name in initial_names)

    record_mcp_tool_used(
        'confirmation-first', 'mcp__xuecheng__read_doc',
        selection_scope_id=scope)
    confirmed = select_active_mcp_tools(
        snapshot, task_id='confirmation-second',
        selection_scope_id=scope, query='Agreed', limit=8)
    assert _names(confirmed) == initial_names
    assert not any(name.startswith('mcp__12306-train__')
                   for name in _names(confirmed))

    isolated_scope = mcp_selection_scope_id(
        task_id='confirmation-other-owner', conv_id='conv-confirmation',
        owner_user_id=8)
    unrelated = select_active_mcp_tools(
        snapshot, task_id='confirmation-other-owner',
        selection_scope_id=isolated_scope, query='Agreed', limit=8)
    assert any(name.startswith('mcp__12306-train__')
               for name in _names(unrelated))


def test_registry_seeds_confirmation_from_previous_turn_mcp_history(monkeypatch):
    snapshot = _confirmation_snapshot()
    messages = [
        {'role': 'user', 'content': 'Read the Xuecheng document'},
        {'role': 'assistant', 'content': 'Authorization is required.',
         'toolRounds': [
             {'toolName': 'mcp__xuecheng__read_doc'},
             {'toolName': 'mcp__xuecheng__login'},
             {'toolName': 'mcp__xuecheng__read_doc'},
         ]},
        {'role': 'user', 'content': 'Agreed'},
    ]
    assert recent_conversation_mcp_tool_names(messages, limit=8) == [
        'mcp__xuecheng__read_doc', 'mcp__xuecheng__login']

    class _Bridge:
        connected = True
        server_count = 3

        @staticmethod
        def get_openai_tool_defs():
            return [row['openai_def'] for row in snapshot]

        @staticmethod
        def get_tool_catalog_snapshot():
            return snapshot

    import lib.mcp as mcp_module
    from lib.tools.registry import _build

    monkeypatch.setattr(mcp_module, 'get_bridge', lambda: _Bridge())
    cfg = {'mcpEnabled': True, 'mcpActiveToolLimit': 8}
    ctx = SimpleNamespace(
        cfg=cfg, tid='confirmation-history', task_id='confirmation-history',
        messages=messages, conv_id='conv-history-seed', owner_user_id=7)
    active_names = _names(_build._build_mcp(ctx))

    assert active_names == [
        'mcp__xuecheng__read_doc', 'mcp__xuecheng__login']
    assert cfg['_mcpSelectionScopeId'] == mcp_selection_scope_id(
        task_id='confirmation-history', conv_id='conv-history-seed',
        owner_user_id=7)


def test_registry_uses_generation_projection_without_public_snapshot(monkeypatch):
    snapshot = _large_snapshot()
    fingerprint = tool_search.catalog_snapshot_fingerprint(snapshot)
    search_projection = {
        row['namespaced_name']: f"precomputed {row['tool_name']}"
        for row in snapshot
    }

    class _Bridge:
        connected = True
        server_count = 2
        projection_calls = 0

        @staticmethod
        def get_openai_tool_defs():
            return [row['openai_def'] for row in snapshot]

        @classmethod
        def get_tool_catalog_projection(cls):
            cls.projection_calls += 1
            return fingerprint, tuple(snapshot)

        @staticmethod
        def get_tool_catalog_search_text_projection():
            return search_projection

        @staticmethod
        def get_tool_catalog_snapshot():
            pytest.fail('the cached projection must bypass public snapshot copies')

    import lib.mcp as mcp_module
    from lib.tools.registry import _build

    monkeypatch.setattr(mcp_module, 'get_bridge', lambda: _Bridge())
    cfg = {'mcpEnabled': True, 'mcpActiveToolLimit': 8}
    ctx = SimpleNamespace(
        cfg=cfg, tid='projection-fast-path', task_id='projection-fast-path',
        messages=[{'role': 'user', 'content': '编辑学城文档'}],
        conv_id='projection-conversation', owner_user_id=7)

    active = _build._build_mcp(ctx)

    assert _Bridge.projection_calls == 1
    assert cfg['_mcpToolSearchTextByName'] is search_projection
    assert 0 < len(active) < len(snapshot)


def test_ambiguous_prompt_gets_deterministic_four_tool_starter_set():
    snapshot = _large_snapshot()
    selected = select_active_mcp_tools(
        snapshot, task_id='mcp-fallback-task', query='完全不匹配的意图',
        limit=8)
    assert len(selected) == 4
    assert selected == select_active_mcp_tools(
        snapshot, task_id='mcp-fallback-task', query='完全不匹配的意图',
        limit=8)



def test_ambiguous_fallback_normalizes_llm_and_hope_risk_vocabularies():
    snapshot = [
        _row('llm', 'safe_none', meta={'risk': 'none'}),
        _row('hope', 'safe_read', meta={'risk': 'read'}),
        _row('other', 'unspecified'),
        _row('llm', 'auth', meta={'risk': 'auth'}),
        _row('llm', 'mutating', meta={'risk': 'mutating'}),
        _row('hope', 'write', meta={'risk': 'write'}),
        _row('llm', 'destructive', meta={'risk': 'destructive'}),
        _row('hope', 'second_read', meta={'risk': 'readonly'}),
        _row('other', 'second_unspecified'),
    ]
    selected = select_active_mcp_tools(
        snapshot, task_id='mcp-risk-vocabulary-task',
        query='no matching intent', limit=8)
    names = _names(selected)
    assert names == [
        'mcp__hope__safe_read',
        'mcp__hope__second_read',
        'mcp__llm__safe_none',
        'mcp__other__second_unspecified',
    ]
    assert not any(name.endswith(('__auth', '__mutating', '__write',
                                  '__destructive')) for name in names)

class _FakeTool:
    def __init__(self, name, *, meta=None):
        self.name = name
        self.description = name
        self.inputSchema = {'type': 'object', 'properties': {}}
        self.meta = meta or {}
        self.annotations = SimpleNamespace(readOnlyHint=True)


def test_bridge_snapshot_uses_server_version_hash_and_refresh_notification():
    bridge = MCPBridge()
    handle = _MCPServerHandle('xuecheng-mcp', {})
    old_tool = _FakeTool('read_doc', meta={'aliases': ['学城']})
    assert bridge._replace_server_catalog(
        'xuecheng-mcp', handle, [old_tool], catalog_version='catalog-1')
    assert not bridge._replace_server_catalog(
        'xuecheng-mcp', handle, [old_tool], catalog_version='catalog-1')
    fingerprint, projection = bridge.get_tool_catalog_projection()
    repeated_fingerprint, repeated_projection = (
        bridge.get_tool_catalog_projection())
    assert repeated_fingerprint == fingerprint
    assert repeated_projection is projection

    snapshot = bridge.get_tool_catalog_snapshot()
    expected_search_text = tool_search.catalog_search_text_by_name(snapshot)
    search_text = bridge.get_tool_catalog_search_text_projection()
    assert dict(search_text) == expected_search_text
    assert bridge.get_tool_catalog_search_text_projection() is search_text
    with pytest.raises(TypeError):
        search_text['mcp__xuecheng-mcp__read_doc'] = 'mutated'
    assert snapshot[0]['catalog_version'] == 'catalog-1'
    assert snapshot[0]['schema_hash'].startswith('sha256:')
    assert snapshot[0]['meta']['aliases'] == ['学城']
    assert 'meta' not in snapshot[0]['openai_def']
    snapshot[0]['tool_name'] = 'mutated-public-copy'
    snapshot[0]['meta'] = {'aliases': ['mutated-public-copy']}
    assert projection[0]['tool_name'] == 'read_doc'
    assert projection[0]['meta']['aliases'] == ['学城']

    new_tool = _FakeTool('update_doc', meta={
        'requires': ['read_doc'], 'risk': 'write'})

    class _Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[old_tool, new_tool],
                                   catalogVersion='catalog-2')

    handle.session = _Session()
    handle.tools_list_changed = True
    asyncio.run(bridge._handle_server_message(
        handle, {'method': 'notifications/tools/list_changed'}))
    refreshed_fingerprint, refreshed_projection = (
        bridge.get_tool_catalog_projection())
    refreshed = bridge.get_tool_catalog_snapshot()
    refreshed_search_text = bridge.get_tool_catalog_search_text_projection()
    assert refreshed_fingerprint != fingerprint
    assert refreshed_projection is not projection
    assert refreshed_search_text is not search_text
    assert dict(refreshed_search_text) == (
        tool_search.catalog_search_text_by_name(refreshed))
    assert {row['tool_name'] for row in refreshed} == {
        'read_doc', 'update_doc'}
    assert {row['catalog_version'] for row in refreshed} == {'catalog-2'}


def test_bridge_disconnect_invalidates_cached_catalog_projection():
    bridge = MCPBridge()
    handle = _MCPServerHandle('docs', {})
    assert bridge._replace_server_catalog(
        'docs', handle, [_FakeTool('read')], catalog_version='catalog-1')
    fingerprint, projection = bridge.get_tool_catalog_projection()

    assert bridge._disconnect_one('docs')
    empty_fingerprint, empty_projection = bridge.get_tool_catalog_projection()

    assert projection
    assert not empty_projection
    assert empty_fingerprint != fingerprint
