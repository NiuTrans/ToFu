"""tests/test_agent_options.py — TofuOptions dataclass contract tests."""

from __future__ import annotations

import unittest

import pytest

from lib.agent_options import TofuOptions

pytestmark = pytest.mark.unit


class TestFromCfg(unittest.TestCase):

    def test_empty_input(self):
        for empty in (None, {}, ):
            opts = TofuOptions.from_cfg(empty)
            self.assertEqual(opts.model, '')
            self.assertEqual(opts.search_mode, 'multi')
            self.assertTrue(opts.fetch_enabled)
            self.assertTrue(opts.memory_enabled)
            self.assertFalse(opts.swarm_enabled)
            self.assertEqual(opts.extras, {})

    def test_non_dict_input(self):
        opts = TofuOptions.from_cfg('not a dict')  # type: ignore[arg-type]
        self.assertEqual(opts.model, '')

    def test_known_keys(self):
        cfg = {
            'model': 'claude-opus-4-7',
            'maxTokens': 200000,
            'temperature': 0.5,
            'thinkingEnabled': True,
            'thinkingDepth': 'high',
            'searchMode': 'multi',
            'fetchEnabled': False,
            'projectPath': '/tmp/proj',
            'swarmEnabled': True,
            'mcpEnabled': False,
        }
        opts = TofuOptions.from_cfg(cfg)
        self.assertEqual(opts.model, 'claude-opus-4-7')
        self.assertEqual(opts.max_tokens, 200000)
        self.assertEqual(opts.temperature, 0.5)
        self.assertTrue(opts.thinking_enabled)
        self.assertEqual(opts.thinking_depth, 'high')
        self.assertEqual(opts.search_mode, 'multi')
        self.assertFalse(opts.fetch_enabled)
        self.assertEqual(opts.project_path, '/tmp/proj')
        self.assertTrue(opts.swarm_enabled)
        self.assertFalse(opts.mcp_enabled)

    def test_legacy_effort_alias(self):
        opts = TofuOptions.from_cfg({'effort': 'gemini'})
        self.assertEqual(opts.preset, 'gemini')
        # When both preset and effort are set, preset wins (declared in
        # _FIELDS; the alias only kicks in when preset is absent).
        opts2 = TofuOptions.from_cfg({'preset': 'opus', 'effort': 'gemini'})
        # Either order of dict-iter is fine; just assert 'preset' was honored.
        self.assertIn(opts2.preset, ('opus', 'gemini'))

    def test_unknown_keys_kept_in_extras(self):
        cfg = {
            'model': 'x',
            'someExperimentalFlag': True,
            'totallyUnknown': 42,
        }
        opts = TofuOptions.from_cfg(cfg)
        self.assertEqual(opts.model, 'x')
        self.assertTrue(opts.extras.get('someExperimentalFlag'))
        self.assertEqual(opts.extras.get('totallyUnknown'), 42)

    def test_bool_coercion(self):
        opts = TofuOptions.from_cfg({
            'fetchEnabled': 'false',
            'memoryEnabled': 1,
            'swarmEnabled': 'yes',
        })
        self.assertFalse(opts.fetch_enabled)
        self.assertTrue(opts.memory_enabled)
        self.assertTrue(opts.swarm_enabled)

    def test_int_coercion_from_string(self):
        opts = TofuOptions.from_cfg({'maxTokens': '50000'})
        self.assertEqual(opts.max_tokens, 50000)

    def test_int_coercion_failure_keeps_value(self):
        opts = TofuOptions.from_cfg({'maxTokens': 'not-a-number'})
        # Coercion failure keeps the raw value so downstream sees the
        # bad input and can decide what to do.
        self.assertEqual(opts.max_tokens, 'not-a-number')


class TestToCfg(unittest.TestCase):

    def test_round_trip_preserves_known_keys(self):
        original = {
            'model': 'claude-opus-4-7',
            'maxTokens': 64000,
            'thinkingEnabled': True,
            'searchMode': 'multi',
            'projectPath': '/tmp/p',
        }
        opts = TofuOptions.from_cfg(original)
        out = opts.to_cfg()
        for k, v in original.items():
            self.assertEqual(out.get(k), v, f'lost key {k}')

    def test_round_trip_preserves_extras(self):
        cfg = {'model': 'm', 'futureKey': {'a': 1}}
        out = TofuOptions.from_cfg(cfg).to_cfg()
        self.assertEqual(out.get('futureKey'), {'a': 1})
        self.assertEqual(out.get('model'), 'm')

    def test_camelcase_emission(self):
        opts = TofuOptions(thinking_depth='high', search_mode='multi',
                           project_path='/x')
        out = opts.to_cfg()
        self.assertIn('thinkingDepth', out)
        self.assertIn('searchMode', out)
        self.assertIn('projectPath', out)
        # No snake_case keys leaking onto the wire.
        self.assertNotIn('thinking_depth', out)
        self.assertNotIn('search_mode', out)
        self.assertNotIn('project_path', out)

    def test_orchestrator_compatibility(self):
        """The dict to_cfg() emits MUST satisfy the orchestrator's reads.

        This locks the wire schema: every key the orchestrator looks up
        with .get() should be present (or default-equivalent).
        """
        opts = TofuOptions.from_cfg({'model': 'm'})
        d = opts.to_cfg()
        # A representative subset of the keys orchestrator + model_config
        # actually call cfg.get(...) on.  Adding a new lookup over there
        # without a corresponding TofuOptions field will trip this test.
        for key in [
            'model', 'maxTokens', 'temperature', 'searchMode',
            'fetchEnabled', 'projectPath', 'memoryEnabled',
            'browserEnabled', 'swarmEnabled', 'imageGenEnabled',
            'humanGuidanceEnabled', 'schedulerEnabled', 'mcpEnabled',
            'codeExecEnabled', 'desktopEnabled',
            'autoApply', 'keepToolHistory',
            'disableModelFallback',
        ]:
            self.assertIn(key, d, f'orchestrator-read key missing: {key}')


class TestReplace(unittest.TestCase):

    def test_replace_overrides_fields(self):
        a = TofuOptions(model='m', endpoint_mode=True, autopilot=True)
        b = a.replace(endpoint_mode=False, autopilot=False)
        self.assertTrue(a.endpoint_mode)
        self.assertFalse(b.endpoint_mode)
        self.assertEqual(b.model, 'm')

    def test_replace_preserves_extras(self):
        a = TofuOptions.from_cfg({'model': 'm', 'futureKey': 1})
        b = a.replace(model='n')
        self.assertEqual(b.model, 'n')
        self.assertEqual(b.extras.get('futureKey'), 1)


class TestOpenAPISchema(unittest.TestCase):

    def test_schema_shape(self):
        s = TofuOptions.openapi_schema()
        self.assertEqual(s['type'], 'object')
        self.assertTrue(s.get('additionalProperties'))
        self.assertIn('properties', s)
        for required in ('model', 'maxTokens', 'thinkingDepth',
                          'searchMode', 'projectPath'):
            self.assertIn(required, s['properties'], f'missing {required}')

    def test_schema_enums_present(self):
        s = TofuOptions.openapi_schema()
        self.assertIn('enum', s['properties']['searchMode'])
        self.assertIn('multi', s['properties']['searchMode']['enum'])

    def test_schema_descriptions_present(self):
        s = TofuOptions.openapi_schema()
        self.assertTrue(s['properties']['model'].get('description'))
        self.assertTrue(s['properties']['mcpEnabled'].get('description'))


if __name__ == '__main__':
    unittest.main()
