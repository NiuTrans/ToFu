# Guard anchor: Plan Mode read-only contract (Codex plan.md analogue). The
# dispatch lane mirrors the multi_agent_read_only rejection lane; these tests
# pin the flag plumbing, the ban-set union, the wire filter, and the
# <proposed_plan> extraction so a NEUTER of any layer fails loudly.
"""tests/test_plan_mode.py — Plan Mode (read-only planning) contract.

Covers the three enforcement layers independently:

  1. Prompt — the context-composer provider block appears iff planMode.
  2. Assembly — mutating schemas leave the wire when planMode is on.
  3. Dispatch — the rejection lane exists and its ban set = write partition
     ∪ PLAN_MODE_EXTRA_BAN (the per-task partitions stay the authority).

Plus the deliverable path: ``<proposed_plan>`` extraction (last block wins,
unclosed tail is not a plan) and the turn-native authority document.
"""

from __future__ import annotations

import json
import unittest

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.plan_mode import (
    PLAN_MODE_EXTRA_BAN,
    extract_proposed_plan,
    plan_mode_banned_names,
    plan_mode_enabled,
    plan_mode_call_allowed,
    plan_mode_filter_tool_schemas,
    plan_mode_prompt_block,
    plan_mode_rejection,
    normalize_plan_mode_conversation_settings,
    normalize_plan_mode_runtime_config,
)


class TestPlanModeFlag(unittest.TestCase):
    def test_enabled_only_on_explicit_true(self):
        self.assertTrue(plan_mode_enabled({'planMode': True}))
        self.assertFalse(plan_mode_enabled({'planMode': False}))
        self.assertFalse(plan_mode_enabled({}))
        self.assertFalse(plan_mode_enabled(None))
        # Wire flags fail closed; truthy garbage must not silently enter a
        # security-sensitive collaboration mode.
        self.assertFalse(plan_mode_enabled({'planMode': 1}))

    def test_resolve_model_config_surfaces_plan_mode(self):
        from lib.tasks_pkg.model_config import _resolve_model_config
        on = _resolve_model_config({'planMode': True}, 'abcdef1234567890')
        off = _resolve_model_config({}, 'abcdef1234567890')
        self.assertTrue(on['plan_mode'])
        self.assertTrue(on['human_guidance_enabled'])
        self.assertFalse(off['plan_mode'])

    def test_runtime_normalization_closes_incompatible_loop_owners(self):
        normalized = normalize_plan_mode_runtime_config({
            'planMode': True,
            'endpointMode': True,
            'endpointEnabled': True,
            'autopilot': True,
            'autopilotEnabled': True,
            'imageGenMode': True,
            'activeFlow': 'builtin:autopilot',
            'flowDefinition': {'nodes': []},
            'flowBuiltin': 'autopilot',
            'flowId': 'flow-a',
        })
        self.assertTrue(normalized['humanGuidanceEnabled'])
        self.assertNotIn('endpointMode', normalized)
        self.assertNotIn('endpointEnabled', normalized)
        self.assertFalse(normalized['autopilot'])
        self.assertFalse(normalized['autopilotEnabled'])
        self.assertFalse(normalized['imageGenMode'])
        self.assertEqual(normalized['activeFlow'], '')
        for key in ('flowDefinition', 'flowBuiltin', 'flowId'):
            self.assertNotIn(key, normalized)

    def test_conversation_settings_normalization_matches_runtime(self):
        normalized = normalize_plan_mode_conversation_settings({
            'planMode': True,
            'humanGuidanceEnabled': False,
            'endpointEnabled': True,
            'autopilotEnabled': True,
            'imageGenMode': True,
            'activeFlow': 'flow-a',
        })
        self.assertTrue(normalized['humanGuidanceEnabled'])
        self.assertNotIn('endpointEnabled', normalized)
        self.assertFalse(normalized['autopilotEnabled'])
        self.assertFalse(normalized['imageGenMode'])
        self.assertEqual(normalized['activeFlow'], '')

    def test_non_plan_interaction_modes_have_one_loop_owner(self):
        flow = normalize_plan_mode_runtime_config({
            'planMode': False,
            'activeFlow': 'flow-a',
            'autopilot': True,
            'imageGenMode': True,
        })
        self.assertEqual(flow['activeFlow'], 'flow-a')
        self.assertFalse(flow['autopilot'])
        self.assertFalse(flow['imageGenMode'])

        autopilot = normalize_plan_mode_runtime_config({
            'planMode': False,
            'autopilot': True,
            'imageGenMode': True,
        })
        self.assertTrue(autopilot['autopilot'])
        self.assertFalse(autopilot['imageGenMode'])

    def test_persisted_flow_precedes_autopilot(self):
        normalized = normalize_plan_mode_conversation_settings({
            'planMode': False,
            'activeFlow': 'flow-a',
            'autopilotEnabled': True,
            'imageGenMode': True,
        })
        self.assertEqual(normalized['activeFlow'], 'flow-a')
        self.assertFalse(normalized['autopilotEnabled'])
        self.assertFalse(normalized['imageGenMode'])


