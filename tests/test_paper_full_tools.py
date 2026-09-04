#!/usr/bin/env python3
"""Paper full-tool-set guards (2026-07-28).

The paper report + Q&A engines now ship the FULL chat-tier tool set (not just
web_search / fetch_url), assembled through the SHARED registry and executed
through the SHARED single-tool dispatch. This suite pins the four contracts:

  1. PARITY + PROJECTION — the server-owned paper catalog is derived from the
     chat-tier registry snapshot. The product default is uncapped for every
     model; an explicit budget keeps hidden capabilities searchable/executable.
  2. ROUTING — non-search tool calls (read_files / todo_write / run_command→
     code_exec) execute through ``_execute_tool_one`` with the paper event /
     display schema preserved; research-only engines keep ``Unknown tool``.
  3. HONEST BOUNDING — shipped/default results use ToolResultEnvelopeV2, with
     an 8k single-result / 24k aggregate cap and owner-scoped semantic artifact
     continuation. An explicit registered control arm can reproduce the still-
     bounded legacy baseline without contaminating the V2 candidate.
  4. POLICY — ordinary write-partition calls in an unattended paper engine are
     auto-approved AND audit-logged. Registry-declared attended-confirmation
     tools are not advertised because a headless task cannot mint their
     one-use approval receipt.

NEUTER map (each mutation was verified to turn the named test(s) red):
  * build_paper_full_tool_context → research set only ...... parity tests
  * _execute_shared_tool → early 'Unknown tool' return ..... routing tests
  * drop the run_command→code_exec flip .................... code_exec routing
  * drop audit_log in _execute_shared_tool ................. auto-approve test
  * cap_tool_result → identity (no envelope/artifact) ...... bounding tests
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


def _names(tools):
    return sorted(t['function']['name'] for t in tools)


# ─── 1. Parity: paper full set == chat-tier registry assembly ────────────

def test_full_tool_set_matches_chat_tier_registry():
    from lib.paper.tools import build_paper_full_tool_context
    paper_tools, _paper_context = build_paper_full_tool_context()
    paper_names = _names(paper_tools)

    # Independently assemble what chat mode gets with the chat-tier flags and
    # no project — the two MUST be identical, today and after any future
    # registry change (that's the whole point of routing through the chassis).
    from lib.tools.registry import (
        ToolContext,
        assemble_tool_list,
        resolve_enabled_plugins,
    )
    cfg = {}
    ctx = ToolContext(
        cfg=cfg, task_id='', project_path='', project_enabled=False,
        search_mode='multi', search_enabled=True, fetch_enabled=True,
        code_exec_enabled=True,
        browser_enabled=False, desktop_enabled=False,
        enabled_plugins=resolve_enabled_plugins(cfg),
    )
    chat_tools, _ = assemble_tool_list(ctx)
    assert paper_names == _names(chat_tools), \
        f'paper full set drifted from chat tier: {paper_names} vs {_names(chat_tools)}'

    # Core pins (belt against the profile itself being gutted):
    for core in ('web_search', 'fetch_url', 'read_files', 'inspect_image',
                 'run_command', 'create_memory', 'search_memories',
                 'todo_write'):
        assert core in paper_names, f'{core} missing from paper full set'
    assert 'schedule_create' in _names(ctx.executable_tool_catalog), (
        'scheduler must remain discoverable even though its searchable policy '
        'keeps it out of the eager wire schema')
    # Project-write family must stay gated OFF (no project attached).
    for gated in ('write_file', 'apply_diff', 'list_dir', 'grep_search'):
        assert gated not in paper_names, f'{gated} leaked into project-less set'
    # The builder must return a fresh registry snapshot on every request.
    rebuilt_tools, _rebuilt_context = build_paper_full_tool_context()
    assert rebuilt_tools is not paper_tools
    assert _names(rebuilt_tools) == paper_names


def test_research_set_stays_narrow():
    """insight / recommend / ideate / survey keep search+fetch ONLY."""
    from lib.paper.tools import build_research_tool_schemas
    assert _names(build_research_tool_schemas()) == ['fetch_url', 'web_search']


def test_full_epoch_default_is_uncapped_and_model_neutral():
    from lib.paper.tools import build_paper_full_tool_epoch
    from lib.tools.gateway import tool_schema_tokens

    epoch = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3')
    other = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='gpt-5.4')
    wire_names = set(_names(epoch.wire_schemas))
    authority_names = set(_names(epoch.executable_schemas))

    assert epoch.schema_tokens == tool_schema_tokens(
        epoch.wire_schemas, model='kimi-k3')
    assert epoch.schema_budget_tokens == other.schema_budget_tokens == 0
    assert epoch.schema_tokens > 4_000
    assert epoch.gateway_schema_tokens == other.gateway_schema_tokens == 0
    assert wire_names == authority_names == set(_names(other.wire_schemas))
    telemetry = epoch.telemetry()
    assert telemetry['configuredSchemaBudgetTokens'] == 0
    assert telemetry['wireToolCount'] == len(epoch.wire_schemas)
    assert telemetry['executableToolCount'] == len(epoch.executable_schemas)
    assert telemetry['searchableToolCount'] == 0
    assert len(epoch.epoch_hash) == 64
    repeated = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3')
    assert repeated.epoch_hash == epoch.epoch_hash
    assert repeated.wire_schemas == epoch.wire_schemas
    assert {'web_search', 'fetch_url', 'read_files', 'inspect_image',
            'read_tool_artifact', 'search_tool_artifact', 'run_command',
            'create_memory', 'schedule_create', 'produce_slides',
            'spawn_agents', 'get_agent_result', 'search_skills', 'load_skill',
            'read_skill_resource', 'local_serve_prepare',
            'local_serve_status', 'local_serve_list',
            'local_serve_stop'} <= wire_names
    assert 'search_tools' not in wire_names and 'execute_tools' not in wire_names
    from lib.tools.registry import all_specs
    attended_confirmation_names = set().union(*(
        set(spec.confirmation_tools) for spec in all_specs()
    ))
    assert not authority_names & attended_confirmation_names
    assert 'ask_human' not in authority_names, (
        'an unattended paper task must never expose a wait that cannot resume')


def test_explicit_budget_bounds_wire_but_preserves_searchable_authority():
    from lib.paper.tools import build_paper_full_tool_epoch

    epoch = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3',
        cfg={'tools': {'schemaBudgetTokens': 4_000}})
    wire_names = set(_names(epoch.wire_schemas))
    authority_names = set(_names(epoch.executable_schemas))

    assert epoch.schema_budget_tokens == 4_000
    assert epoch.schema_tokens <= 4_000
    assert epoch.gateway_schema_tokens <= 500
    assert {'web_search', 'fetch_url', 'read_files', 'inspect_image',
            'read_tool_artifact', 'search_tool_artifact',
            'search_tools', 'execute_tools'} <= wire_names
    assert {'run_command', 'create_memory', 'schedule_create',
            'produce_slides', 'spawn_agents'} <= authority_names - wire_names
    assert all(epoch.discovery_policy_by_name[name] == 'searchable'
               for name in authority_names - wire_names)


def test_full_epoch_tokenizer_budget_drift_never_aborts(monkeypatch):
    from lib.paper.tools import build_paper_full_tool_epoch
    import lib.tools.gateway as gateway

    real_counter = gateway.tool_schema_tokens

    def _counter_with_gateway_drift(tools, *, model=''):
        values = list(tools or ())
        names = {
            str((tool.get('function') or {}).get('name') or '')
            for tool in values if isinstance(tool, dict)
        }
        measured = real_counter(values, model=model)
        if names and names <= gateway.GATEWAY_TOOL_NAMES:
            return max(measured, gateway.LOCAL_GATEWAY_MAX_TOKENS + 1)
        return measured

    monkeypatch.setattr(gateway, 'tool_schema_tokens',
                        _counter_with_gateway_drift)

    epoch = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3',
        cfg={'tools': {'schemaBudgetTokens': 4_000}})

    assert epoch.gateway_schema_tokens == gateway.LOCAL_GATEWAY_MAX_TOKENS + 1
    assert {'search_tools', 'execute_tools'} <= set(
        _names(epoch.wire_schemas))


def test_full_epoch_contract_defect_degrades_to_text_only(monkeypatch):
    import lib.paper.tools as paper_tools

    def _fail_compile(*_args, **_kwargs):
        raise ValueError('derived paper contract is invalid')

    monkeypatch.setattr(
        paper_tools, 'compile_execution_contract_documents', _fail_compile)

    epoch = paper_tools.build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3')

    assert epoch.wire_schemas == ()
    assert epoch.executable_schemas == ()
    assert epoch.contract_documents_by_name == {}
    assert epoch.schema_tokens == 0
    assert epoch.gateway_schema_tokens == 0
    assert epoch.telemetry()['degradedReason'] == (
        'tool_epoch_assembly_failed:ValueError')


def test_ownerless_full_epoch_never_advertises_unreadable_artifacts():
    from lib.paper.tools import build_paper_full_tool_epoch

    epoch = build_paper_full_tool_epoch(owner_user_id=0, model='kimi-k3')
    authority_names = set(_names(epoch.executable_schemas))
    assert 'read_tool_artifact' not in authority_names
    assert 'search_tool_artifact' not in authority_names


def test_explicit_zero_budget_restores_the_pre_tool_search_control_surface():
    from lib.paper.tools import (
        build_paper_full_tool_epoch,
        make_paper_exec_shim,
    )

    epoch = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3',
        cfg={'tools': {'schemaBudgetTokens': 0}})
    names = set(_names(epoch.wire_schemas))
    assert epoch.schema_budget_tokens == 0
    assert epoch.schema_tokens > 4_000
    assert epoch.gateway_schema_tokens == 0
    assert len(epoch.wire_schemas) == len(epoch.executable_schemas) == 21
    assert 'search_tools' not in names and 'execute_tools' not in names
    assert {'run_command', 'create_memory', 'spawn_agents'} <= names
    assert {'search_skills', 'load_skill', 'read_skill_resource'} <= names
    assert 'request_skill_install' not in names
    assert not any(value == 'searchable'
                   for value in epoch.discovery_policy_by_name.values())

    shim = make_paper_exec_shim(
        task_id='paper_tool_surface_control',
        owner_user_id=TEST_OWNER_USER_ID, tool_epoch=epoch)
    assert shim['_tool_gateway_names'] == []
    assert shim['_toolSearchMode'] == 'off'


def _long_agent_arm_config(name):
    from lib.experiments.long_agent_strategy import LONG_AGENT_POLICIES

    policy = LONG_AGENT_POLICIES[name]
    return {
        'responses': {'promptProfile': policy['promptProfile']},
        'tools': {
            'schemaBudgetTokens': policy['schemaBudgetTokens'],
            'resultEnvelope': policy['resultEnvelope'],
        },
        'context': {'globalBudgetTokens': policy['contextBudgetTokens']},
        'compaction': {'strategy': policy['compactionStrategy']},
        'orchestration': {'policy': policy['orchestrationPolicy']},
    }


def test_registered_tool_arms_are_single_factor_in_paper_epoch():
    from lib.paper.tools import build_paper_full_tool_epoch

    control = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3',
        cfg=_long_agent_arm_config('control'))
    surface = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3',
        cfg=_long_agent_arm_config('tool_surface_v2'))
    result = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3',
        cfg=_long_agent_arm_config('tool_result_v2'))

    control_names = set(_names(control.wire_schemas))
    surface_names = set(_names(surface.wire_schemas))
    result_names = set(_names(result.wire_schemas))
    assert control.result_envelope == surface.result_envelope == 'legacy'
    assert control.schema_budget_tokens == result.schema_budget_tokens == 0
    assert surface.schema_budget_tokens == 4_000
    assert surface.schema_tokens <= 4_000
    assert {'search_tools', 'execute_tools'} <= surface_names
    assert not {'search_tools', 'execute_tools'} & control_names
    assert not {'read_tool_artifact', 'search_tool_artifact'} & control_names
    assert result.result_envelope == 'v2'
    assert {'read_tool_artifact', 'search_tool_artifact'} <= result_names
    assert control.telemetry()['wireToolCount'] == 19
    assert surface.telemetry()['wireToolCount'] == 6
    assert result.telemetry()['wireToolCount'] == 21


def test_full_paper_prompts_teach_the_bounded_gateway_contract():
    from lib.paper.prompts import _REPORT_PROMPT_EN, _REPORT_PROMPT_ZH
    from lib.paper.qa_context import build_qa_messages
    from lib.paper.tools import (
        apply_paper_tool_epoch_guidance,
        build_paper_full_tool_epoch,
    )

    for prompt in (_REPORT_PROMPT_EN, _REPORT_PROMPT_ZH):
        assert 'search_tools' not in prompt and 'execute_tools' not in prompt
        assert 'code_exec' not in prompt and 'run_command' not in prompt

    candidate = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3',
        cfg={'tools': {'schemaBudgetTokens': 4_000}})
    control = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3',
        cfg={'tools': {'schemaBudgetTokens': 0}})
    for lang in ('en', 'zh'):
        messages, _diag = build_qa_messages(
            'verify a number', 'Introduction\nEvidence.', '', lang=lang)
        assert apply_paper_tool_epoch_guidance(
            messages, candidate, lang=lang) is True
        system = messages[0]['content']
        assert 'search_tools' in system and 'execute_tools' in system
        assert 'arguments_schema' in system

        control_messages = [{'role': 'system', 'content': 'control prompt'}]
        assert apply_paper_tool_epoch_guidance(
            control_messages, control, lang=lang) is False
        assert control_messages[0]['content'] == 'control prompt'


def test_owner_epoch_adds_only_bounded_artifact_continuation():
    """Result continuation is task-relevant, owner-scoped, and retractable."""
    from lib.paper.tools import (
        build_research_tool_schemas,
        freeze_paper_tool_epoch,
    )

    owned, _contracts = freeze_paper_tool_epoch(
        build_research_tool_schemas(), owner_user_id=TEST_OWNER_USER_ID)
    assert _names(owned) == [
        'fetch_url', 'read_tool_artifact', 'search_tool_artifact', 'web_search']
    forced_final, final_contracts = freeze_paper_tool_epoch(
        None, owner_user_id=TEST_OWNER_USER_ID)
    assert forced_final == [] and final_contracts == {}


# ─── 2b. The repair schema index must see BUILDER-produced schemas ────────

def test_repair_index_covers_builder_produced_schemas(monkeypatch):
    """_build_schema_index walks declared schema owners.
    When a tool's schema becomes runtime-BUILT (build_search_tool), the walk
    must still index it or the bare-string-args repair for that tool silently
    dies (the '507 searches' regression class). Pins the builder branch."""
    import lib.tools.search as tools_mod
    import lib.tool_input_repair._schema as schema_mod

    probe_name = 'probe_builder_tool'
    monkeypatch.setattr(
        tools_mod, 'build_probe_tool_schema',
        lambda: {'type': 'function',
                 'function': {'name': probe_name,
                              'parameters': {'type': 'object',
                                             'properties': {'items': {'type': 'array'}}}}},
        raising=False)
    monkeypatch.setattr(schema_mod, '_SCHEMA_INDEX', None)

    idx = schema_mod._build_schema_index()
    assert probe_name in idx, 'builder-produced schema was NOT indexed'
    assert idx[probe_name]['properties']['items']['type'] == 'array'
    # And the repair actually fires through the public seam.
    from lib.tool_input_repair import parse_and_repair_tool_args
    repaired, log = parse_and_repair_tool_args(probe_name, '{"items": "bare"}')
    assert repaired['items'] == ['bare'], f'no coercion: {repaired!r} {log!r}'


def test_repair_index_sees_real_web_search_builder():
    """The production case: web_search is builder-produced (runtime vertical
    credentials) and its ``queries`` array type MUST be visible to repair."""
    from lib.tool_input_repair import parse_and_repair_tool_args
    repaired, log = parse_and_repair_tool_args(
        'web_search', '{"queries": "a real follow-up query"}')
    assert repaired['queries'] == ['a real follow-up query'], \
        f'bare-string queries not coerced: {repaired!r}'
    assert ('queries', 'bare_string_to_array') in log


# ─── 2. Routing through the shared dispatch ───────────────────────────────

def _shim(task_id='paper_test_1'):
    from lib.paper.tools import (
        build_paper_full_tool_epoch,
        make_paper_exec_shim,
    )
    epoch = build_paper_full_tool_epoch(
        owner_user_id=TEST_OWNER_USER_ID, model='kimi-k3',
        cfg={'tools': {'schemaBudgetTokens': 4_000}})
    return make_paper_exec_shim(
        task_id=task_id,
        owner_user_id=TEST_OWNER_USER_ID,
        tool_epoch=epoch,
        model='kimi-k3',
    )


def _round_entry(name, tc_id='tc_1'):
    return {'roundNum': 1, 'toolName': name, 'query': name,
            'toolCallId': tc_id, 'status': 'searching', 'results': None}


def test_shared_dispatch_read_files_reads_real_file(tmp_path):
    from lib.paper.tools import execute_paper_tool
    target = tmp_path / 'staged_asset.txt'
    body = 'paper full-tools e2e sentinel: the-model-must-see-this\n' * 3
    target.write_text(body, encoding='utf-8')

    shim = _shim()
    re_ = _round_entry('read_files')
    args = json.dumps({'path': str(target)})
    content, display, diag, ebkdn, verts = execute_paper_tool(
        'read_files', args, exec_shim=shim, round_entry=re_)

    assert 'the-model-must-see-this' in content, \
        f'read_files did not return the file body: {content[:200]!r}'
    assert diag is None and ebkdn is None and verts is None
    assert re_['status'] == 'done'
    assert re_['results'], 'handler must finalize display results'
    assert display == re_['results'], 'adapter must surface the finalized meta'
    meta = re_['results'][0]
    # build_project_tool_meta's shape (no toolName key): fetched + file label.
    assert meta.get('fetched') is True
    assert 'staged_asset.txt' in (meta.get('title', '') + meta.get('snippet', ''))


def test_unknown_tool_without_shim_keeps_narrow_behavior():
    """Research-only engines (no shim): a hallucinated name stops here."""
    from lib.paper.tools import execute_paper_tool
    content, display, *_ = execute_paper_tool(
        'read_files', '{"path": "/etc/hostname"}')
    assert content == 'Unknown tool: read_files'
    assert display == []


def test_shared_dispatch_todo_write_runs_and_finalizes():
    from lib.paper.tools import execute_paper_tool
    shim = _shim()
    re_ = _round_entry('todo_write')
    args = json.dumps({'todos': [{'id': '1', 'content': 'scan literature',
                                  'status': 'in_progress'}]})
    content, display, *_ = execute_paper_tool(
        'todo_write', args, exec_shim=shim, round_entry=re_)
    assert 'Unknown tool' not in content and 'Error' not in content[:40]
    assert shim.get('_todos'), 'todo_write must persist the checklist on the shim'
    assert re_['status'] == 'done' and re_['results']
    assert re_['results'][0].get('source') == 'Checklist'


def test_paper_tool_search_finds_and_executes_hidden_tool():
    """Schema reduction must not turn hidden catalog entries into dead tools."""
    from lib.paper.tools import execute_paper_tool

    shim = _shim('paper_gateway_e2e')
    search_round = _round_entry('search_tools', 'search_0')
    search_content, search_display, *_ = execute_paper_tool(
        'search_tools', json.dumps({'query': 'manage a task checklist'}),
        exec_shim=shim, round_entry=search_round)
    search_payload = json.loads(search_content)
    hits = {item['name']: item for item in search_payload['items']}
    assert 'todo_write' in hits
    assert hits['todo_write']['arguments_schema']['properties']['todos']
    assert any(row.get('toolName') == 'todo_write' for row in search_display)

    execute_round = _round_entry('execute_tools', 'execute_0')
    execute_content, execute_display, *_ = execute_paper_tool(
        'execute_tools', json.dumps({
            'calls': [{
                'name': 'todo_write',
                'arguments': {'todos': [{
                    'id': 'gateway-1', 'content': 'verify hidden execution',
                    'status': 'in_progress',
                }]},
            }],
        }), exec_shim=shim, round_entry=execute_round)
    execute_payload = json.loads(execute_content)

    assert execute_payload['status'] == 'ok'
    assert execute_payload['results'][0]['name'] == 'todo_write'
    assert execute_payload['results'][0]['status'] == 'done'
    assert shim['_todos'][0]['id'] == 'gateway-1'
    assert execute_round['status'] == 'done'
    assert execute_display == execute_round['results']


def test_run_command_routes_to_code_exec_special_handler():
    """run_command in a project-less engine must hit __code_exec__, not the
    project handler (which would die with 'No project path')."""
    from lib.paper.tools import execute_paper_tool, paper_effective_tool_name
    shim = _shim()
    re_ = _round_entry('code_exec')   # the engine pre-flips the display name
    args = json.dumps({'command': 'echo paper-full-tools-routing-ok'})
    content, display, *_ = execute_paper_tool(
        'run_command', args, exec_shim=shim, round_entry=re_)
    assert 'paper-full-tools-routing-ok' in content, content[:300]
    assert 'No project path' not in content
    assert paper_effective_tool_name('run_command') == 'code_exec'
    meta = (re_['results'] or [{}])[0]
    assert meta.get('toolName') == 'code_exec'
    assert str(meta.get('exitCode')) == '0'


def test_contract_rejection_never_reaches_shared_dispatch(monkeypatch):
    """A visible paper tool with invalid args settles rejected, not done."""
    import lib.paper.tools as paper_tools_mod

    schema = {
        'type': 'function',
        'function': {
            'name': 'paper_contract_probe',
            'description': 'Bounded paper contract probe.',
            'parameters': {
                'type': 'object',
                'properties': {'count': {'type': 'integer'}},
                'required': ['count'],
                'additionalProperties': False,
            },
        },
    }
    _schemas, contracts = paper_tools_mod.freeze_paper_tool_epoch([schema])
    shim = paper_tools_mod.make_paper_exec_shim(
        task_id='paper_contract_reject',
        owner_user_id=TEST_OWNER_USER_ID,
        tool_contract_documents_by_name=contracts,
    )
    round_entry = _round_entry('paper_contract_probe')

    def _must_not_execute(*_args, **_kwargs):
        raise AssertionError('contract-rejected paper call reached backend')

    monkeypatch.setattr(
        paper_tools_mod, '_execute_shared_tool', _must_not_execute,
        raising=True)

    content, display, *_ = paper_tools_mod.execute_paper_tool(
        'paper_contract_probe', '{"count": "many"}',
        exec_shim=shim, round_entry=round_entry)

    assert 'NOT executed' in content
    assert 'invalid_argument_type' in content
    assert display == []
    assert round_entry['status'] == 'rejected'
    assert round_entry['contractError']['code'] == 'invalid_argument_type'


def test_ambiguous_paper_epoch_fails_before_dispatch():
    """Duplicate executable names never resolve by list order."""
    from lib.paper.tools import freeze_paper_tool_epoch

    schema = {
        'type': 'function',
        'function': {
            'name': 'duplicate_paper_probe',
            'description': 'probe',
            'parameters': {'type': 'object', 'properties': {}},
        },
    }
    with pytest.raises(ValueError, match='duplicate executable tool contract'):
        freeze_paper_tool_epoch([schema, schema])


def test_research_round_rejects_tool_removed_from_dynamic_epoch():
    """Usage-policy filtering is also an execution deny-list for that round."""
    from lib.agent_loop import AbortSignal
    from lib.paper.tools import (
        PaperToolResultBudgetV2,
        build_research_tool_schemas,
        freeze_paper_tool_epoch,
        make_research_tool_executor,
    )

    web_only = [
        schema for schema in build_research_tool_schemas()
        if schema['function']['name'] == 'web_search'
    ]
    _schemas, contracts = freeze_paper_tool_epoch(web_only)
    messages = []
    events = []

    def _must_not_execute(*_args, **_kwargs):
        raise AssertionError('round-filtered research call reached backend')

    execute = make_research_tool_executor(
        messages,
        user_question='probe',
        abort_signal=AbortSignal.never(),
        result_budget=PaperToolResultBudgetV2(),
        paper_tool_executor=_must_not_execute,
        on_tool_event=events.append,
        contract_documents_for_round=lambda _rnd: contracts,
    )
    execute(0, {
        'id': 'tc_filtered',
        'function': {
            'name': 'fetch_url',
            'arguments': '{"urls": [{"url": "https://example.com"}]}',
        },
    })

    assert len(messages) == 1 and messages[0]['role'] == 'tool'
    assert 'NOT executed' in messages[0]['content']
    done = next(event for event in events if event['type'] == 'tool_done')
    assert done['status'] == 'rejected'
    assert done['contractError']['code'] == 'tool_contract_unavailable'


# ─── 3. Unattended auto-approval: explicit + audited ─────────────────────

def test_write_partition_call_is_auto_approved_and_audited(
        tmp_path, monkeypatch):
    import lib.log as _log
    import lib.memory.storage._dirs as _dirs

    audits = []
    monkeypatch.setattr(_log, 'audit_log',
                        lambda kind, **kw: audits.append((kind, kw)))
    # Redirect the server-side global memory store into the tmp sandbox.
    monkeypatch.setattr(_dirs, '_server_global_memory_dir',
                        lambda: str(tmp_path))

    from lib.paper.tools import execute_paper_tool
    shim = _shim()
    re_ = _round_entry('create_memory')
    args = json.dumps({
        'name': 'paper-autoapprove-probe',
        'description': 'auto-approval audit guard probe memory',
        'body': '## Why\nprobe body',
        'scope': 'global',
    })
    content, display, *_ = execute_paper_tool(
        'create_memory', args, exec_shim=shim, round_entry=re_)

    assert 'Memory created' in content, f'write did not execute: {content[:300]!r}'
    assert any(k == 'paper_tool_auto_approve' and kw.get('tool') == 'create_memory'
               for k, kw in audits), \
        f'auto-approval was NOT audited: {audits!r}'
    # read-only tools must NOT trigger the write audit.
    audits.clear()
    execute_paper_tool('todo_write', '{"todos": []}',
                         exec_shim=_shim(), round_entry=_round_entry('todo_write'))
    assert not audits, f'read-only tool spuriously audited: {audits!r}'

    # A write discovered behind the fixed gateway must keep the same explicit
    # paper approval trail; auditing only the outer execute_tools name would
    # make this path silently different from a direct call.
    gateway_shim = _shim('paper_gateway_write_audit')
    execute_paper_tool(
        'execute_tools', json.dumps({'calls': [{
            'name': 'create_memory',
            'arguments': {
                'name': 'paper-gateway-autoapprove-probe',
                'description': 'nested approval audit guard probe',
                'body': '## Why\nnested probe body',
                'scope': 'global',
            },
        }]}),
        exec_shim=gateway_shim,
        round_entry=_round_entry('execute_tools', 'gateway_write_0'),
    )
    assert any(
        kind == 'paper_tool_auto_approve'
        and detail.get('tool') == 'create_memory'
        for kind, detail in audits), audits


# ─── 4. Honest V2 bounding + continuation ─────────────────────────────────

def test_cap_tool_result_wraps_short_content_in_v2_envelope():
    from lib.paper.tools import cap_tool_result
    value = json.loads(cap_tool_result('short', 'read_files'))
    assert value['contractVersion'] == 'tofu.tool-result/v2'
    assert value['status'] == 'ok' and value['summary'] == 'short'
    assert value['artifactRef'] == '' and value['truncated'] is False


def test_cap_tool_result_bounds_read_files_and_uses_owner_artifact(monkeypatch):
    from lib.paper.tools import cap_tool_result
    import lib.tasks_pkg.compaction._budget as _budget

    stored = []
    monkeypatch.setattr(
        _budget, '_store_tool_result_artifact',
        lambda content, **kwargs: (
            stored.append((content, kwargs['user_id']))
            or 'tool-result:' + 'a' * 64))
    big = 'evidence line\n' * 20_000
    visible = cap_tool_result(
        big, 'read_files', owner_user_id=TEST_OWNER_USER_ID)
    value = json.loads(visible)
    assert _budget._model_result_tokens(visible, '') <= 8_000
    assert value['status'] == 'partial' and value['truncated'] is True
    assert value['artifactRef'] == 'tool-result:' + 'a' * 64
    assert stored == [(big, TEST_OWNER_USER_ID)]


def test_cap_tool_result_store_failure_is_honest_and_has_no_path(monkeypatch):
    from lib.paper.tools import cap_tool_result
    import lib.tasks_pkg.compaction._budget as _budget

    monkeypatch.setattr(
        _budget, '_store_tool_result_artifact',
        lambda *_args, **_kwargs: '')
    out = cap_tool_result(
        'x' * 70_000, 'fetch_url', owner_user_id=TEST_OWNER_USER_ID)
    value = json.loads(out)
    assert value['truncated'] is True
    assert value['artifactRef'] == '' and value['cursor'] == ''
    assert 'Full result unavailable' in value['summary']
    assert '[Persisted to:' not in out and '/tmp/' not in out


def test_paper_round_budget_handles_duplicate_and_empty_call_ids(monkeypatch):
    from lib.paper.tools import PaperToolResultBudgetV2
    import lib.tasks_pkg.compaction._budget as _budget

    monkeypatch.setattr(
        _budget, '_store_tool_result_artifact',
        lambda *_args, **_kwargs: 'tool-result:' + 'b' * 64)
    messages = []
    entries = []
    budget = PaperToolResultBudgetV2(
        owner_user_id=TEST_OWNER_USER_ID)
    call_ids = ['', 'duplicate', 'duplicate', 'x', 'y', 'z']
    for index, call_id in enumerate(call_ids):
        entry = {}
        entries.append(entry)
        budget.append(
            messages, round_index=3, tool_name='web_search',
            tool_call_id=call_id,
            content=(f'value-{index} ' * 4_800), round_entry=entry)
    budget.finish_round(3)

    assert len(messages) == len(call_ids)
    assert [message['tool_call_id'] for message in messages] == call_ids
    assert sum(_budget._result_tokens(message['content'], '')
               for message in messages) <= 24_000
    assert any(entry.get('aggregateResultBudgetApplied') for entry in entries)
    assert all('contractVersion' not in json.loads(message['content'])
               for message in messages)
    assert all(entry['toolResultEvidence']['resultContractVersion']
               == 'tofu.tool-result/v2' for entry in entries)


def test_paper_batch_read_budget_consumes_fair_projection(
        tmp_path, monkeypatch):
    from lib.paper.tools import PaperToolResultBudgetV2
    from lib.project_mod.read_tools import tool_read_files
    from lib.tools.result_projection import TOOL_RESULT_PROJECTION_ITEMS_KEY
    import lib.tasks_pkg.compaction._budget as _budget

    (tmp_path / 'first.py').write_text(
        'FIRST_PAPER_SENTINEL\n' + 'paper filler\n' * 12_000)
    (tmp_path / 'second.py').write_text('SECOND_PAPER_SENTINEL\n')
    (tmp_path / 'third.py').write_text('THIRD_PAPER_SENTINEL\n')
    arguments = {'reads': [
        {'path': 'first.py'}, {'path': 'second.py'}, {'path': 'third.py'},
    ]}
    projection_items = []
    raw = tool_read_files(
        str(tmp_path), arguments['reads'], result_items=projection_items)
    monkeypatch.setattr(
        _budget, '_store_tool_result_artifact',
        lambda *_args, **_kwargs: 'tool-result:' + 'q' * 64)
    round_entry = {TOOL_RESULT_PROJECTION_ITEMS_KEY: projection_items}
    messages = []

    PaperToolResultBudgetV2(owner_user_id=TEST_OWNER_USER_ID).append(
        messages, round_index=1, tool_name='read_files',
        tool_call_id='paper-batch-read', content=raw,
        round_entry=round_entry, tool_arguments=arguments)

    value = json.loads(messages[0]['content'])
    assert [item['path'] for item in value['items']] == [
        'first.py', 'second.py', 'third.py']
    assert 'SECOND_PAPER_SENTINEL' in value['items'][1]['preview']
    assert 'THIRD_PAPER_SENTINEL' in value['items'][2]['preview']
    assert TOOL_RESULT_PROJECTION_ITEMS_KEY not in round_entry


def test_explicit_legacy_result_arm_does_not_silently_use_v2():
    from lib.paper.tools import PaperToolResultBudgetV2
    from lib.tasks_pkg.compaction._constants import (
        _SINGLE_RESULT_HARD_CEILING_CHARS,
    )

    messages = []
    round_entry = {}
    budget = PaperToolResultBudgetV2(
        owner_user_id=TEST_OWNER_USER_ID,
        result_envelope='legacy',
        conv_id='paper-result-control')
    visible = budget.append(
        messages, round_index=0, tool_name='web_search',
        tool_call_id='legacy-call', content='LEGACY RAW RESULT',
        round_entry=round_entry)
    budget.finish_round(0)

    assert visible == 'LEGACY RAW RESULT'
    assert messages[0]['content'] == 'LEGACY RAW RESULT'
    assert round_entry['resultContract'] == 'legacy'
    assert budget.telemetry()['resultEnvelope'] == 'legacy'
    assert 'tofu.tool-result/v2' not in messages[0]['content']

    oversized_messages = []
    oversized = 'bounded legacy evidence\n' * (
        _SINGLE_RESULT_HARD_CEILING_CHARS // 24 + 2_000)
    bounded = budget.append(
        oversized_messages, round_index=1, tool_name='read_files',
        tool_call_id='legacy-large-read', content=oversized)
    assert len(bounded) <= _SINGLE_RESULT_HARD_CEILING_CHARS
    assert len(bounded) < len(oversized)


def test_research_executor_reads_owner_artifact_through_shared_dispatch(
        monkeypatch):
    from lib.agent_loop import AbortSignal
    from lib.paper.tools import (
        PaperToolResultBudgetV2,
        build_research_tool_schemas,
        freeze_paper_tool_epoch,
        make_paper_exec_shim,
        make_research_tool_executor,
    )
    import lib.tasks_pkg.handlers.tool_result_artifacts as artifact_handler

    artifact_ref = 'tool-result:' + 'c' * 64

    class Repository:
        def read_range(self, **kwargs):
            assert kwargs['user_id'] == TEST_OWNER_USER_ID
            assert kwargs['artifact_ref'] == artifact_ref
            return {'artifactRef': artifact_ref, 'content': 'recover-me',
                    'nextCursor': None, 'truncated': False}

    monkeypatch.setattr(
        artifact_handler, 'ToolResultArtifactRepository', Repository)
    schemas, contracts = freeze_paper_tool_epoch(
        build_research_tool_schemas(), owner_user_id=TEST_OWNER_USER_ID)
    assert 'read_tool_artifact' in _names(schemas)
    shim = make_paper_exec_shim(
        task_id='paper_artifact_continue',
        owner_user_id=TEST_OWNER_USER_ID,
        tool_contract_documents_by_name=contracts)
    messages = []
    result_budget = PaperToolResultBudgetV2(
        owner_user_id=TEST_OWNER_USER_ID)
    execute = make_research_tool_executor(
        messages, user_question='recover evidence',
        abort_signal=AbortSignal.never(), result_budget=result_budget,
        contract_documents_for_round=lambda _rnd: contracts,
        exec_shim=shim)
    execute(0, {
        'id': 'tc_artifact',
        'function': {'name': 'read_tool_artifact', 'arguments': json.dumps({
            'artifact_ref': artifact_ref, 'cursor': '0', 'limit': 8192,
        })},
    })
    result_budget.finish_round(0)

    value = json.loads(messages[0]['content'])
    assert 'contractVersion' not in value
    assert value['status'] == 'ok'
    assert value['content'] == 'recover-me'


# ─── 5. Engine-level e2e: the report loop really runs read_files ──────────


def test_report_engine_explicit_control_keeps_legacy_full_wire():
    import lib.paper.report_engine.worker as re_mod
    from lib.paper.report_runtime import _new_report_task

    seen = {}

    def _fake_dispatch(messages, on_content=None, **kwargs):
        seen['messages'] = list(messages)
        seen['tools'] = list(kwargs.get('tools') or [])
        content = '# Control Report\n\n## ⚡ TL;DR\nControl arm completed.'
        if on_content:
            on_content(content)
        return ({'role': 'assistant', 'content': content, 'tool_calls': []},
                'stop', {'_dispatch': {}})

    original = re_mod.dispatch_stream
    re_mod.dispatch_stream = _fake_dispatch
    try:
        task = _new_report_task(
            'rpt_tool_surface_control',
            'phashpapersurfacecontrol0000000000', 'en', 'kimi-k3',
            config={
                'tools': {'schemaBudgetTokens': 0},
                'paperInsightEnabled': False,
                'paperCheckpointsEnabled': False,
            },
            user_id=TEST_OWNER_USER_ID,
        )
        re_mod.run_report_task(task, [
            {'role': 'system', 'content': 'control system'},
            {'role': 'user', 'content': 'paper text'},
        ], [])
    finally:
        re_mod.dispatch_stream = original

    names = set(_names(seen['tools']))
    assert task['status'] == 'done', task.get('error')
    assert task['toolEpochV2']['configuredSchemaBudgetTokens'] == 0
    assert task['toolEpochV2']['wireToolCount'] == 21
    assert 'search_tools' not in names and 'execute_tools' not in names
    assert seen['messages'][0]['content'] == 'control system'


def test_report_engine_full_loop_executes_read_files(tmp_path):
    import lib.paper.report_engine.worker as re_mod
    from lib.paper.report_runtime import _new_report_task

    asset = tmp_path / 'fetched_paper_asset.txt'
    asset.write_text('STAGED-ASSET-CONTENT: derivation details the model needs.',
                     encoding='utf-8')

    seen_messages = []
    plan = [
        ('', [{'id': 'tc_read',
               'function': {'name': 'read_files',
                            'arguments': json.dumps({'path': str(asset)})}}]),
        ('# Staged Paper\n\n## ⚡ TL;DR\nThe asset says the derivation works.', []),
    ]

    def _fake_dispatch(messages, on_content=None, on_thinking=None, **kw):
        seen_messages.append(list(messages))
        content, tool_calls = plan.pop(0)
        if content and on_content:
            on_content(content)
        return ({'role': 'assistant', 'content': content,
                 'tool_calls': tool_calls},
                ('tool_calls' if tool_calls else 'stop'),
                {'_dispatch': {}})

    orig = re_mod.dispatch_stream
    re_mod.dispatch_stream = _fake_dispatch
    try:
        task = _new_report_task('rpt_full_tools_e2e', 'phashfulltools0000000000000000e2e',
                                'en', 'kimi-k3', client_title='Staged Paper',
                                # offline suite — the insight second pass must
                                # not dispatch a real LLM (CI 401 → endless
                                # cooldown cycle → 600s timeout, 233daa6)
                                config={
                                    'responses': {'promptProfile': 'full'},
                                    'paperInsightEnabled': False,
                                    'paperCheckpointsEnabled': False,
                                }, user_id=TEST_OWNER_USER_ID)
        re_mod.run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper text'},
        ], [])
    finally:
        re_mod.dispatch_stream = orig

    assert task['status'] == 'done', task.get('error')
    assert task['toolEpochV2']['configuredSchemaBudgetTokens'] == 0
    assert task['toolEpochV2']['wireSchemaTokens'] > 4_000
    assert task['toolEpochV2']['wireToolCount'] > 0
    assert (task['toolEpochV2']['wireToolCount'] ==
            task['toolEpochV2']['executableToolCount'])
    assert len(task['tool_rounds']) == 1
    assert task['tool_rounds'][0]['toolName'] == 'read_files'
    assert task['tool_rounds'][0]['status'] == 'done'
    assert task['tool_rounds'][0]['results'], 'read_files round has no display meta'
    types = [e.get('type') for e in task['events']]
    assert 'tool_start' in types and 'tool_done' in types and 'done' in types
    # The evidence must have been fed back inside the bounded V2 envelope.
    final_round_msgs = seen_messages[-1]
    tool_msgs = [m for m in final_round_msgs if m.get('role') == 'tool']
    assert tool_msgs, 'tool result was not fed back into the loop'
    assert any('STAGED-ASSET-CONTENT' in m.get('content', '')
               for m in tool_msgs), 'staged asset content missing from tool message'
    assert 'derivation works' in (task.get('enriched_text') or task['full_text'])


def test_qa_engine_full_loop_executes_read_files(tmp_path):
    import lib.paper.qa_engine as qe
    from lib.paper.qa_runtime import _new_qa_task

    asset = tmp_path / 'qa_asset.txt'
    asset.write_text('QA-ASSET: the answer is 42.', encoding='utf-8')

    plan = [
        ('', [{'id': 'tc_q',
               'function': {'name': 'read_files',
                            'arguments': json.dumps({'path': str(asset)})}}]),
        ('The answer is 42.', []),
    ]
    seen_messages = []

    def _fake_dispatch(messages, on_content=None, **kw):
        seen_messages.append(list(messages))
        content, tool_calls = plan.pop(0)
        if content and on_content:
            on_content(content)
        return ({'role': 'assistant', 'content': content,
                 'tool_calls': tool_calls},
                ('tool_calls' if tool_calls else 'stop'), {'_dispatch': {}})

    orig = qe.dispatch_stream
    qe.dispatch_stream = _fake_dispatch
    try:
        task = _new_qa_task('qa_full_tools_e2e', 'phashqafulltools0000000000000e2e',
                            'en', 'kimi-k3', question='what is in the asset?', user_id=TEST_OWNER_USER_ID)
        qe._run_qa_task(task, [{'role': 'system', 'content': 'sys'},
                               {'role': 'user', 'content': 'what is in the asset?'}])
    finally:
        qe.dispatch_stream = orig

    assert task['status'] == 'done', task.get('error')
    assert task['toolEpochV2']['configuredSchemaBudgetTokens'] == 0
    assert task['toolEpochV2']['wireSchemaTokens'] > 4_000
    assert task['toolEpochV2']['wireToolCount'] > 0
    assert (task['toolEpochV2']['wireToolCount'] ==
            task['toolEpochV2']['executableToolCount'])
    assert task['tool_rounds'][0]['toolName'] == 'read_files'
    assert '42' in task['full_text']
    # Complement pin: the asset content must have been fed back as a tool
    # message (a scripted final answer alone can never prove execution).
    final_msgs = seen_messages[-1]
    tool_msgs = [m for m in final_msgs if m.get('role') == 'tool']
    assert tool_msgs and any('QA-ASSET' in m.get('content', '')
                             for m in tool_msgs), \
        'read_files result was not fed back into the QA loop'
