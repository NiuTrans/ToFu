"""tests/tasks_pkg/test_approval_dialog_renders_risk.py — the write-approval dialog MUST
show WHAT it is asking the user to approve.

WHY THIS EXISTS (the criterion this file replaces)
--------------------------------------------------
An earlier ratchet asserted ``fn_name in _APPROVAL_META_ENRICHERS`` — i.e. "an
enricher exists". That was the WRONG criterion and it was measurably green while
the product was broken: the renderer only drew a
detail block for shapes it recognises, so an enricher writing some other key
produced a dialog with NO detail at all. 8 of the first 15 enrichers were in
that state, including ``browser_execute_js``, whose own docstring promised the
JS body was "surfaced verbatim" — it wrote ``search`` without ``replace``, the
search+replace branch never fired, and the code was invisible. The user saw a
bare tool name plus Approve/Reject and approved blind.

So the criterion here is the RESULT, per the charter's behaviour-guard rule:
feed a realistic argument dict through the REAL enricher, render with the REAL
frontend function, and assert the risk-bearing argument appears in the HTML.

DISCIPLINE (charter: "绿着的守卫在测一段从未存在过的代码")
--------------------------------------------------------
* The renderer is bundled from the real typed owner and its real dependencies
  at run time — never re-implemented or sliced out of a retained source file.
  A copy would decouple on the first refactor and keep asserting a world that
  no longer ships.
* The tool list comes from the LIVE write partition ∩ ``provides``, so a newly
  partitioned write tool joins this guard automatically instead of needing a
  literal added here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_OWNER_JS = Path(native_module_path(
    '.native/tool-approval-risk-contract.js',
    Path(_REPO)
    / 'frontend/src/conversation/presentation/'
    / 'tool-approval-presentation.ts',
))


def _write_partition_tools() -> list[str]:
    """Model-visible write tools, from the LIVE partition (not a literal list).

    ``desktop_move_file`` is in the desktop spec's ``write_tools`` but NOT in
    ``provides`` — the model cannot call it, so it can never reach this dialog.
    """
    import lib.tasks_pkg.handlers  # noqa: F401 — registration side-effect
    from lib.tasks_pkg.tool_dispatch._flags import _WRITE_TOOLS
    from lib.tools.registry import all_specs

    provides: set[str] = set()
    for s in all_specs():
        if s.source != 'plugin':
            provides |= set(s.provides)
    return sorted(t for t in _WRITE_TOOLS if t in provides)


#: Realistic args per write tool + the ONE substring that MUST reach the user.
#: The probe value is deliberately alarming: if the dialog cannot show
#: ``rm -rf ~/Documents``, it cannot show anything that matters.
CASES: dict[str, tuple[dict, str]] = {
    'browser_check': ({'tab_id': '7', 'selector': '#subscribe', 'checked': False}, '#subscribe'),
    'browser_select_option': ({'tab_id': '7', 'selector': '#region', 'option': 'Europe'}, 'Europe'),
    'consider_project_message': ({'message_id': 'message-7', 'decision': 'accepted', 'note': 'Run scoped parser check', 'expected_revision': 1}, 'Run scoped parser check'),
    'create_conversation': ({
        'title': 'Review parser migration', 'idempotency_key': 'create-7',
    }, 'Review parser migration'),
    'dispatch_project_assignment': ({
        'assignment_id': 'assignment-7', 'expected_revision': 2,
    }, 'assignment-7'),
    'delegate_project_work': ({
        'title': 'Parser review', 'brief': 'Inspect parser changes and report failing cases',
        'idempotency_key': 'delegate-7',
    }, 'Inspect parser changes and report failing cases'),
    'cancel_project_work': ({'assignment_id': 'assignment-7'}, 'assignment-7'),
    'review_project_work': ({
        'assignment_id': 'assignment-7', 'result_revision': 2,
        'decision': 'rework', 'note': 'Repair the missing timeout case',
    }, 'Repair the missing timeout case'),
    'finish_project_request': ({
        'report_turn_id': 'report-turn-7', 'outcome': 'complete',
    }, 'report-turn-7'),
    'report_to_user': ({
        'title': 'Parser verified', 'message': 'All parser checks passed',
        'kind': 'delivery', 'idempotency_key': 'report-7',
    }, 'All parser checks passed'),
    'evolution_explore': ({'action': 'read', 'program_id': 'program-7'}, 'program-7'),
    'manage_agents': ({'action': 'create', 'name': 'Parser reviewer', 'objective': 'Inspect parser changes'}, 'Inspect parser changes'),
    'publish_response_file': ({'path': 'reports/audit.pdf', 'title': 'Audit'}, 'reports/audit.pdf'),
    'publish_response_asset': ({'path': 'charts/chart.png', 'alt': 'Quarterly revenue'}, 'charts/chart.png'),
    'publish_rich_response': ({'payload': {'version': 1, 'blocks': [{'type': 'markdown', 'text': 'Revenue report'}]}}, 'Revenue report'),
    'send_agent_message': ({'agent_id': 'agent-7', 'content': 'Inspect parser changes', 'idempotency_key': 'test-msg-7'}, 'Inspect parser changes'),
    'send_project_message': ({'conversation_id': 'conversation-7', 'content': 'Inspect parser changes', 'idempotency_key': 'test-project-msg-7'}, 'Inspect parser changes'),

    'record_working_fact': ({'revision': 3, 'text': 'The payment was accepted',
                            'kind': 'derived', 'sources': [{'kind': 'fact', 'id': 'wf_source', 'version': 1}]},
                           'The payment was accepted'),
    'run_command': ({'command': 'rm -rf /tmp/victim'}, 'rm -rf /tmp/victim'),
    'request_skill_install': ({
        'catalog_id': 'skill-creator',
        'scope': 'global',
        'overwrite': True,
        'reason': 'Generate and install a task-specific workflow',
    }, 'skill-creator'),
    'paper_import_arxiv': ({'arxiv': '2301.12345'}, '2301.12345'),
    'write_file': ({'path': 'a.py', 'content': 'PAYLOAD_X'}, 'PAYLOAD_X'),
    'edit_file': ({'description': 'replace dangerous code', 'edits': [{
        'path': 'a.py', 'operation': 'replace',
        'anchor': 'OLD_X', 'content': 'NEW_X',
    }]}, 'OLD_X'),
    'apply_diff': ({'path': 'a.py', 'search': 'OLD_X', 'replace': 'NEW_X'}, 'OLD_X'),
    'apply_diffs': ({'edits': [{'path': 'a.py', 'search': 'OLD_X',
                                'replace': 'NEW_X'}]}, 'OLD_X'),
    'insert_content': ({'path': 'a.py', 'anchor': 'ANCH_X',
                        'content': 'INS_X'}, 'ANCH_X'),
    'insert_contents': ({'edits': [{'path': 'a.py', 'anchor': 'ANCH_X',
                                    'content': 'INS_X'}]}, 'ANCH_X'),
    'browser_execute_js': ({'tab_id': '7',
                            'code': 'document.querySelector("#pay").click()'},
                           '#pay'),
    'browser_devtools': ({'tab_id': '7', 'action': 'evaluate',
                          'expression': 'document.querySelector("#pay").click()'},
                         '#pay'),
    'browser_navigate': ({'url': 'https://evil.example/pay'}, 'evil.example'),
    'kill_background_command': ({'command_id': 'bg-rm-rf-victim'}, 'bg-rm-rf-victim'),
    'evolution_artifact': ({
        'paths': ['research/packet.json', 'research/validation.txt'],
        'delivery': 'The experiment failed its preregistered test.',
    }, 'research/validation.txt'),
    'evolution_coordinate': ({
        'action': 'assign',
        'content': 'run rm -rf /tmp/victim',
        'source_seq': 3,
    }, 'rm -rf /tmp/victim'),
    'evolution_material': ({
        'action': 'save', 'kind': 'paper', 'title': 'X',
        'url': 'https://evil.example/x',
    }, 'evil.example'),
    'evolution_teach': ({
        'name': 'shell-wipe', 'description': 'wipe disks fast',
        'knowledge': 'run rm -rf / first', 'examples': ['rm -rf /tmp/victim'],
    }, 'rm -rf /tmp/victim'),
    'evolution_reindex': ({
        'queries': ['KV cache compression survey'],
        'reason': 'rebuild after pruning 40 archived goals',
    }, 'rebuild after pruning 40 archived goals'),
    'kill': ({'id': 'cmd-42'}, 'cmd-42'),
    'paper_cite_insert': ({
        'doi': '10.1038/nature14539', 'anchor': 'attention mechanism',
    }, '10.1038/nature14539'),
    'browser_research_page': ({
        'url': 'https://notes.example.com/skills/skills-market',
        'max_scrolls': 8,
        'max_pages': 5,
        'pagination': 'auto',
    }, 'notes.example.com'),
    'browser_site': ({'site': 'notes', 'operation': 'search',
                      'params': {'query': 'Claude'}}, 'Claude'),
    'browser_fill_form': ({'fields': {'#card': '4111111111111111'}},
                          '4111111111111111'),
    'browser_click': ({'tab_id': '7', 'selector': '#confirm-purchase'},
                      '#confirm-purchase'),
    # v2 surface (pt_869e5648403e4745) — the retired names (keyboard,
    # create_tab, hover_and_click, right_click_menu) left the write partition
    # with the consolidation, so they left this table with it.
    'browser_type': ({'tab_id': '7', 'text': 'Card number',
                      'value': '4111111111111111'}, '4111111111111111'),
    'browser_press_key': ({'tab_id': '7', 'keys': 'Control+Shift+Delete'},
                          'Control+Shift+Delete'),
    'browser_menu_click': ({'tab_id': '7', 'target_text': 'File',
                            'item_text': 'Delete Forever'}, 'Delete Forever'),
    'browser_close_tab': ({'tab_ids': ['7', '8']}, '7'),
    'desktop_run_command': ({'command': 'rm -rf ~/Documents'}, 'rm -rf ~/Documents'),
    'desktop_write_file': ({'path': '~/.bashrc', 'content': 'EVIL_PAYLOAD'},
                           'EVIL_PAYLOAD'),
    'desktop_open_file': ({'path': '~/secret_X.pdf'}, 'secret_X.pdf'),
    'desktop_open_app': ({'app': 'Terminal_X', 'args': ['-e', 'rm -rf ~']},
                         'Terminal_X'),
    'create_memory': ({'name': 'n', 'description': 'd', 'body': 'BODY_X'}, 'BODY_X'),
    'update_memory': ({'memory_id': 'mem-42-X', 'body': 'NEW_X'}, 'mem-42-X'),
    'delete_memory': ({'memory_id': 'important-note-X'}, 'important-note-X'),
    'merge_memories': ({'memory_ids': ['m1-X', 'm2-X'], 'name': 'n',
                        'description': 'd', 'body': 'B'}, 'm1-X'),
    'motion_video_render': ({'project_dir': 'p', 'output': 'out_X.mp4'},
                            'out_X.mp4'),
    'motion_video_concat': ({'inputs': ['a.mp4'], 'output': 'final_X.mp4'},
                            'final_X.mp4'),
    'motion_video_mux': ({'video': 'v.mp4', 'audio': 'a.wav',
                          'output': 'final_X.mp4'}, 'final_X.mp4'),
    'motion_video_narrate': ({'scenes_path': 's.json', 'out_dir': 'audio_X/'},
                             'audio_X/'),
    'schedule_create': ({'name': 'n', 'schedule': '0 3 * * *',
                         'command': 'rm -rf /tmp/X', 'task_type': 'command'},
                        'rm -rf /tmp/X'),
    'schedule_manage': ({'action': 'delete', 'task_id': 'task-X'}, 'task-X'),
    'timer_create': ({'check_command': 'grep DONE f',
                      'continuation_message': 'CONT_X'}, 'CONT_X'),
    'timer_manage': ({'action': 'cancel', 'timer_id': 'timer-X'}, 'timer-X'),
    'local_serve_deploy': ({
        'model_path': '/models/Acme-X',
        'engine': 'vllm',
    }, '/models/Acme-X'),
    'local_serve_stop': ({'instance_id': 'local-stop-X'}, 'local-stop-X'),
    'local_serve_remove': ({'instance_id': 'local-remove-X'}, 'local-remove-X'),
    'project_charter_commit': ({'decision': 'DECISION_X'}, 'DECISION_X'),
    'update_search_settings': ({'profile': 'deep',
                                'overrides': {'fetch_top_n': 9}},
                               'fetch_top_n'),
    'call_mcp_write_tool': ({
        'name': 'mcp__github__delete_issue',
        'arguments': {'issue_id': 'issue-DELETE-X'},
    }, 'issue-DELETE-X'),
}


def _live_schema_properties() -> dict[str, set[str]]:
    """tool name → its REAL JSON-schema parameter names, from the registry.

    Built by assembling the tool list the way a task does, so the property
    names come from the schemas actually shipped to the model rather than from
    anyone's memory of them.
    """
    import lib.tasks_pkg.handlers  # noqa: F401 — registration side-effect
    from lib.tools.registry import ToolContext, assemble_tool_list

    ctx = ToolContext(
        cfg={'projectEnabled': True, 'evolution': {
            'stage': 'executing', 'run_id': 'run-schema', 'project_path': '/tmp/p'}},
        task_id='t-schema', conv_id='conv-schema', owner_user_id=7,
        project_path='/tmp/p', project_enabled=True,
        search_mode='multi', search_enabled=True, fetch_enabled=True,
        code_exec_enabled=False, browser_enabled=True, desktop_enabled=True,
        image_gen_enabled=True,
        human_guidance_enabled=True, scheduler_enabled=True, messages=[],
    )
    out: dict[str, set[str]] = {}
    try:
        tool_list, _ = assemble_tool_list(ctx)
    except Exception:  # pragma: no cover — degrade to "unknown", never abort
        return out
    for t in tool_list or []:
        fn = (t or {}).get('function') or {}
        name = fn.get('name')
        if not name:
            continue
        props = ((fn.get('parameters') or {}).get('properties') or {})
        out[name] = set(props)
    # browser / desktop families are gated on a live extension/agent being
    # connected, so they may be absent from the assembled list in CI. Pull
    # their schemas directly — they are plain module constants.
    from lib.tools.working_facts import WORKING_FACT_CONTRACTS
    for contract in WORKING_FACT_CONTRACTS:
        out[contract.name] = set(contract.parameters.get('properties', {}))
    for mod, attr in (('lib.tools.browser', 'BROWSER_TOOLS'),
                      ('lib.browser.advanced', 'ADVANCED_BROWSER_TOOLS'),
                      ('lib.desktop_tools', 'DESKTOP_TOOLS'),
                      ('lib.tools.project', 'PROJECT_TOOLS_LEGACY'),
                      ('lib.tools.project', 'PROJECT_TOOL_KILL_BACKGROUND'),
                      ('lib.tools.project', 'PROJECT_TOOLS_UNIFIED'),
                      ('lib.tools.motion_video', 'MOTION_VIDEO_TOOLS'),
                      ('lib.tools.paper_build', 'PAPER_ARXIV_IMPORT_TOOL'),
                      ('lib.tools.paper_citations', 'PAPER_CITE_SEARCH_TOOL'),
                      ('lib.tools.paper_citations', 'PAPER_CITE_INSERT_TOOL'),
                      ('lib.tools.paper_citations', 'PAPER_CITATION_AUDIT_TOOL'),
                      ('lib.tools.paper_edits', 'PAPER_PROPOSE_EDITS_TOOL'),
                      ('lib.tools.paper_quality', 'PAPER_CHECK_TOOL'),
                      ('lib.tools.paper_quality', 'PAPER_REVIEW_TOOL'),
                      ('lib.tools.paper_quality', 'PAPER_SCAFFOLD_TOOL'),
                      ('lib.tools.poll', 'POLL_TOOL'),
                      ('lib.tools.poll', 'KILL_TOOL'),
                      ('lib.local_serve.tool_defs', 'LOCAL_SERVE_TOOLS'),
                      ('lib.memory', 'ALL_MEMORY_TOOLS'),
                      ('lib.scheduler.tool_defs', 'SCHEDULER_TOOLS')):
        try:
            import importlib
            group = getattr(importlib.import_module(mod), attr, None) or []
        except Exception:  # pragma: no cover
            continue
        if isinstance(group, dict):
            group = [group]
        for t in group:
            fn = (t or {}).get('function') or {}
            name = fn.get('name')
            if name and name not in out:
                props = ((fn.get('parameters') or {}).get('properties') or {})
                out[name] = set(props)
    # Evolution tools are exposure-gated on a task-level evolution binding, so
    # the plain context assembly never includes them; pull their definitions
    # from the owning modules the way the gated builder does.
    try:
        from lib.evolution.artifact_evidence import build as _artifact_build
        from lib.evolution import coordination as _evo_coordination
        from lib.evolution import library as _evo_library
        from lib.evolution.refetch import REINDEX_TOOL_DEFINITION
        from lib.evolution.teach import TEACH_TOOL_DEFINITION
        from lib.evolution.exploration_tools import tool_definition as exploration_definition
        for definition in (
                *_artifact_build(ctx),
                _evo_coordination.tool_definition(),
                _evo_library.material_tool_definition(),
                REINDEX_TOOL_DEFINITION,
                TEACH_TOOL_DEFINITION, exploration_definition()):
            fn = (definition or {}).get('function') or {}
            name = fn.get('name')
            if name and name not in out:
                props = ((fn.get('parameters') or {}).get('properties') or {})
                out[name] = set(props)
    except Exception:  # pragma: no cover
        pass
    return out


def _build_meta(tool: str, args: dict) -> dict:
    """Run the REAL enricher over the REAL base meta shape."""
    from lib.tasks_pkg.tool_dispatch._approval import enrich_approval_metadata

    meta = {
        'approvalId': 'appr-1',
        'toolName': tool,
        'path': args.get('path', ''),
        'description': args.get('description', ''),
    }
    enrich_approval_metadata(meta, tool, args)
    return meta


def _render(metas: dict[str, dict]) -> dict[str, str]:
    """Render each meta with the bundled typed owner under node."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available')

    harness = """
'use strict';
eval(process.env.OWNER_SOURCE);
const presentation = createToolApprovalPresentation({
  translate: (key) => key,
});
const INPUT = JSON.parse(process.env.APPROVAL_METAS);
const out = {};
for (const [tool, meta] of Object.entries(INPUT)) {
  const round = { status: 'pending_approval', approvalId: meta.approvalId,
                  approvalMeta: meta, toolName: tool };
  out[tool] = presentation.renderApprovalHtml(round, {
    iconHtml: '<svg></svg>', queryHtml: tool,
  });
}
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run([node, '-e', harness], capture_output=True,
                          text=True, timeout=120, env={
                              **os.environ,
                              'OWNER_SOURCE': _OWNER_JS.read_text(encoding='utf-8'),
                              'APPROVAL_METAS': json.dumps(
                                  metas, ensure_ascii=False),
                          })
    assert proc.returncode == 0, (
        'node failed rendering with the bundled typed approval owner '
        f'({os.path.relpath(_OWNER_JS, _REPO)}):\n{proc.stderr[-3000:]}'
    )
    return json.loads(proc.stdout)


def _detail_body(html: str) -> str:
    """The part of the dialog ABOVE the Approve/Reject buttons.

    Asserting against the whole blob would pass on the button labels alone; the
    question is whether a DETAIL block was drawn.
    """
    marker = 'ptool-approval-btns'
    idx = html.find(marker)
    return html[:idx] if idx != -1 else html


class TestEveryWriteToolShowsItsRisk:
    def test_case_table_covers_the_live_write_partition(self):
        """No write tool may quietly escape this guard.

        If a tool enters the write partition without a case here, that is
        exactly the blind-approval hole reopening.
        """
        missing = [t for t in _write_partition_tools() if t not in CASES]
        assert not missing, (
            f'{len(missing)} write tool(s) have no render case: {missing}. Add '
            f'realistic args + the risk substring to CASES so the dialog is '
            f'proven to show them.'
        )

    def test_case_args_exist_in_the_real_tool_schema(self):
        """CASES arg names MUST be real schema parameters.

        Closes the co-drift hole: ``CASES`` is hand-written, and so is every
        ``fn_args.get('selector')`` inside an enricher. If a tool renames a
        parameter (``selector`` → ``target``), the enricher reads ``None``,
        ``_risk`` drops the empty field, the dialog silently loses a row — and
        the render assertions above STAY GREEN, because the stale probe and the
        stale getter agree with each other. Two wrongs validating each other is
        exactly the charter's third failure mode ("a green guard testing code
        that never shipped").

        Anchoring on the LIVE schema breaks that symmetry: a rename fails here
        immediately, naming the tool and the vanished parameter.
        """
        schemas = _live_schema_properties()
        if not schemas:
            pytest.skip('tool schemas unavailable in this environment')

        unknown, unchecked = [], []
        for tool, (args, _probe) in sorted(CASES.items()):
            props = schemas.get(tool)
            if props is None:
                unchecked.append(tool)
                continue
            for key in args:
                if key not in props:
                    unknown.append((tool, key, sorted(props)))

        assert not unknown, (
            'CASES uses argument name(s) that do NOT exist in the live tool '
            'schema — the sample args (and probably the enricher reading them) '
            'are stale:\n' + '\n'.join(
                f'  {t}: {k!r} not in schema; real params = {p}'
                for t, k, p in unknown)
        )
        # Not a hard failure: a family gated on a disconnected extension can be
        # legitimately absent. Surfaced so it cannot silently become "all of
        # them" and hollow out this check.
        assert len(unchecked) <= 4, (
            f'{len(unchecked)} tools had no resolvable schema, so their sample '
            f'args went unverified: {unchecked}. If this list is growing, the '
            f'schema resolver is failing rather than the tools being gated.'
        )

    def test_dialog_renders_a_detail_block_and_the_risk_value(self):
        """THE contract: the user sees WHAT they are approving."""
        tools = [t for t in _write_partition_tools() if t in CASES]
        metas = {t: _build_meta(t, CASES[t][0]) for t in tools}
        rendered = _render(metas)

        blank, hidden = [], []
        for t in tools:
            body = _detail_body(rendered[t])
            if 'ptool-diff-preview' not in body and 'ptool-batch-preview' not in body:
                blank.append(t)
                continue
            probe = CASES[t][1]
            # The renderer HTML-escapes values, so compare against an escaped
            # probe too (e.g. quotes inside a JS snippet).
            esc = (probe.replace('&', '&amp;').replace('<', '&lt;')
                        .replace('>', '&gt;').replace('"', '&quot;')
                        .replace("'", '&#39;'))
            if probe not in body and esc not in body:
                hidden.append((t, probe))

        assert not blank, (
            f'{len(blank)} write tool(s) render NO detail block — the user gets '
            f'a bare tool name plus Approve/Reject: {blank}. Use _risk() in the '
            f'enricher so riskFields is populated.'
        )
        assert not hidden, (
            f'{len(hidden)} write tool(s) render a detail block that OMITS the '
            f'risk-bearing argument: {hidden}. The dialog looks informative but '
            f'hides the thing being approved.'
        )

    def test_the_generic_shape_is_what_carries_most_tools(self):
        """Pins that the fix is the GENERIC branch, not 33 bespoke shapes.

        If someone later removes ``riskFields`` and reverts to per-family keys,
        this fails — the point of the branch is that a new write tool needs no
        frontend change.
        """
        tools = [t for t in _write_partition_tools() if t in CASES]
        via_risk = [t for t in tools
                    if _build_meta(t, CASES[t][0]).get('riskFields')]
        assert len(via_risk) >= 20, (
            f'only {len(via_risk)} tools use the generic riskFields shape; the '
            f'generic branch is meant to carry the bulk of the write partition '
            f'so new tools need zero frontend work.'
        )

    def test_renderer_opts_out_cleanly_when_there_is_no_approval(self):
        """No approvalId → return "" so the caller falls through.

        The call site (``if (approvalHtml) return approvalHtml;``) treats the
        empty string as "not my case" and tries the timer / stdin renderers.
        Emitting a buttons-only shell here would hijack every non-approval
        round, so "" is the contract — asserted so a future refactor cannot
        turn the opt-out into a stray empty dialog.
        """
        rendered = _render({'run_command': {}})
        assert rendered['run_command'] == '', (
            'renderer must return "" for a round with no approvalId; the '
            'caller relies on falsy to fall through to other row renderers'
        )

    def test_renderer_survives_an_approval_with_no_meta(self):
        """A cold/legacy round (approvalId but no approvalMeta) still renders.

        Recovered-from-restart rounds can carry the id without the meta blob;
        the dialog must still offer Approve/Reject rather than throwing.
        """
        rendered = _render({'run_command': {'approvalId': 'a1'}})
        assert 'ptool-approval-btns' in rendered['run_command']


def test_artifact_approval_renders_delivery_and_independent_review():
    meta = _build_meta('evolution_artifact', CASES['evolution_artifact'][0])
    html = _render({'evolution_artifact': meta})['evolution_artifact']
    assert 'The experiment failed its preregistered test.' in html
    assert 'independent review decides acceptance' in html
    assert 'research/packet.json' in html


def test_generic_plugin_preview_renders_exact_build_and_resource_scope():
    """An independently installed write tool discloses risk without a core case."""
    import html
    from lib.tasks_pkg.tool_dispatch._approval import enrich_approval_metadata
    args = {
        'operation_id': 'operation',
        'plan': {'provider': 'custom_platform', 'spec': {
            'image': {'instructions': 'RUN echo REVIEWED_BUILD'},
            'workspace': {'cpu': 8, 'memory_gib': 28, 'queue': 'reviewed-queue'},
        }},
    }
    meta = {'approvalId': 'plugin-approval', 'toolName': 'environment_apply'}
    enrich_approval_metadata(meta, 'environment_apply', args)
    rendered = html.unescape(_render({'environment_apply': meta})['environment_apply'])
    assert 'REVIEWED_BUILD' in rendered and 'reviewed-queue' in rendered
    assert 'memory_gib' in rendered and 'custom_platform' in rendered
