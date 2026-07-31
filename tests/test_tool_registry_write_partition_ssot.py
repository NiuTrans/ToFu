"""Tool-registry SSOT guard — provides coverage + write-partition completeness.

Two invariants, both with a measured production consequence:

1. **provides coverage** — every tool name with a live handler must be
   declared by exactly one ``ToolSpec.provides``. The registry is supposed to
   be the single source of truth for "what tools exist"; an undeclared handler
   is invisible to the partition tables, to the custom-tool collision check
   (``lib/tools/tool_env.py``), and to any future audit that enumerates specs.

2. **write-partition completeness** — every STATE-CHANGING tool must appear in
   some ``ToolSpec.write_tools``. This is not cosmetic. In
   ``lib/tasks_pkg/tool_dispatch/_pipeline.py`` the Manual write-approval gate
   is derived *directly* from the per-task write partition::

       needs_approval = fn_name in _write_tools and _attended and not auto_apply …

   and an attended task defaults ``auto_apply=False``. So a state-changing tool
   absent from the partition (a) never prompts the user for approval and
   (b) runs in the PARALLEL dispatch pool instead of the serial write phase.
   Before this guard, ``browser_execute_js`` (arbitrary JS in the user's
   browser), ``schedule_create``/``timer_create`` (persistent background jobs),
   ``store_artifact`` and ``project_charter_commit`` all had that shape.

Both invariants are asserted against the REAL registry (no hand-mirrored
list), so a newly added tool that forgets to declare itself fails here rather
than silently bypassing the approval gate in production.
"""

import pytest

pytestmark = pytest.mark.unit


def _all_specs():
    from lib.tools import all_specs
    return list(all_specs())


def _declared_provides():
    d = set()
    for s in _all_specs():
        d |= set(s.provides)
    return d


def _declared_write_tools():
    w = set()
    for s in _all_specs():
        w |= set(s.write_tools)
    return w


# ── Tool-name inventories, pulled from the schema modules themselves ──
# These are the LLM-visible surfaces; importing them (rather than retyping the
# names) means a rename upstream shows up here as a failure, not as a stale
# literal that quietly still passes.

def _browser_names():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
    from lib.tools import BROWSER_TOOL_NAMES
    return set(BROWSER_TOOL_NAMES) | set(ADVANCED_BROWSER_TOOL_NAMES)


def _scheduler_names():
    from lib.scheduler.tool_defs import SCHEDULER_TOOLS
    return {t['function']['name'] for t in SCHEDULER_TOOLS}


def _memory_names():
    from lib.memory import ALL_MEMORY_TOOLS
    return {t['function']['name'] for t in ALL_MEMORY_TOOLS}


#: Tools that MUTATE state outside the model's context and therefore must be
#: approval-eligible + serially dispatched. Each entry names the mutation so a
#: future reader can judge the classification rather than trusting the list.
STATE_CHANGING_EXPECTATIONS = {
    # browser — drives the user's real browser session
    'browser_execute_js':   'runs arbitrary JS in the user page',
    'browser_navigate':     'changes what page the user is on',
    'browser_click':        'activates page controls (may submit/purchase)',
    'browser_fill_form':    'types into the user page',
    'browser_keyboard':     'synthetic keystrokes into the user page',
    'browser_hover_and_click': 'activates page controls',
    'browser_right_click_menu': 'activates context-menu actions',
    'browser_create_tab':   'opens a tab in the user browser',
    'browser_close_tab':    'closes a tab (can discard user work)',
    # scheduler — persists background jobs that outlive the turn
    'schedule_create':      'persists a recurring background job',
    'schedule_manage':      'mutates/deletes an existing scheduled job',
    'timer_create':         'persists a polling watcher',
    'timer_manage':         'cancels an existing watcher',
    # memory CRUD (already partitioned — pinned so it cannot regress out)
    'create_memory':        'writes a memory file',
    'update_memory':        'rewrites a memory file',
    'delete_memory':        'deletes a memory file',
    'merge_memories':       'deletes N memories, writes 1',
    # project brain — charter commit is shared project-wide intent
    'project_charter_commit': 'appends a decision every sibling conv reads',
}

