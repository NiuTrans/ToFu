"""tests/test_conv_config.py — port-parity tests for conv config builders."""

from __future__ import annotations

import unittest

from lib.conv_config import (
    canonicalise_model_id, extract_legacy_thinking_depth,
    resolve_conv_config, resolve_conv_settings,
)


class ResolveConvConfigTest(unittest.TestCase):

    def test_active_conv_uses_overrides(self):
        out = resolve_conv_config(
            conv_settings={'model': 'inactive-m', 'searchMode': 'off'},
            overrides={'model': 'override-m', 'searchMode': 'multi',
                        'fetchEnabled': True},
            server_defaults={'serverModel': 'default-m'},
            is_active=True,
        )
        self.assertEqual(out['model'], 'override-m')
        self.assertEqual(out['preset'], 'override-m')
        self.assertEqual(out['searchMode'], 'multi')
        self.assertTrue(out['fetchEnabled'])

    def test_inactive_conv_uses_stored(self):
        out = resolve_conv_config(
            conv_settings={'model': 'inactive-m', 'searchMode': 'off',
                            'fetchEnabled': True},
            overrides={'model': 'override-m', 'searchMode': 'multi'},
            server_defaults={'serverModel': 'default-m'},
            is_active=False,
        )
        self.assertEqual(out['model'], 'inactive-m')
        self.assertEqual(out['searchMode'], 'off')
        self.assertTrue(out['fetchEnabled'])

    def test_falls_back_to_server_default(self):
        out = resolve_conv_config(
            conv_settings={},
            overrides={},
            server_defaults={'serverModel': 'default-m'},
            is_active=True,
        )
        self.assertEqual(out['model'], 'default-m')

    def test_inactive_with_no_conv_settings_falls_back(self):
        out = resolve_conv_config(
            conv_settings={},
            overrides={'model': 'should-be-ignored'},
            server_defaults={'serverModel': 'default-m'},
            is_active=False,
        )
        self.assertEqual(out['model'], 'default-m')

    def test_memory_enabled_defaults_true(self):
        # JS: conv.memoryEnabled !== undefined ? !!conv.memoryEnabled : true
        out = resolve_conv_config(
            conv_settings={},  # no memoryEnabled key
            overrides={},
            is_active=False,
        )
        self.assertTrue(out['memoryEnabled'])

    def test_memory_enabled_explicit_false(self):
        out = resolve_conv_config(
            conv_settings={'memoryEnabled': False},
            overrides={},
            is_active=False,
        )
        self.assertFalse(out['memoryEnabled'])

    def test_search_mode_default_multi(self):
        out = resolve_conv_config(
            conv_settings={},
            overrides={},
            is_active=False,
        )
        self.assertEqual(out['searchMode'], 'multi')

    def test_auto_translate_defaults_false(self):
        # ★ Gate-divergence guard. The runtime config produced here defaults
        # autoTranslate=False, but the server-side safety net
        # (lib/tasks_pkg/auto_translate.py) reads settings.autoTranslate with a
        # default of TRUE. When the task config says False yet conv settings
        # say True (or vice-versa) the incremental accumulator and the safety
        # net disagree about who owns translation; the orphan-teardown in
        # _maybe_auto_translate_assistant's finally block must absorb that
        # divergence (see tests/test_auto_translate_safety_net.py). This test
        # pins the config default so the divergence can't widen silently.
        out = resolve_conv_config(conv_settings={}, overrides={}, is_active=True)
        self.assertFalse(out['autoTranslate'])

    def test_auto_translate_override_true_active(self):
        out = resolve_conv_config(
            conv_settings={}, overrides={'autoTranslate': True}, is_active=True)
        self.assertTrue(out['autoTranslate'])

    def test_auto_translate_conv_value_wins_inactive(self):
        out = resolve_conv_config(
            conv_settings={'autoTranslate': True},
            overrides={'autoTranslate': False},
            is_active=False)
        self.assertTrue(out['autoTranslate'])

    def test_browser_client_id_only_when_enabled(self):
        out = resolve_conv_config(
            conv_settings={},
            overrides={'browserEnabled': True,
                        'browserClientId': 'client-abc'},
            is_active=True,
        )
        self.assertEqual(out['browserClientId'], 'client-abc')

        out2 = resolve_conv_config(
            conv_settings={},
            overrides={'browserEnabled': False,
                        'browserClientId': 'client-abc'},
            is_active=True,
        )
        self.assertIsNone(out2['browserClientId'])

    def test_endpoint_mode_maps_from_endpointEnabled_inactive(self):
        # JS: endpointMode = is_active ? endpointEnabled (override) : !!conv.endpointEnabled
        out = resolve_conv_config(
            conv_settings={'endpointEnabled': True},
            overrides={'endpointMode': False},
            is_active=False,
        )
        self.assertTrue(out['endpointMode'])

    def test_keep_tool_history_default_true(self):
        # JS: config.keepToolHistory !== false → true even if undefined.
        out = resolve_conv_config(
            conv_settings={}, overrides={}, is_active=True)
        self.assertTrue(out['keepToolHistory'])

    def test_keep_tool_history_explicit_false(self):
        out = resolve_conv_config(
            conv_settings={},
            overrides={'keepToolHistory': False},
            is_active=True,
        )
        self.assertFalse(out['keepToolHistory'])

    def test_project_paths_list_copy(self):
        paths = ['/a', '/b']
        out = resolve_conv_config(
            conv_settings={'projectPaths': paths},
            overrides={}, is_active=False,
        )
        self.assertEqual(out['projectPaths'], paths)
        # Mutating the result must NOT mutate input.
        out['projectPaths'].append('/c')
        self.assertEqual(paths, ['/a', '/b'])

    def test_read_only_paths_list_copy(self):
        ro = ['/b']
        out = resolve_conv_config(
            conv_settings={'projectPaths': ['/a', '/b'], 'readOnlyPaths': ro},
            overrides={}, is_active=False,
        )
        self.assertEqual(out['readOnlyPaths'], ['/b'])
        out['readOnlyPaths'].append('/c')
        self.assertEqual(ro, ['/b'])

    def test_read_only_paths_default_empty(self):
        out = resolve_conv_config(
            conv_settings={}, overrides={}, is_active=True)
        self.assertEqual(out['readOnlyPaths'], [])

    def test_all_expected_keys_present(self):
        out = resolve_conv_config(
            conv_settings={}, overrides={}, is_active=True)
        expected = {
            'maxTokens', 'thinkingEnabled', 'model', 'preset',
            'systemPrompt', 'systemPromptMode', 'systemPromptBlocks',
            'thinkingDepth', 'temperature', 'searchMode',
            'fetchEnabled', 'codeExecEnabled', 'memoryEnabled',
            'schedulerEnabled', 'swarmEnabled', 'projectPath',
            'projectPaths', 'readOnlyPaths', 'autoApply', 'browserEnabled',
            'desktopEnabled', 'imageGenEnabled', 'humanGuidanceEnabled',
            'endpointMode', 'autopilot',
            'autoTranslate', 'langCorrectionEnabled', 'uiLang',
            'browserClientId', 'keepToolHistory',
            'activeFlow', 'flowBuiltin', 'flowId',
        }
        self.assertEqual(set(out.keys()), expected)

    def test_active_flow_builtin_parsed(self):
        out = resolve_conv_config(
            conv_settings={}, overrides={'activeFlow': 'builtin:autopilot'},
            is_active=True)
        self.assertEqual(out['activeFlow'], 'builtin:autopilot')
        self.assertEqual(out['flowBuiltin'], 'autopilot')
        self.assertEqual(out['flowId'], '')

    def test_active_flow_stored_id_parsed(self):
        out = resolve_conv_config(
            conv_settings={}, overrides={'activeFlow': 'orch_abc123'},
            is_active=True)
        self.assertEqual(out['flowBuiltin'], '')
        self.assertEqual(out['flowId'], 'orch_abc123')

    def test_active_flow_unknown_builtin_ignored(self):
        out = resolve_conv_config(
            conv_settings={}, overrides={'activeFlow': 'builtin:nope'},
            is_active=True)
        self.assertEqual(out['flowBuiltin'], '')
        self.assertEqual(out['flowId'], '')

    def test_active_flow_empty_when_none(self):
        out = resolve_conv_config(conv_settings={}, overrides={}, is_active=True)
        self.assertEqual(out['activeFlow'], '')
        self.assertEqual(out['flowBuiltin'], '')
        self.assertEqual(out['flowId'], '')

    def test_active_flow_inactive_reads_stored(self):
        out = resolve_conv_config(
            conv_settings={'activeFlow': 'orch_stored'},
            overrides={'activeFlow': 'builtin:endpoint'},
            is_active=False)
        self.assertEqual(out['flowId'], 'orch_stored')
        self.assertEqual(out['flowBuiltin'], '')