class TestBanSet(unittest.TestCase):
    def test_extra_ban_contents(self):
        # The Codex update_plan analogue + scheduler/swarm/artifact lanes.
        for name in ('todo_write', 'spawn_agents', 'schedule_create',
                     'schedule_manage', 'timer_create', 'timer_manage',
                     'generate_image', 'produce_video', 'produce_report',
                     'produce_research', 'produce_slides', 'edit_slides'):
            self.assertIn(name, PLAN_MODE_EXTRA_BAN)

    def test_banned_names_unions_write_partition(self):
        banned = plan_mode_banned_names({'write_file', 'run_command'})
        self.assertIn('write_file', banned)
        self.assertIn('run_command', banned)
        self.assertIn('todo_write', banned)      # extra ban rides along
        self.assertNotIn('read_files', banned)
        self.assertNotIn('web_search', banned)

    def test_registry_write_partition_is_banned(self):
        # The dispatch lane's authority: every spec-declared write tool must
        # land in the ban set (browser/desktop mutators, MCP writes, …).
        from lib.tasks_pkg.tool_dispatch._flags import _registry_tool_flags
        write, _ = _registry_tool_flags()
        banned = plan_mode_banned_names(write)
        for name in ('write_file', 'edit_file', 'run_command',
                     'create_memory', 'update_search_settings',
                     'browser_click', 'desktop_write_file'):
            self.assertIn(name, banned, f'{name} must be banned in Plan Mode')

    def test_rejection_text_names_tool_and_way_out(self):
        msg = plan_mode_rejection('write_file')
        self.assertIn('write_file', msg)
        self.assertIn('Plan mode', msg)
        self.assertIn('proposed_plan', msg)


