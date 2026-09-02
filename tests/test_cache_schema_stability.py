"""Contracts for deterministic, request-live tool assembly.

The available tools and their schema projection are always rebuilt from the
current request so conversation toggles take effect on the next model round.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from lib.tools.registry import ToolContext, assemble_tool_list


ROOT = Path(__file__).resolve().parents[1]

# These paths are intentionally asserted absent after the conversation tool
# freeze was removed. They are migration tombstones, not live source anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/tools/registry/_latch.py',
    'static/js/toolset-apply.js',
}


def _names(tool_list):
    return [tool['function']['name'] for tool in (tool_list or [])]


def _ctx(**overrides):
    base = dict(
        cfg={}, task_id='t-test', conv_id='',
        project_path='', project_enabled=False,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        image_gen_enabled=False,
        human_guidance_enabled=False, scheduler_enabled=False, messages=[],
    )
    base.update(overrides)
    return ToolContext(**base)


def _edit_file_desc(tool_list):
    for tool in tool_list or []:
        if tool['function']['name'] == 'edit_file':
            item = tool['function']['parameters']['properties']['edits']['items']
            return item['properties']['path'].get('description', '')
    return ''


class TestLiveMultirootSchema(unittest.TestCase):
    def _single(self, conv_id):
        return _ctx(
            conv_id=conv_id, project_path='/tmp/a', project_enabled=True,
            cfg={'projectPaths': ['/tmp/a']})

    def _multi(self, conv_id):
        return _ctx(
            conv_id=conv_id, project_path='/tmp/a', project_enabled=True,
            cfg={'projectPaths': ['/tmp/a', '/tmp/b']})

    def test_single_root_has_no_hint(self):
        tools, _ = assemble_tool_list(self._single('_mr_conv'))
        self.assertNotIn('rootname:', _edit_file_desc(tools))

    def test_multi_root_adds_path_hint(self):
        tools, _ = assemble_tool_list(self._multi('_mr_conv'))
        self.assertIn('rootname:', _edit_file_desc(tools))

    def test_downgrade_removes_path_hint_next_assembly(self):
        assemble_tool_list(self._multi('_mr_conv'))
        tools, _ = assemble_tool_list(self._single('_mr_conv'))
        self.assertNotIn('rootname:', _edit_file_desc(tools))


class TestMCPDeterministicOrdering(unittest.TestCase):
    def _client_with(self, names):
        from lib.mcp.client import MCPBridge

        client = MCPBridge()
        for name in names:
            client._tool_index[name] = {
                'server_name': name.split('__')[1] if '__' in name else 's',
                'tool_name': name,
                'namespaced_name': name,
                'description': '',
                'input_schema': {'type': 'object', 'properties': {}},
                'openai_def': {
                    'type': 'function', 'function': {'name': name}},
                'read_only_hint': True,
            }
        return client

    def test_ordering_is_sorted_not_insertion_order(self):
        client = self._client_with(['mcp__z__t', 'mcp__a__t', 'mcp__m__t'])
        self.assertEqual(
            _names(client.get_openai_tool_defs()),
            ['mcp__a__t', 'mcp__m__t', 'mcp__z__t'])

    def test_reconnect_order_does_not_change_sequence(self):
        first = self._client_with(
            ['mcp__hope__a', 'mcp__hope__b', 'mcp__x__c'])
        second = self._client_with(
            ['mcp__x__c', 'mcp__hope__b', 'mcp__hope__a'])
        self.assertEqual(
            _names(first.get_openai_tool_defs()),
            _names(second.get_openai_tool_defs()))


class TestLiveToolAvailability(unittest.TestCase):
    """Feature changes are reflected immediately within one conversation."""

    CONV = '_live_tool_conv'

    def _assemble(self, *, project=False):
        from lib.tasks_pkg.model_config import _assemble_tool_list

        cfg = {
            'mcpEnabled': False,
            'projectPaths': ['/tmp/project'] if project else [],
        }
        return _assemble_tool_list(
            cfg,
            project_path='/tmp/project' if project else '',
            project_enabled=project,
            task_id='t-live-tool',
            search_mode='off', search_enabled=False, fetch_enabled=False,
            code_exec_enabled=False, browser_enabled=False,
            desktop_enabled=False,
            messages=[], conv_id=self.CONV,
        )[0]

    def test_swarm_tools_are_default_tools(self):
        # Swarm is a default tool family (no user-facing switch since
        # 2026-08-23): spawn_agents must ride EVERY assembly, project or not.
        self.assertIn('spawn_agents', _names(self._assemble()))
        self.assertIn('spawn_agents', _names(self._assemble(project=True)))

    def test_project_attach_is_visible_next_assembly(self):
        before = _names(self._assemble(project=False))
        after = _names(self._assemble(project=True))
        self.assertNotIn('run_command', before)
        self.assertIn('run_command', after)

    def test_project_detach_is_visible_next_assembly(self):
        before = _names(self._assemble(project=True))
        after = _names(self._assemble(project=False))
        self.assertIn('run_command', before)
        self.assertNotIn('run_command', after)


class TestConversationToolFreezeRemoved(unittest.TestCase):
    def test_state_api_and_frontend_entrypoints_are_absent(self):
        self.assertFalse((ROOT / 'lib/tools/registry/_latch.py').exists())
        self.assertFalse((ROOT / 'static/js/toolset-apply.js').exists())

        sources = {
            'model_config': ROOT / 'lib/tasks_pkg/model_config.py',
            'finalize': ROOT / 'lib/tasks_pkg/orchestrator/_finalize.py',
            'conversations_api': ROOT / 'routes/api_v1/conversations.py',
            'frontend_runtime': ROOT / 'frontend/src/runtime/app-runtime.js',
            'template': ROOT / 'index.html',
        }
        forbidden = (
            'latch_tool_list', 'tool_list_latch', 'toolsetDiff',
            'toolsetDiverged', '/toolset/apply', 'toolset-apply.js',
            'applyToolset', 'syncToolsetBanner',
        )
        for label, path in sources.items():
            text = path.read_text(encoding='utf-8')
            for needle in forbidden:
                self.assertNotIn(needle, text, f'{label} retains {needle}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