class ResolveConvSettingsTest(unittest.TestCase):

    def test_basic_settings(self):
        out = resolve_conv_settings(
            conv_settings={
                'model': 'm1', 'thinkingDepth': 'high',
                'searchMode': 'multi', 'fetchEnabled': True,
                'memoryEnabled': True, 'projectPath': '/code',
                'folderId': 'f1',
            },
        )
        self.assertEqual(out['model'], 'm1')
        self.assertEqual(out['preset'], 'm1')
        self.assertEqual(out['thinkingDepth'], 'high')
        self.assertEqual(out['searchMode'], 'multi')
        self.assertTrue(out['fetchEnabled'])
        self.assertTrue(out['memoryEnabled'])
        self.assertEqual(out['projectPath'], '/code')
        self.assertEqual(out['folderId'], 'f1')

    def test_memory_enabled_default_true_when_missing(self):
        out = resolve_conv_settings(conv_settings={})
        self.assertTrue(out['memoryEnabled'])

    def test_memory_enabled_explicit_false(self):
        out = resolve_conv_settings(
            conv_settings={'memoryEnabled': False})
        self.assertFalse(out['memoryEnabled'])

    def test_search_mode_default_multi(self):
        out = resolve_conv_settings(conv_settings={})
        self.assertEqual(out['searchMode'], 'multi')

    def test_folder_id_null_when_missing(self):
        out = resolve_conv_settings(conv_settings={})
        self.assertIsNone(out['folderId'])

    def test_project_paths_list_copy(self):
        paths = ['/a']
        out = resolve_conv_settings(
            conv_settings={'projectPaths': paths})
        self.assertEqual(out['projectPaths'], paths)
        out['projectPaths'].append('/b')
        self.assertEqual(paths, ['/a'])

    def test_overrides_fill_when_conv_empty(self):
        # When conv has no model, fall back to overrides.
        out = resolve_conv_settings(
            conv_settings={},
            overrides={'model': 'fallback', 'serverModel': 'srv'},
        )
        self.assertEqual(out['model'], 'fallback')

    def test_server_model_when_no_others(self):
        out = resolve_conv_settings(
            conv_settings={},
            overrides={'serverModel': 'srv-only'},
        )
        self.assertEqual(out['model'], 'srv-only')

    def test_all_expected_keys_present(self):
        out = resolve_conv_settings(conv_settings={})
        expected = {
            'model', 'preset', 'thinkingDepth', 'searchMode',
            'fetchEnabled', 'codeExecEnabled', 'browserEnabled',
            'desktopEnabled', 'memoryEnabled', 'schedulerEnabled',
            'swarmEnabled', 'endpointEnabled', 'autopilotEnabled',
            'imageGenEnabled', 'humanGuidanceEnabled', 'projectPath',
            'projectPaths', 'readOnlyPaths', 'autoTranslate', 'folderId',
            'activeFlow', 'uiLang',
        }
        self.assertEqual(set(out.keys()), expected)


