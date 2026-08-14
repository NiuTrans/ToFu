"""tests/test_explicit_tools_passthrough.py — caller-supplied tools take precedence.

When a caller passes ``cfg['tools'] = [...]`` (typically from an OpenAI
or Anthropic compat client), the orchestrator must use those tools
verbatim and skip the auto-derived feature toggles
(search/fetch/memory/MCP).
"""

import unittest


class ExplicitToolsTest(unittest.TestCase):

    def _assemble(self, cfg, **overrides):
        from lib.tasks_pkg.model_config import _assemble_tool_list
        defaults = {
            'project_path': '',
            'project_enabled': False,
            'task_id': 'tttttttt',
            'search_mode': 'multi',
            'search_enabled': True,
            'fetch_enabled': True,
            'code_exec_enabled': False,
            'browser_enabled': False,
            'desktop_enabled': False,
            'swarm_enabled': False,
            'image_gen_enabled': False,
            'human_guidance_enabled': False,
            'scheduler_enabled': False,
            'messages': [],
        }
        defaults.update(overrides)
        return _assemble_tool_list(cfg, **defaults)

    def test_explicit_tools_wins_over_auto(self):
        cfg = {
            'tools': [{
                'type': 'function',
                'function': {'name': 'my_tool',
                              'description': 'd',
                              'parameters': {'type': 'object'}},
            }],
            'mcpEnabled': True,  # would normally inject MCP tools
            'memoryEnabled': True,
            'searchMode': 'multi',
        }
        tools, has_real = self._assemble(cfg)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['function']['name'], 'my_tool')
        self.assertTrue(has_real)
        self.assertEqual(cfg['_frontendSelectedToolNames'], ['my_tool'])
        self.assertEqual(cfg['_toolNamespaceByName'], {'my_tool': 'custom'})

    def test_empty_explicit_tools_falls_back_to_auto(self):
        # Empty list should NOT lock out the auto-derived path —
        # callers who literally want zero tools should set
        # searchMode='off', fetchEnabled=False, etc.
        cfg = {'tools': []}
        tools, has_real = self._assemble(cfg, search_mode='off',
                                         search_enabled=False,
                                         fetch_enabled=False)
        # read_files always present even with no other tools.
        self.assertTrue(any('read_files' in (t.get('function', {}) or {}).get('name', '')
                             for t in (tools or [])))

    def test_malformed_tool_dropped_others_kept(self):
        cfg = {
            'tools': [
                {'type': 'function',
                 'function': {'name': 'good',
                              'description': '',
                              'parameters': {'type': 'object'}}},
                'not a dict',
                {'no_function_key': True},
            ],
        }
        tools, has_real = self._assemble(cfg)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['function']['name'], 'good')

    def test_no_tools_field_uses_auto(self):
        # No 'tools' key at all → auto-derive from feature flags.
        tools, _ = self._assemble({}, search_mode='off',
                                  search_enabled=False,
                                  fetch_enabled=False)
        # Should at least include read_files.
        self.assertTrue(tools)

    def test_explicit_tools_disable_mcp(self):
        # The contract: explicit tools means caller takes full control.
        # MCP tools must NOT be appended when explicit tools win.
        cfg = {
            'tools': [{'type': 'function',
                        'function': {'name': 'a',
                                     'description': '',
                                     'parameters': {'type': 'object'}}}],
            'mcpEnabled': True,
        }
        tools, _ = self._assemble(cfg)
        names = [t['function']['name'] for t in tools]
        self.assertEqual(names, ['a'])


if __name__ == '__main__':
    unittest.main()