#: Read-only counterparts that must NOT be dragged into the write partition —
#: partitioning them would serialize + prompt on harmless reads.
MUST_STAY_READ_ONLY = {
    'browser_read_tab', 'browser_list_tabs', 'browser_screenshot',
    'browser_get_cookies', 'browser_get_history', 'browser_get_app_state',
    'browser_get_interactive_elements', 'browser_summarize_page',
    'browser_wait', 'browser_hover',
    'schedule_list', 'await_task',
    'search_memories',
    'project_charter_read', 'project_board_read', 'project_peer_status',
    'list_conversations', 'get_conversation',
}


class TestProvidesCoverage:
    def test_browser_spec_declares_its_tools(self):
        """The browser spec shipped provides=∅ while registering 19 tools."""
        assert _browser_names() <= _declared_provides()

    def test_scheduler_spec_declares_its_tools(self):
        assert _scheduler_names() <= _declared_provides()

    def test_memory_spec_declares_its_tools(self):
        assert _memory_names() <= _declared_provides()

    def test_charter_commit_is_declared(self):
        """project_charter_commit has a handler but was in no provides set."""
        assert 'project_charter_commit' in _declared_provides()

    def test_no_spec_declares_a_name_twice(self):
        """Two specs claiming one name makes the owning spec ambiguous."""
        seen, dupes = set(), set()
        for s in _all_specs():
            for n in s.provides:
                if n in seen:
                    dupes.add(n)
                seen.add(n)
        assert not dupes, f'tool names declared by 2+ specs: {sorted(dupes)}'

    def test_artifact_tools_are_declared(self):
        """Sub-agent-only tools still need declaring.

        store/read/list_artifact(s) never enter the MASTER schema (they are
        injected per-sub-agent by SubAgent._inject_artifact_tools), but they DO
        have handlers on the main dispatch registry via SWARM_TOOL_NAMES — so
        an undeclared name is invisible to the partition tables and to the
        custom-tool collision check.
        """
        d = _declared_provides()
        for n in ('store_artifact', 'read_artifact', 'list_artifacts'):
            assert n in d


class TestFullCoverageRatchet:
    """Every registered handler must be declared by some spec.

    This is the invariant the individual per-family tests generalize: the
    registry is the single source of truth for "what tools exist". Measured
    at 90 handlers vs 55 declared (gap 35) before this epic.
    """

    #: Names allowed to have a handler with no ``provides`` entry, each for a
    #: STRUCTURAL reason — not "we didn't get to it yet".
    EXEMPT = {
        # Dispatched by round metadata via handler_special, never by fn_name,
        # so it is not a tool name the model can call. See
        # ToolSpec.handler_special / @tool_registry.special.
        '__code_exec__',
    }

    def test_every_handler_is_declared(self):
        import lib.tasks_pkg.handlers  # noqa: F401 — registers the handlers
        from lib.tasks_pkg.executor import tool_registry
        handlers = {n for n, _c, _d in tool_registry.list_tools()}
        undeclared = handlers - _declared_provides() - self.EXEMPT
        assert not undeclared, (
            f'{len(undeclared)} handler(s) have no ToolSpec.provides entry: '
            f'{sorted(undeclared)}. Declare them on the owning spec (and add '
            f'to write_tools if they mutate state) — an undeclared handler is '
            f'invisible to the write/idempotent partitions AND to the '
            f'custom-tool collision check in lib/tools/tool_env.py. If a name '
            f'is structurally exempt (special dispatch key), add it to EXEMPT '
            f'with the reason.'
        )

    def test_exemptions_still_apply(self):
        """An exemption that stopped being real is a silent coverage hole."""
        import lib.tasks_pkg.handlers  # noqa: F401
        from lib.tasks_pkg.executor import tool_registry
        handlers = {n for n, _c, _d in tool_registry.list_tools()}
        stale = self.EXEMPT - handlers
        assert not stale, (
            f'EXEMPT names no longer registered: {sorted(stale)} — drop them')