class TestAssemblyWireFilter(unittest.TestCase):
    """Plan Mode strips mutating schemas from the exposed tool list."""

    BASE_CFG = {
        'codeExecEnabled': True, 'searchMode': 'multi',
        'fetchEnabled': True, 'memoryEnabled': True,
    }

    def _names(self, cfg):
        from lib.tasks_pkg.model_config import _assemble_tool_list
        tools, _ = _assemble_tool_list(
            cfg, '', False, 'abcdef1234567890', 'multi',
            True, True, True, False, False)
        return {
            (t.get('function') or {}).get('name') or ''
            for t in (tools or [])
        }

    def test_plan_mode_strips_mutating_tools(self):
        names = self._names(dict(self.BASE_CFG, planMode=True))
        for banned in ('run_command', 'todo_write', 'update_search_settings',
                       'create_memory', 'write_file'):
            self.assertNotIn(banned, names)
        # Read-only exploration tools stay.
        self.assertIn('web_search', names)
        self.assertIn('fetch_url', names)
        self.assertIn('read_files', names)
        self.assertIn('search_memories', names)
        self.assertIn('ask_human', names)

    def test_normal_mode_untouched(self):
        names = self._names(dict(self.BASE_CFG))
        self.assertIn('todo_write', names)
        self.assertIn('create_memory', names)
        self.assertIn('update_search_settings', names)

    def test_exact_call_classifier_restricts_mixed_and_unknown_tools(self):
        self.assertTrue(plan_mode_call_allowed(
            'desktop_clipboard', {'action': 'read'}))
        self.assertFalse(plan_mode_call_allowed(
            'desktop_clipboard', {'action': 'write', 'text': 'x'}))
        self.assertTrue(plan_mode_call_allowed(
            'desktop_system_info', {'type': 'overview'}))
        self.assertFalse(plan_mode_call_allowed(
            'desktop_system_info', {'type': 'environment'}))
        self.assertFalse(plan_mode_call_allowed('unknown_remote_mutation', {}))
        self.assertFalse(plan_mode_call_allowed('custom__untrusted', {}))

    def test_wire_filter_restricts_mixed_schema_discriminators(self):
        tools = [{
            'type': 'function',
            'function': {
                'name': 'desktop_clipboard',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'action': {'type': 'string', 'enum': ['read', 'write']},
                    },
                },
            },
        }, {
            'type': 'function',
            'function': {'name': 'unknown_remote_mutation', 'parameters': {}},
        }]
        kept, dropped = plan_mode_filter_tool_schemas(tools)
        self.assertEqual(
            kept[0]['function']['parameters']['properties']['action']['enum'],
            ['read'],
        )
        self.assertEqual(dropped, ['unknown_remote_mutation'])

    def test_caller_supplied_tools_are_not_an_assembly_bypass(self):
        from lib.tasks_pkg.model_config import _assemble_tool_list
        cfg = {
            'planMode': True,
            'tools': [{
                'type': 'function',
                'function': {'name': 'read_files', 'parameters': {}},
            }, {
                'type': 'function',
                'function': {'name': 'evil_custom_write', 'parameters': {}},
            }],
        }
        tools, has_real_tools = _assemble_tool_list(
            cfg, '', False, 'abcdef1234567890', 'off',
            False, False, False, False, False,
        )
        self.assertEqual(
            [(tool.get('function') or {}).get('name') for tool in tools or []],
            ['read_files', 'ask_human'],
        )
        self.assertTrue(has_real_tools)
        self.assertNotIn(
            'evil_custom_write', cfg['_executableToolNamespaceByName'])
        self.assertEqual(
            cfg['_executableToolNamespaceByName']['ask_human'], 'builtin')


class TestPromptBlock(unittest.TestCase):
    def test_block_content_contract(self):
        block = plan_mode_prompt_block()
        self.assertIn('Plan Mode', block)
        self.assertIn('non-mutating', block)
        self.assertIn('proposed_plan', block)
        collapsed = ' '.join(block.split())
        self.assertIn('Continue discussing', collapsed)
        self.assertIn('Execute with current context', collapsed)
        self.assertIn('Execute in fresh context', collapsed)

    def test_provider_block_follows_toggle(self):
        from lib.tasks_pkg.context_composer._models import ComposeRequest
        from lib.tasks_pkg.context_composer._providers import (
            collect_context_blocks)
        msgs = [{'role': 'user', 'content': 'hi'}]
        on = collect_context_blocks(
            msgs, ComposeRequest(task={'config': {'planMode': True}}))
        on_block = next(b for b in on if b.id == 'plan_mode')
        self.assertFalse(on_block.suppressed_reason)
        self.assertIn('<plan_mode>', on_block.content)
        self.assertEqual(on_block.placement, 'tail')

        off = collect_context_blocks(
            msgs, ComposeRequest(task={'config': {}}))
        off_block = next(b for b in off if b.id == 'plan_mode')
        self.assertEqual(off_block.suppressed_reason, 'plan_mode_off')
        self.assertEqual(off_block.content, '')