class CanonicaliseModelIdTest(unittest.TestCase):

    def test_legacy_preset_rewritten(self):
        self.assertEqual(canonicalise_model_id('opus'),
                          'aws.claude-opus-4.7')
        self.assertEqual(canonicalise_model_id('qwen'),
                          'qwen3.6-plus')
        self.assertEqual(canonicalise_model_id('gemini'),
                          'gemini-3-flash-preview')
        self.assertEqual(canonicalise_model_id('low'), 'qwen3.6-plus')

    def test_compound_preset_rewritten_to_opus(self):
        for preset in ('medium', 'high', 'xhigh', 'max'):
            self.assertEqual(canonicalise_model_id(preset),
                              'aws.claude-opus-4.7',
                              f'preset={preset}')

    def test_canonical_model_id_passthrough(self):
        self.assertEqual(canonicalise_model_id('claude-opus-4-7'),
                          'claude-opus-4-7')
        self.assertEqual(canonicalise_model_id('gpt-4o'), 'gpt-4o')
        self.assertEqual(canonicalise_model_id('aws.claude-opus-4.7'),
                          'aws.claude-opus-4.7')

    def test_empty_input(self):
        self.assertEqual(canonicalise_model_id(''), '')
        self.assertEqual(canonicalise_model_id(None), '')
        self.assertEqual(canonicalise_model_id(123), '')


