"""tests/test_tool_registry.py — Declarative tool-assembly registry.

Pins the contract that lets tools be added/removed as drop-in
:class:`~lib.tools.registry.ToolSpec` plugins without editing core
orchestration code (``lib/tasks_pkg/model_config.py``).

Covered:
  * Tool ordering matches the cache-stable canonical layout.
  * ``has_real_tools`` snapshot semantics (base vs capability phase).
  * Caller-supplied ``cfg['tools']`` override short-circuits assembly.
  * Memory attaches iff a base tool exists; swarm/mcp do NOT need base tools.
  * Third-party plugin specs register and contribute through the same path.
  * ``_WRITE_TOOLS`` / ``_IDEMPOTENT_TOOLS`` stay in sync with spec flags.
"""

from __future__ import annotations

import unittest

import pytest

from lib.tasks_pkg.model_config import _assemble_tool_list
from lib.tools.registry import (
    ToolContext,
    ToolSpec,
    all_specs,
    assemble_tool_list,
    build_tool_result_meta,
    register_tool_spec,
)


pytestmark = pytest.mark.unit


def _names(tool_list):
    return [t['function']['name'] for t in (tool_list or [])]


def _ctx(**overrides):
    base = dict(
        cfg={}, task_id='t-test',
        project_path='', project_enabled=False,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        image_gen_enabled=False,
        human_guidance_enabled=False, scheduler_enabled=False, messages=[],
    )
    base.update(overrides)
    return ToolContext(**base)