class TestProposedPlanExtraction(unittest.TestCase):
    def test_extracts_inner_markdown(self):
        text = ('先做些说明。\n\n<proposed_plan>\n## Summary\n做 X\n\n'
                '## Test Plan\n跑 Y\n</proposed_plan>\n\n后记')
        self.assertEqual(
            extract_proposed_plan(text), '## Summary\n做 X\n\n## Test Plan\n跑 Y')

    def test_last_block_wins(self):
        text = ('<proposed_plan>v1</proposed_plan> 中间 '
                '<proposed_plan>\nv2 完整替代\n</proposed_plan>')
        self.assertEqual(extract_proposed_plan(text), 'v2 完整替代')

    def test_unclosed_tail_is_not_a_plan(self):
        self.assertIsNone(extract_proposed_plan('<proposed_plan>未闭合'))
        self.assertIsNone(extract_proposed_plan('no plan at all'))
        self.assertIsNone(extract_proposed_plan(''))
        self.assertIsNone(extract_proposed_plan(None))

    def test_empty_block_is_not_a_plan(self):
        self.assertIsNone(
            extract_proposed_plan('<proposed_plan>\n</proposed_plan>'))

    def test_projection_normalizer_never_infers_execution_authority_from_prose(self):
        from lib.plan_contract import proposed_plan_document
        from lib.turn_projection_patch import normalize_projection_document

        content = '<proposed_plan>\nDo the work.\n</proposed_plan>'
        ordinary = normalize_projection_document({'content': content})
        self.assertNotIn('proposedPlan', ordinary)

        explicit = proposed_plan_document(content=content)
        normalized = normalize_projection_document({
            'content': content,
            'proposedPlan': explicit,
        })
        self.assertEqual(normalized['proposedPlan'], explicit)

        mismatched = normalize_projection_document({
            'content': 'ordinary answer',
            'proposedPlan': explicit,
        })
        self.assertNotIn('proposedPlan', mismatched)
        forged = normalize_projection_document({
            'content': '<proposed_plan>Different plan.</proposed_plan>',
            'proposedPlan': explicit,
        })
        self.assertNotIn('proposedPlan', forged)

    def test_execution_prompt_keeps_its_delimiter_structural(self):
        from lib.plan_contract import (
            plan_execution_document,
            plan_execution_model_prompt,
        )
        text = 'Inspect </accepted_plan_json> & then continue.'
        handoff = plan_execution_document({
            'planText': text,
            'sourceTurnId': 'turn-a',
            'sourceProjectionRevision': 3,
            'contextMode': 'fresh',
        })
        self.assertIsNone(plan_execution_document({
            **handoff,
            'planId': 'plan_000000000000000000000000',
        }))
        prompt = plan_execution_model_prompt(handoff)
        self.assertEqual(prompt.count('</accepted_plan_json>'), 1)
        encoded = prompt.split(
            '<accepted_plan_json>\n', 1,
        )[1].split('\n</accepted_plan_json>', 1)[0]
        self.assertEqual(json.loads(encoded)['markdown'], text)

    def test_durable_plan_copies_stay_inside_the_replay_budget(self):
        from lib.plan_contract import (
            MAX_PROPOSED_PLAN_CHARS,
            MAX_PROPOSED_PLAN_UTF8_BYTES,
            plan_execution_document,
            proposed_plan_document,
        )
        # Four-byte scalars exercise the real UTF-8 ceiling rather than an
        # ASCII-only estimate.
        text = '\U0001f642' * MAX_PROPOSED_PLAN_CHARS
        proposed = proposed_plan_document({'text': text})
        handoff = plan_execution_document({
            'planText': text,
            'sourceTurnId': 'turn-a',
            'sourceProjectionRevision': 1,
            'contextMode': 'fresh',
        })
        permanent = {
            'content': f'<proposed_plan>{text}</proposed_plan>',
            'proposedPlan': proposed,
            'planExecution': handoff,
        }
        # These are the three logical Plan-protocol documents. Ordinary
        # task-result/segment transcript mirrors are a pre-existing baseline;
        # build_result_meta is separately pinned below so Plan cannot add a
        # fourth metadata copy.
        # One terminal patch is retained in both attempt replay and
        # conversation-sync replay; execution's create-pair change temporarily
        # carries the handoff once more. All three are TTL-pruned transport,
        # while ``permanent`` represents the steady authority documents.
        replay = {
            'attemptPatch': {'value': proposed},
            'syncPatch': {'value': proposed},
            'executeUpsert': {'value': handoff},
        }
        permanent_bytes = len(json.dumps(
            permanent, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8'))
        peak_bytes = permanent_bytes + len(json.dumps(
            replay, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8'))
        self.assertEqual(MAX_PROPOSED_PLAN_CHARS, 64_000)
        self.assertEqual(MAX_PROPOSED_PLAN_UTF8_BYTES, 256_000)
        self.assertLessEqual(
            permanent_bytes, 3 * MAX_PROPOSED_PLAN_UTF8_BYTES + 4096)
        self.assertLessEqual(
            peak_bytes, 6 * MAX_PROPOSED_PLAN_UTF8_BYTES + 16_384)
        self.assertIsNone(extract_proposed_plan(
            f'<proposed_plan>{text}x</proposed_plan>'))


class TestResultMetaDoesNotDuplicatePlanAuthority(unittest.TestCase):
    def test_build_result_meta_does_not_copy_plan_text(self):
        from lib.tasks_pkg.manager._persist import build_result_meta
        task = {'id': 'abcdef1234567890',
                'config': {'planMode': True},
                'content': '前言\n<proposed_plan>\n## S\n干活\n</proposed_plan>'}
        meta = build_result_meta(task)
        # Content is already durable; executable identity is minted exactly
        # once by turn_lifecycle into projection.proposedPlan.
        self.assertNotIn('plan', meta)


class TestConvConfigResolver(unittest.TestCase):
    def test_plan_mode_resolution_matrix(self):
        from lib.conv_config._resolve import (
            resolve_conv_config, resolve_conv_settings)
        # Active conv: live toolbar override wins.
        self.assertFalse(resolve_conv_config(
            conv_settings={'planMode': True},
            overrides={'planMode': False}, is_active=True)['planMode'])
        # Active conv with an old frontend (no override key): stored value.
        self.assertTrue(resolve_conv_config(
            conv_settings={'planMode': True},
            overrides={}, is_active=True)['planMode'])
        # Inactive conv reads its stored value.
        self.assertTrue(resolve_conv_config(
            conv_settings={'planMode': True}, is_active=False)['planMode'])
        # Absent everywhere → off (headless fail-closed).
        self.assertFalse(resolve_conv_config()['planMode'])
        # Settings persistence round-trips the toggle.
        self.assertTrue(resolve_conv_settings(
            conv_settings={'planMode': True})['planMode'])
        self.assertFalse(resolve_conv_settings()['planMode'])

    def test_resolvers_persist_and_execute_one_compatible_plan_state(self):
        from lib.conv_config._resolve import (
            resolve_conv_config, resolve_conv_settings)
        resolved = resolve_conv_config(conv_settings={
            'planMode': True,
            'autopilotEnabled': True,
            'activeFlow': 'builtin:autopilot',
            'humanGuidanceEnabled': False,
        })
        self.assertTrue(resolved['humanGuidanceEnabled'])
        self.assertFalse(resolved['autopilot'])
        self.assertEqual(resolved['activeFlow'], '')
        self.assertNotIn('flowBuiltin', resolved)
        self.assertNotIn('flowId', resolved)

        settings = resolve_conv_settings(conv_settings={
            'planMode': True,
            'autopilotEnabled': True,
            'activeFlow': 'builtin:autopilot',
            'humanGuidanceEnabled': False,
        })
        self.assertTrue(settings['humanGuidanceEnabled'])
        self.assertFalse(settings['autopilotEnabled'])
        self.assertEqual(settings['activeFlow'], '')


class TestDispatchLaneWiring(unittest.TestCase):
    """Structural anchor: the read-only rejection lane stays wired into the
    dispatch pipeline (NEUTER bite: deleting the lane fails this test)."""

    def test_pipeline_rejects_banned_calls_in_plan_mode(self):
        import inspect
        import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
        src = inspect.getsource(_pipeline)
        self.assertIn('plan_mode_rejection', src)
        self.assertIn('plan_mode_call_allowed', src)
        self.assertIn('plan_mode_read_only', src)
        # The lane must consult the per-task write partition, not a frozen
        # import-time snapshot.
        self.assertIn('_write_tools', src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