class ExtractLegacyThinkingDepthTest(unittest.TestCase):

    def test_compound_presets(self):
        self.assertEqual(extract_legacy_thinking_depth('medium'), 'medium')
        self.assertEqual(extract_legacy_thinking_depth('high'), 'high')
        self.assertEqual(extract_legacy_thinking_depth('xhigh'), 'xhigh')
        self.assertEqual(extract_legacy_thinking_depth('max'), 'max')

    def test_non_compound_returns_none(self):
        self.assertIsNone(extract_legacy_thinking_depth('opus'))
        self.assertIsNone(extract_legacy_thinking_depth('qwen'))
        self.assertIsNone(extract_legacy_thinking_depth('claude-opus-4-7'))
        self.assertIsNone(extract_legacy_thinking_depth(''))
        self.assertIsNone(extract_legacy_thinking_depth(None))


class LegacyPresetIntegrationTest(unittest.TestCase):
    """Ensure legacy preset → canonical migration flows through both
    resolve_conv_config and resolve_conv_settings."""

    def test_resolve_config_canonicalises_active_model(self):
        out = resolve_conv_config(
            conv_settings={},
            overrides={'model': 'opus'},
            is_active=True,
        )
        self.assertEqual(out['model'], 'aws.claude-opus-4.7')
        self.assertEqual(out['preset'], 'aws.claude-opus-4.7')

    def test_resolve_config_canonicalises_inactive_model(self):
        out = resolve_conv_config(
            conv_settings={'model': 'qwen'},
            overrides={},
            is_active=False,
        )
        self.assertEqual(out['model'], 'qwen3.6-plus')

    def test_compound_preset_backfills_thinking_depth(self):
        out = resolve_conv_config(
            conv_settings={},
            overrides={'model': 'high'},  # legacy compound preset
            is_active=True,
        )
        # Model becomes opus; depth comes from the preset.
        self.assertEqual(out['model'], 'aws.claude-opus-4.7')
        self.assertEqual(out['thinkingDepth'], 'high')

    def test_explicit_depth_wins_over_legacy(self):
        out = resolve_conv_config(
            conv_settings={},
            overrides={'model': 'high', 'thinkingDepth': 'medium'},
            is_active=True,
        )
        # Caller-supplied thinkingDepth wins.
        self.assertEqual(out['thinkingDepth'], 'medium')

    def test_resolve_settings_canonicalises(self):
        out = resolve_conv_settings(
            conv_settings={'model': 'opus'},
        )
        self.assertEqual(out['model'], 'aws.claude-opus-4.7')

    def test_resolve_settings_compound_preset_backfills_depth(self):
        out = resolve_conv_settings(
            conv_settings={'model': 'max'},
        )
        self.assertEqual(out['model'], 'aws.claude-opus-4.7')
        self.assertEqual(out['thinkingDepth'], 'max')


if __name__ == '__main__':
    unittest.main()