class TestWritePartitionCompleteness:
    @pytest.mark.parametrize('tool,why', sorted(STATE_CHANGING_EXPECTATIONS.items()))
    def test_state_changing_tool_is_in_write_partition(self, tool, why):
        assert tool in _declared_write_tools(), (
            f'{tool} ({why}) is NOT in any ToolSpec.write_tools — it therefore '
            f'skips the Manual write-approval gate AND runs in the parallel '
            f'pool. See _pipeline.py: needs_approval = fn_name in _write_tools.'
        )

    @pytest.mark.parametrize('tool', sorted(MUST_STAY_READ_ONLY))
    def test_read_only_tool_stays_out_of_write_partition(self, tool):
        assert tool not in _declared_write_tools(), (
            f'{tool} is read-only; putting it in the write partition would '
            f'serialize it and prompt the user for approval on a plain read.'
        )

    def test_partition_reaches_the_live_dispatch_table(self):
        """Spec declarations must actually land in the dispatch-side union.

        Guards the seam between the registry and _flags.py — a declaration
        that never reaches _WRITE_TOOLS protects nothing.
        """
        from lib.tasks_pkg.tool_dispatch._flags import _WRITE_TOOLS
        for tool in STATE_CHANGING_EXPECTATIONS:
            assert tool in _WRITE_TOOLS, (
                f'{tool} declared in a spec but missing from the live '
                f'_WRITE_TOOLS union'
            )

    def test_write_tools_union_is_independent_of_provides(self):
        """write_tools must be honoured even when provides is empty.

        Pinned because the fix declares BOTH; if someone later assumes
        write_tools is filtered through provides, memory (which shipped
        write_tools with provides=∅ and still gated correctly) proves
        otherwise.
        """
        from lib.tasks_pkg.tool_dispatch._flags import _registry_tool_flags
        write, _idem = _registry_tool_flags()
        assert _declared_write_tools() <= set(write)


class TestApprovalMetaCoverage:
    """Every partitioned tool whose args carry the RISK must show them.

    A write-approval prompt renders from ``approval_meta``; with no enricher
    the user sees the bare tool name and approves blind — worse than not
    prompting, because it manufactures false confidence.
    """

    @pytest.mark.parametrize('tool', [
        'browser_execute_js', 'browser_navigate', 'browser_fill_form',
        'schedule_create', 'schedule_manage', 'timer_create',
        'project_charter_commit',
    ])
    def test_enricher_exists(self, tool):
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
        assert tool in _APPROVAL_META_ENRICHERS

    def test_execute_js_enricher_surfaces_the_code(self):
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
        meta = {}
        _APPROVAL_META_ENRICHERS['browser_execute_js'](
            meta, {'code': 'document.querySelector("#pay").click()'})
        blob = ' '.join(str(v) for v in meta.values())
        assert '#pay' in blob, 'approval prompt would hide the JS being run'

    def test_schedule_create_enricher_surfaces_cron_and_command(self):
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
        meta = {}
        _APPROVAL_META_ENRICHERS['schedule_create'](
            meta, {'name': 'nightly', 'schedule': '0 3 * * *',
                   'command': 'rm -rf /tmp/cache', 'task_type': 'command'})
        blob = ' '.join(str(v) for v in meta.values())
        assert '0 3 * * *' in blob and 'rm -rf /tmp/cache' in blob

    def test_enricher_tolerates_missing_args(self):
        """Enrichers run on model-supplied args — a missing key must not raise
        inside the approval path (that would abort the gate itself)."""
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
        for tool in ('browser_execute_js', 'schedule_create',
                     'project_charter_commit'):
            _APPROVAL_META_ENRICHERS[tool]({}, {})