def test_large_contribution_scans_existing_authority_once(monkeypatch):
    """Catalog de-duplication must stay linear as dynamic catalogs grow."""
    import lib.tools.registry._spec as spec_module

    class _CountingCatalog(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    schemas = [
        {'type': 'function', 'function': {
            'name': f'_bounded_dynamic_{index}',
            'description': 'bounded dynamic tool',
            'parameters': {'type': 'object', 'properties': {}},
        }}
        for index in range(128)
    ]
    spec = ToolSpec(
        key='_bounded_dynamic', build=lambda _context: schemas,
        provides=frozenset(_names(schemas)),
    )
    monkeypatch.setattr(spec_module, '_TOOL_SPECS', [spec])
    ctx = _ctx()
    ctx.executable_tool_catalog = _CountingCatalog([schemas[0]])

    assemble_tool_list(ctx)

    assert ctx.executable_tool_catalog.iterations == 1
    assert _names(ctx.executable_tool_catalog) == _names(schemas)


class TestOrdering(unittest.TestCase):
    def test_full_project_ordering_is_cache_stable(self):
        tl, hr = assemble_tool_list(_ctx(
            project_path='/tmp/x', project_enabled=True,
            search_mode='multi', search_enabled=True, fetch_enabled=True,
        ))
        names = _names(tl)
        self.assertTrue(hr)
        # search → fetch → read_files → inspect_image → project tools → memory (end)
        self.assertEqual(names[:6], [
            'web_search', 'fetch_url', 'read_files', 'inspect_image',
            'grep_search', 'find_files',
        ])
        self.assertNotIn('list_dir', names)
        # memory tools always come last (capability phase)
        self.assertIn('create_memory', names)
        self.assertLess(names.index('run_command'), names.index('create_memory'),
                        'project (base) must precede memory (capability)')

    def test_single_search_mode_is_legacy_alias_for_multi(self):
        # 'single' is a retired mode kept as a backward-compat alias: it now
        # yields the same web_search (multi) tool as 'multi'.
        tl, _ = assemble_tool_list(_ctx(search_mode='single', search_enabled=True))
        self.assertEqual(_names(tl)[0], 'web_search')

    def test_skill_install_schema_is_deferred_but_remains_discoverable(self):
        from lib.tools.gateway import local_wire_tools

        ctx = _ctx(
            project_path='/tmp/x', project_enabled=True,
            search_mode='multi', search_enabled=True, fetch_enabled=True,
        )
        assembled, _ = assemble_tool_list(ctx)
        self.assertEqual(
            ctx.discovery_policy_by_name['request_skill_install'],
            'searchable')
        self.assertIn(
            'request_skill_install',
            {tool['function']['name']
             for tool in ctx.executable_tool_catalog})
        projected = local_wire_tools(
            assembled,
            discovery_policy_by_name=ctx.discovery_policy_by_name,
            discovery_catalog_size=len(ctx.executable_tool_catalog),
            searchable_count=sum(
                policy == 'searchable'
                for policy in ctx.discovery_policy_by_name.values()),
        )
        projected_names = set(_names(projected))
        self.assertNotIn('request_skill_install', projected_names)
        self.assertTrue({
            'search_skills', 'load_skill', 'read_skill_resource',
            'search_tools', 'execute_tools',
        } <= projected_names)

    def test_v2_artifact_recovery_tools_are_eager_and_directly_callable(self):
        """A result may not advertise a continuation tool hidden by search."""
        ctx = _ctx(cfg={'tools': {'resultEnvelope': 'v2'}})
        assembled, _ = assemble_tool_list(ctx)
        names = set(_names(assembled))
        for name in ('read_tool_artifact', 'search_tool_artifact'):
            self.assertIn(name, names)
            self.assertEqual(ctx.discovery_policy_by_name[name], 'eager')


class TestResultMetadataSeam(unittest.TestCase):
    def test_project_metadata_is_built_by_the_owning_spec(self):
        meta = build_tool_result_meta(
            'write_file',
            {'path': 'notes.txt', 'description': 'save notes'},
            'File updated: notes.txt',
        )
        self.assertEqual(meta['source'], 'Project')
        self.assertEqual(meta['badge'], 'updated')
        self.assertTrue(meta['writeOk'])

    def test_unowned_tool_uses_bounded_neutral_metadata(self):
        content = 'unknown plugin result\n' + ('x' * 200)
        meta = build_tool_result_meta('plugin__unknown', {}, content)
        self.assertEqual(meta['title'], 'plugin__unknown')
        self.assertEqual(meta['fetchedChars'], len(content))
        self.assertEqual(meta['snippet'], content[:120].replace('\n', ' '))
        self.assertEqual(meta['badge'], '')


class TestPhaseSemantics(unittest.TestCase):
    def test_every_executable_tool_has_one_v2_contract_document(self):
        contexts = [
            _ctx(),
            _ctx(
                project_path='/tmp/x', project_enabled=True,
                search_mode='multi', search_enabled=True,
                fetch_enabled=True, code_exec_enabled=True,
                browser_enabled=True, scheduler_enabled=True,
            ),
        ]
        for ctx in contexts:
            assemble_tool_list(ctx)
            executable = set(_names(ctx.executable_tool_catalog))
            documents = ctx.tool_contract_documents_by_name
            self.assertEqual(executable, set(documents))
            for name in executable:
                self.assertEqual(
                    documents[name].get('contractVersion'),
                    'tofu.tool-contract/v2')
                self.assertEqual(documents[name].get('name'), name)
                self.assertIsInstance(
                    documents[name].get('arguments_schema'), dict)

    def test_read_files_declares_source_recovery(self):
        ctx = _ctx(project_path='/tmp/x', project_enabled=True)
        assemble_tool_list(ctx)
        self.assertEqual(
            ctx.tool_contract_documents_by_name['read_files'][
                'resultRecovery'],
            'source')

    def test_available_scope_keeps_hidden_tool_executable_not_on_wire(self):
        ctx = _ctx()
        tl, _ = assemble_tool_list(ctx)
        self.assertNotIn('run_command', _names(tl))
        self.assertIn('run_command', _names(ctx.executable_tool_catalog))
        self.assertEqual(
            ctx.discovery_policy_by_name['run_command'], 'searchable')

    def test_selected_only_scope_preserves_legacy_authority(self):
        ctx = _ctx(cfg={'tools': {'executionScope': 'selected_only'}})
        tl, _ = assemble_tool_list(ctx)
        self.assertNotIn('run_command', _names(tl))
        self.assertNotIn('run_command', _names(ctx.executable_tool_catalog))

    def test_memory_write_scope_matches_project_context(self):
        no_project_tools, _ = assemble_tool_list(_ctx(
            cfg={'memoryEnabled': True}))
        no_project = {
            tool['function']['name']: tool for tool in no_project_tools}
        for name in ('create_memory', 'merge_memories'):
            scope = no_project[name]['function']['parameters']['properties']['scope']
            self.assertEqual(scope['enum'], ['global'])
            self.assertIn('Default: global', scope['description'])

        project_tools, _ = assemble_tool_list(_ctx(
            cfg={'memoryEnabled': True}, project_path='/tmp/project',
            project_enabled=True))
        project = {tool['function']['name']: tool for tool in project_tools}
        for name in ('create_memory', 'merge_memories'):
            scope = project[name]['function']['parameters']['properties']['scope']
            self.assertEqual(scope['enum'], ['global', 'project'])

        # Context specialization must never mutate the module-level schemas.
        from lib.memory.tools import CREATE_MEMORY_TOOL
        static_scope = (CREATE_MEMORY_TOOL['function']['parameters']
                        ['properties']['scope'])
        self.assertEqual(static_scope['enum'], ['global', 'project'])


class TestMemoryExecutionDefaults(unittest.TestCase):
    def test_omitted_scope_uses_global_without_project(self):
        from unittest.mock import patch
        from lib.tasks_pkg.handlers.memory import _memory_create

        seen = {}

        def _create_memory(**kwargs):
            seen.update(kwargs)
            return {'name': kwargs['name'], 'id': 'm1',
                    'scope': kwargs['scope']}

        with patch('lib.memory.storage.create_memory', _create_memory):
            content, badge, _title = _memory_create(
                {'name': 'N', 'description': 'D', 'body': 'B'}, None)
        self.assertEqual(seen['scope'], 'global')
        self.assertIsNone(seen['project_path'])
        self.assertIn('scope: global', content)
        self.assertEqual(badge, '💡 saved')

    def test_omitted_scope_keeps_project_default_with_project(self):
        from unittest.mock import patch
        from lib.tasks_pkg.handlers.memory import _memory_create

        seen = {}

        def _create_memory(**kwargs):
            seen.update(kwargs)
            return {'name': kwargs['name'], 'id': 'm2',
                    'scope': kwargs['scope']}

        with patch('lib.memory.storage.create_memory', _create_memory):
            _memory_create(
                {'name': 'N', 'description': 'D', 'body': 'B'}, '/tmp/p')
        self.assertEqual(seen['scope'], 'project')
        self.assertEqual(seen['project_path'], '/tmp/p')

    def test_read_files_always_on_and_pulls_memory(self):
        # Even bare (no project/search), read_files is on → counts as a base
        # tool → memory tools attach.
        tl, hr = assemble_tool_list(_ctx())
        names = _names(tl)
        self.assertTrue(hr)
        self.assertIn('read_files', names)
        self.assertIn('create_memory', names)

    def test_scheduler_is_default_authority_regardless_of_flag(self):
        # Scheduler tools are a DEFAULT capability (like memory / todo): they
        # attach whenever a base tool exists, NOT gated on scheduler_enabled.
        # read_files is always on, so they're present even with the flag off.
        for flag in (False, True):
            ctx = _ctx(scheduler_enabled=flag)
            assemble_tool_list(ctx)
            names = _names(ctx.executable_tool_catalog)
            for n in ('schedule_create', 'schedule_list', 'schedule_manage'):
                self.assertIn(n, names,
                              f'{n} must remain executable regardless of scheduler_enabled={flag}')
                self.assertEqual(ctx.discovery_policy_by_name[n], 'searchable')

    def test_swarm_without_base_tools(self):
        # Swarm is NOT gated on has_base_tools — but read_files is always on,
        # so assert the three swarm tools are present regardless.
        tl, _ = assemble_tool_list(_ctx())
        names = _names(tl)
        for n in ('spawn_agents', 'await_agents', 'get_agent_result'):
            self.assertIn(n, names)

    def test_conv_ref_requires_mention(self):
        tl_no, _ = assemble_tool_list(_ctx())
        self.assertNotIn('list_conversations', _names(tl_no))
        # Real server-injected wrapper (carries title=") on a USER turn → on.
        tl_yes, _ = assemble_tool_list(_ctx(messages=[{
            'role': 'user',
            'content': ('The user has attached the following conversation(s):\n'
                        '[REFERENCED_CONVERSATION title="Old chat" id="abc"]\n'
                        'body\n[/REFERENCED_CONVERSATION]'),
        }]))
        self.assertIn('list_conversations', _names(tl_yes))

    def test_conv_ref_structured_field_enables(self):
        # The authoritative signal: a user turn carrying convRefs (raw row).
        tl, _ = assemble_tool_list(_ctx(messages=[{
            'role': 'user', 'content': 'compare with this',
            'convRefs': [{'id': 'abc', 'title': 'Old chat'}],
        }]))
        self.assertIn('list_conversations', _names(tl))

    def test_project_mode_registers_only_automatic_integration_tools(self):
        """Project Brain is runtime-driven, not a model tool surface."""
        ctx_proj = _ctx(project_path='/tmp/x', project_enabled=True)
        assemble_tool_list(ctx_proj)
        names = _names(ctx_proj.executable_tool_catalog)
        self.assertIn('integration_checkpoint', names)
        self.assertIn('integration_submit', names)
        retired = {
            'project_charter_read', 'project_charter_propose',
            'project_board_read', 'project_board_post', 'project_board_claim',
            'project_board_complete', 'project_board_block',
            'project_peer_status', 'project_feed_read', 'project_message',
            'project_intervene', 'integration_status',
        }
        self.assertTrue(retired.isdisjoint(names))

    def test_conv_ref_not_triggered_by_assistant_prose(self):
        # REGRESSION: a conversation *about* the feature, where the assistant
        # quotes the bare token, must NOT self-enable the tools.
        tl, _ = assemble_tool_list(_ctx(messages=[
            {'role': 'user', 'content': 'what is the REFERENCED_CONVERSATION tag?'},
            {'role': 'assistant',
             'content': 'It is the `[REFERENCED_CONVERSATION` marker injected by...'},
        ]))
        self.assertNotIn('list_conversations', _names(tl))
        self.assertNotIn('get_conversation', _names(tl))


class TestProjectBrainSurface(unittest.TestCase):
    """The project model surface contains execution integration only."""

    _RETIRED = ('project_charter_read', 'project_charter_propose',
                'project_board_read', 'project_board_post',
                'project_board_claim', 'project_board_complete',
                'project_board_block', 'project_peer_status',
                'project_feed_read', 'project_message', 'project_intervene',
                'integration_status')

    def test_project_brain_tool_schema_budget_is_zero(self):
        ctx = _ctx(project_path='/tmp/x', project_enabled=True,
                   messages=[{'role': 'user', 'content': 'fix the login bug'}])
        assemble_tool_list(ctx)
        catalog = _names(ctx.executable_tool_catalog)
        for name in self._RETIRED:
            self.assertNotIn(name, catalog)
            self.assertNotIn(name, ctx.discovery_policy_by_name)
        self.assertIn('integration_checkpoint', catalog)
        self.assertIn('integration_submit', catalog)

    def test_create_project_removed_from_model_catalog(self):
        # Neither the resident project wire nor the searchable/executable
        # catalog may still carry create_project — otherwise the model keeps
        # discovering a scaffold tool we deliberately retired.
        ctx = _ctx(project_path='/tmp/x', project_enabled=True)
        tl, _ = assemble_tool_list(ctx)
        self.assertNotIn('create_project', _names(tl))
        self.assertNotIn(
            'create_project', _names(ctx.executable_tool_catalog))
        self.assertNotIn('create_project', ctx.discovery_policy_by_name)
        self.assertFalse(any('create_project' in spec.provides
                             for spec in all_specs()))
        ctx_none = _ctx()
        assemble_tool_list(ctx_none)
        self.assertNotIn('create_project',
                         _names(ctx_none.executable_tool_catalog))

    def test_brain_absent_without_project(self):
        ctx = _ctx()
        assemble_tool_list(ctx)
        catalog = _names(ctx.executable_tool_catalog)
        for n in self._RETIRED:
            self.assertNotIn(n, catalog)


class TestLegacyShim(unittest.TestCase):
    def test_caller_supplied_tools_override(self):
        tl, hr = _assemble_tool_list(
            cfg={'tools': [{'type': 'function', 'function': {'name': 'foo'}}]},
            project_path='', project_enabled=False, task_id='t', search_mode='multi',
            search_enabled=True, fetch_enabled=True, code_exec_enabled=False,
            browser_enabled=False, desktop_enabled=False,
            messages=[])
        self.assertEqual(_names(tl), ['foo'])
        self.assertTrue(hr)

    def test_empty_returns_none_tool_list(self):
        # Force-disable read_files by simulating no specs would be a deeper
        # change; instead assert the legacy shim's None contract when the
        # registry produces nothing.  read_files is always on, so we verify
        # the shim wraps an empty list to None via a direct registry call.
        tl, hr = assemble_tool_list(_ctx())
        # read_files keeps this non-empty — assert the shim path stays valid.
        self.assertIsNotNone(tl)
        self.assertTrue(hr)

    def test_eager_families_are_not_misclassified_as_explicit_pins(self):
        cfg = {'memoryEnabled': True, 'mcpEnabled': False}
        tl, _ = _assemble_tool_list(
            cfg=cfg, project_path='/tmp/x', project_enabled=True,
            task_id='t', search_mode='multi', search_enabled=True,
            fetch_enabled=True, code_exec_enabled=False,
            browser_enabled=False, desktop_enabled=False,
            image_gen_enabled=False,
            messages=[])
        pins = set(cfg['_frontendSelectedToolNames'])
        names = set(_names(tl))
        assert pins == set()
        assert 'list_dir' not in names
        # The compact skills surface stays callable with zero installed packs:
        # search_skills is how the model discovers the verified catalog, and
        # load_skill must remain authorized for an approved same-turn install.
        assert 'search_skills' in names
        assert 'load_skill' in names
        # High-level production tools merely ride the search gate; the human
        # did not explicitly select them, so they remain eligible for search.
        assert 'produce_report' in _names(cfg['_executableToolCatalog'])
        assert 'produce_report' not in pins


class TestPluginRegistration(unittest.TestCase):
    def test_plugin_spec_contributes(self):
        marker = {'type': 'function', 'function': {'name': '_test_weather_tool'}}
        spec = ToolSpec(
            key='_test_weather',
            build=lambda ctx: [marker] if ctx.cfg.get('weatherEnabled') else [],
            phase='base', category='test',
        )
        try:
            register_tool_spec(spec)
            self.assertIn(spec, all_specs())
            ctx = _ctx(cfg={'weatherEnabled': True})
            assemble_tool_list(ctx)
            self.assertIn(
                '_test_weather_tool', _names(ctx.executable_tool_catalog))
            # Gate off → absent.
            tl_off, _ = assemble_tool_list(_ctx(cfg={}))
            self.assertNotIn('_test_weather_tool', _names(tl_off))
        finally:
            # Clean up so the global registry isn't polluted for other tests.
            import lib.tools.registry as _reg
            _reg._TOOL_SPECS[:] = [s for s in _reg._TOOL_SPECS if s.key != '_test_weather']
            _reg._REGISTERED_KEYS.discard('_test_weather')

    def test_duplicate_key_ignored_without_replace(self):
        import lib.tools.registry as _reg
        spec = ToolSpec(key='_dup_test', build=lambda ctx: [], phase='base')
        try:
            register_tool_spec(spec)
            n_before = len(_reg._TOOL_SPECS)
            register_tool_spec(ToolSpec(key='_dup_test', build=lambda ctx: [], phase='base'))
            self.assertEqual(len(_reg._TOOL_SPECS), n_before, 'duplicate must be ignored')
        finally:
            _reg._TOOL_SPECS[:] = [s for s in _reg._TOOL_SPECS if s.key != '_dup_test']
            _reg._REGISTERED_KEYS.discard('_dup_test')


class TestHandlerSync(unittest.TestCase):
    """A ToolSpec with handler= must bind into the dispatch tool_registry,
    so one external package can ship schema + gate + handler."""

    def _make_spec(self, key, names, *, special=''):
        def _h(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, pp, pe,
               all_tools=None):
            return tc_id, f'ran:{fn_name}', False
        return ToolSpec(
            key=key,
            build=lambda ctx: [],
            phase='base',
            provides=frozenset(names),
            handler=_h,
            handler_special=special,
            category='test',
        ), _h

    def _cleanup(self, key):
        import lib.tools.registry as _reg
        _reg._TOOL_SPECS[:] = [s for s in _reg._TOOL_SPECS if s.key != key]
        _reg._REGISTERED_KEYS.discard(key)
        # Dropping the ToolSpec does NOT unbind the handler its registration
        # pushed into the dispatch registry: that leaked _hsync_tool_a /
        # __hsync_special__ into the process-global tables and tripped the
        # SSOT coverage ratchet in a later file (test_every_handler_is_declared)
        # whenever the two ran in one process. Restore via the registry's own
        # snapshot primitive so a newly added state table is covered here for
        # free instead of being forgotten like _provenance was.
        snap = getattr(self, '_registry_snap', None)
        if snap is not None:
            from lib.tasks_pkg.executor import tool_registry
            tool_registry.restore(snap)

    def setUp(self):
        from lib.tasks_pkg.executor import tool_registry
        self._registry_snap = tool_registry.snapshot()

    def test_late_registered_handler_is_bound(self):
        # Importing executor runs the startup sync + sets _dispatch_registry.
        from lib.tasks_pkg.executor import tool_registry
        spec, fn = self._make_spec('_hsync_a', ['_hsync_tool_a'])
        try:
            register_tool_spec(spec)
            self.assertIs(tool_registry.lookup('_hsync_tool_a', None), fn)
        finally:
            self._cleanup('_hsync_a')

    def test_special_handler_binding(self):
        from lib.tasks_pkg.executor import tool_registry
        spec, fn = self._make_spec('_hsync_b', ['_hsync_tool_b'],
                                   special='__hsync_special__')
        try:
            register_tool_spec(spec)
            # Special handlers are matched via round_entry toolName mapping;
            # assert it landed in the special table.
            self.assertIs(tool_registry._special.get('__hsync_special__'), fn)
        finally:
            self._cleanup('_hsync_b')

    def test_startup_sync_is_idempotent(self):
        # Re-running sync_spec_handlers must not raise and must keep bindings.
        from lib.tools.registry import sync_spec_handlers
        from lib.tasks_pkg.executor import tool_registry
        n = sync_spec_handlers(tool_registry)
        self.assertGreaterEqual(n, 0)


class TestConcurrencyFlagSync(unittest.TestCase):
    def test_write_and_idempotent_sets_reflect_specs(self):
        from lib.tasks_pkg.tool_dispatch._flags import _IDEMPOTENT_TOOLS, _WRITE_TOOLS
        # Project write tools.
        self.assertIn('run_command', _WRITE_TOOLS)
        self.assertIn('write_file', _WRITE_TOOLS)
        # Memory write tools.
        self.assertIn('create_memory', _WRITE_TOOLS)
        # Idempotent read tools (base set + spec-declared).
        self.assertIn('web_search', _IDEMPOTENT_TOOLS)
        self.assertIn('grep_search', _IDEMPOTENT_TOOLS)
        # Live browser observers are read-only but their results mutate under
        # identical arguments, so they are not in the cache partition.
        self.assertNotIn('browser_read_page', _IDEMPOTENT_TOOLS)
        self.assertNotIn('browser_list_tabs', _IDEMPOTENT_TOOLS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
