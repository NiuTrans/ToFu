#!/usr/bin/env python3
"""Paper full-tool-set guards (2026-07-28).

The paper report + Q&A engines now ship the FULL chat-tier tool set (not just
web_search / fetch_url), assembled through the SHARED registry and executed
through the SHARED single-tool dispatch. This suite pins the four contracts:

  1. PARITY — the paper full set is byte-equivalent to what
     ``assemble_tool_list`` produces for the chat-tier flag profile, so the
     two modes can never drift again (chat gains a tool → paper gains it).
  2. ROUTING — non-search tool calls (read_files / todo_write / run_command→
     code_exec) execute through ``_execute_tool_one`` with the paper event /
     display schema preserved; research-only engines keep ``Unknown tool``.
  3. HONEST BOUNDING — oversized results spill to disk (readable via
     read_files) for full-set engines, and carry an explicit TRUNCATED marker
     for research-only engines. The silent 30k slice is gone.
  4. POLICY — write-partition calls in an unattended paper engine are
     auto-approved AND audit-logged (never silently bypassed, never blocked
     on a human who cannot answer).

NEUTER map (each mutation was verified to turn the named test(s) red):
  * _build_full_tool_schemas → research set only ........... parity tests
  * _execute_shared_tool → early 'Unknown tool' return ..... routing tests
  * drop the run_command→code_exec flip .................... code_exec routing
  * drop audit_log in _execute_shared_tool ................. auto-approve test
  * cap_tool_result → identity (no spill / no marker) ...... bounding tests
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit


def _names(tools):
    return sorted(t['function']['name'] for t in tools)


# ─── 1. Parity: paper full set == chat-tier registry assembly ────────────

def test_full_tool_set_matches_chat_tier_registry():
    from lib.paper.prompts import _FULL_REPORT_TOOLS, _build_full_tool_schemas
    paper_names = _names(_FULL_REPORT_TOOLS)

    # Independently assemble what chat mode gets with the chat-tier flags and
    # no project — the two MUST be identical, today and after any future
    # registry change (that's the whole point of routing through the chassis).
    from lib.tools import ToolContext, assemble_tool_list, resolve_enabled_plugins
    cfg = {}
    ctx = ToolContext(
        cfg=cfg, task_id='', project_path='', project_enabled=False,
        search_mode='multi', search_enabled=True, fetch_enabled=True,
        code_exec_enabled=True,
        browser_enabled=False, desktop_enabled=False, swarm_enabled=False,
        enabled_plugins=resolve_enabled_plugins(cfg),
    )
    chat_tools, _ = assemble_tool_list(ctx)
    assert paper_names == _names(chat_tools), \
        f'paper full set drifted from chat tier: {paper_names} vs {_names(chat_tools)}'

    # Core pins (belt against the profile itself being gutted):
    for core in ('web_search', 'fetch_url', 'read_files', 'inspect_image',
                 'run_command', 'create_memory', 'search_memories',
                 'activate_skill', 'todo_write', 'schedule_create'):
        assert core in paper_names, f'{core} missing from paper full set'
    # Project-write family must stay gated OFF (no project attached).
    for gated in ('write_file', 'apply_diff', 'list_dir', 'grep_search'):
        assert gated not in paper_names, f'{gated} leaked into project-less set'
    # The lazy list must not be a frozen import-time snapshot.
    assert _names(_build_full_tool_schemas()) == paper_names


def test_research_set_stays_narrow():
    """insight / recommend / ideate / survey keep search+fetch ONLY."""
    from lib.paper.prompts import _REPORT_TOOLS
    assert _names(_REPORT_TOOLS) == ['fetch_url', 'web_search']


# ─── 2b. The repair schema index must see BUILDER-produced schemas ────────

def test_repair_index_covers_builder_produced_schemas(monkeypatch):
    """_build_schema_index walks lib.tools attrs — static dicts/lists only.
    When a tool's schema becomes runtime-BUILT (build_search_tool), the walk
    must still index it or the bare-string-args repair for that tool silently
    dies (the '507 searches' regression class). Pins the builder branch."""
    import lib.tools as tools_mod
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
    from lib.paper.tools import make_paper_exec_shim
    return make_paper_exec_shim(task_id=task_id)


def _round_entry(name, tc_id='tc_1'):
    return {'roundNum': 1, 'toolName': name, 'query': name,
            'toolCallId': tc_id, 'status': 'searching', 'results': None}


def test_shared_dispatch_read_files_reads_real_file(tmp_path):
    from lib.paper.tools import _execute_report_tool
    target = tmp_path / 'staged_asset.txt'
    body = 'paper full-tools e2e sentinel: the-model-must-see-this\n' * 3
    target.write_text(body, encoding='utf-8')

    shim = _shim()
    re_ = _round_entry('read_files')
    args = json.dumps({'path': str(target)})
    content, display, diag, ebkdn, verts = _execute_report_tool(
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
    from lib.paper.tools import _execute_report_tool
    content, display, *_ = _execute_report_tool(
        'read_files', '{"path": "/etc/hostname"}')
    assert content == 'Unknown tool: read_files'
    assert display == []


def test_shared_dispatch_todo_write_runs_and_finalizes():
    from lib.paper.tools import _execute_report_tool
    shim = _shim()
    re_ = _round_entry('todo_write')
    args = json.dumps({'todos': [{'id': '1', 'content': 'scan literature',
                                  'status': 'in_progress'}]})
    content, display, *_ = _execute_report_tool(
        'todo_write', args, exec_shim=shim, round_entry=re_)
    assert 'Unknown tool' not in content and 'Error' not in content[:40]
    assert shim.get('_todos'), 'todo_write must persist the checklist on the shim'
    assert re_['status'] == 'done' and re_['results']
    assert re_['results'][0].get('source') == 'Checklist'


def test_run_command_routes_to_code_exec_special_handler():
    """run_command in a project-less engine must hit __code_exec__, not the
    project handler (which would die with 'No project path')."""
    from lib.paper.tools import _execute_report_tool, paper_effective_tool_name
    shim = _shim()
    re_ = _round_entry('code_exec')   # the engine pre-flips the display name
    args = json.dumps({'command': 'echo paper-full-tools-routing-ok'})
    content, display, *_ = _execute_report_tool(
        'run_command', args, exec_shim=shim, round_entry=re_)
    assert 'paper-full-tools-routing-ok' in content, content[:300]
    assert 'No project path' not in content
    assert paper_effective_tool_name('run_command') == 'code_exec'
    meta = (re_['results'] or [{}])[0]
    assert meta.get('toolName') == 'code_exec'
    assert str(meta.get('exitCode')) == '0'


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

    from lib.paper.tools import _execute_report_tool
    shim = _shim()
    re_ = _round_entry('create_memory')
    args = json.dumps({
        'name': 'paper-autoapprove-probe',
        'description': 'auto-approval audit guard probe memory',
        'body': '## Why\nprobe body',
        'scope': 'global',
    })
    content, display, *_ = _execute_report_tool(
        'create_memory', args, exec_shim=shim, round_entry=re_)

    assert 'Memory created' in content, f'write did not execute: {content[:300]!r}'
    assert any(k == 'paper_tool_auto_approve' and kw.get('tool') == 'create_memory'
               for k, kw in audits), \
        f'auto-approval was NOT audited: {audits!r}'
    # read-only tools must NOT trigger the write audit.
    audits.clear()
    _execute_report_tool('todo_write', '{"todos": []}',
                         exec_shim=_shim(), round_entry=_round_entry('todo_write'))
    assert not audits, f'read-only tool spuriously audited: {audits!r}'


# ─── 4. Honest bounding (spill vs explicit truncation) ────────────────────

def test_cap_tool_result_passthrough_under_cap():
    from lib.paper.tools import cap_tool_result
    assert cap_tool_result('short', 'read_files', can_read=True) == 'short'
    assert cap_tool_result('short', 'web_search', can_read=False) == 'short'


def test_cap_tool_result_read_files_is_budget_exempt():
    """TOOL_RESULT_MAX_CHARS['read_files'] == 0 = chat's L0 exemption: a huge
    read_files result flows whole (never spilled, never truncated)."""
    from lib.paper.tools import cap_tool_result
    big = 'z' * 70_000
    assert cap_tool_result(big, 'read_files', can_read=True) == big


def test_cap_tool_result_spills_to_disk_with_read_pointer(
        tmp_path, monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.compaction._persist._PERSIST_DIR_BASE',
                        str(tmp_path))
    from lib.paper.tools import cap_tool_result
    big = 'x' * 70_000   # over fetch_url's 50k budget
    out = cap_tool_result(big, 'fetch_url', 'tc_spill',
                          conv_id='paper-testhash', can_read=True)
    assert out != big and len(out) < len(big)
    assert '[Persisted to:' in out, f'no spill pointer: {out[:200]!r}'
    assert 'read_files' in out, 'the pointer must name the tool that opens it'
    persisted = list(tmp_path.rglob('fetch_url_*.txt'))
    assert persisted, f'no spill file under {tmp_path}'


def test_cap_tool_result_explicit_truncation_marker_for_narrow_engine():
    from lib.paper.tools import cap_tool_result
    big = 'y' * 70_000
    out = cap_tool_result(big, 'fetch_url', 'tc_narrow', can_read=False)
    assert len(out) < len(big)
    assert '[TRUNCATED' in out and '70,000' in out
    assert 'no read_files' in out, 'marker must say WHY it cannot page back'
    assert 'Do NOT fabricate' in out


# ─── 5. Engine-level e2e: the report loop really runs read_files ──────────

def test_report_engine_full_loop_executes_read_files(tmp_path):
    import lib.paper.report_engine as re_mod
    from lib.paper import _new_report_task

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
                                'en', None, client_title='Staged Paper',
                                # offline suite — the insight second pass must
                                # not dispatch a real LLM (CI 401 → endless
                                # cooldown cycle → 600s timeout, 233daa6)
                                config={'paperInsightEnabled': False})
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper text'},
        ], [])
    finally:
        re_mod.dispatch_stream = orig

    assert task['status'] == 'done', task.get('error')
    assert len(task['tool_rounds']) == 1
    assert task['tool_rounds'][0]['toolName'] == 'read_files'
    assert task['tool_rounds'][0]['status'] == 'done'
    assert task['tool_rounds'][0]['results'], 'read_files round has no display meta'
    types = [e.get('type') for e in task['events']]
    assert 'tool_start' in types and 'tool_done' in types and 'done' in types
    # The tool result must have been fed back to the model verbatim.
    final_round_msgs = seen_messages[-1]
    tool_msgs = [m for m in final_round_msgs if m.get('role') == 'tool']
    assert tool_msgs, 'tool result was not fed back into the loop'
    assert any('STAGED-ASSET-CONTENT' in m.get('content', '')
               for m in tool_msgs), 'staged asset content missing from tool message'
    assert 'derivation works' in (task.get('enriched_text') or task['full_text'])


def test_qa_engine_full_loop_executes_read_files(tmp_path):
    import lib.paper.qa_engine as qe
    from lib.paper import _new_qa_task

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
                            'en', None, question='what is in the asset?')
        qe._run_qa_task(task, [{'role': 'system', 'content': 'sys'},
                               {'role': 'user', 'content': 'what is in the asset?'}])
    finally:
        qe.dispatch_stream = orig

    assert task['status'] == 'done', task.get('error')
    assert task['tool_rounds'][0]['toolName'] == 'read_files'
    assert '42' in task['full_text']
    # Complement pin: the asset content must have been fed back as a tool
    # message (a scripted final answer alone can never prove execution).
    final_msgs = seen_messages[-1]
    tool_msgs = [m for m in final_msgs if m.get('role') == 'tool']
    assert tool_msgs and any('QA-ASSET' in m.get('content', '')
                             for m in tool_msgs), \
        'read_files result was not fed back into the QA loop'
