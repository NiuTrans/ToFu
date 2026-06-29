"""tests/test_personal_scope_headless.py — the permanent personal-vs-headless guardrail.

Enforces the contract of ``lib/agent_core/personal_scope``: every app-level
personal capability (the operator's memory store + preference profile, and any
future addition) MUST fail CLOSED on EVERY headless cfg-builder, while the
interactive UI keeps its default-on behaviour.

This is the ratchet that prevents the "new feature silently re-bundled into the
headless API" regression. It is written to fail loudly when:

  * a new entry is added to ``PERSONAL_CAPABILITIES`` but a headless builder
    doesn't honour it (the parametrised ``_assert_fail_closed`` loop), or
  * someone flips a headless default back to default-on, or
  * the preference-profile injection is re-coupled to ``memoryEnabled`` on the
    headless side.

Pure-unit: no HTTP, no live model. Marked ``unit``.
"""

import unittest

import pytest

pytestmark = pytest.mark.unit


class PersonalScopeRegistryTest(unittest.TestCase):
    def test_registry_is_fail_closed(self):
        from lib.agent_core.personal_scope import PERSONAL_CAPABILITIES
        self.assertTrue(PERSONAL_CAPABILITIES, 'registry must not be empty')
        for key, cap in PERSONAL_CAPABILITIES.items():
            self.assertEqual(cap.cfg_key, key)
            # The whole point: headless default is always fail-closed (off).
            self.assertFalse(
                cap.headless_default,
                f'{key} headless_default must be False (fail-closed)')

    def test_apply_fills_only_gaps(self):
        from lib.agent_core.personal_scope import (
            PERSONAL_CAPABILITIES, apply_headless_personal_defaults,
        )
        # Empty cfg → every personal key forced to its fail-closed default.
        cfg = {}
        apply_headless_personal_defaults(cfg)
        for key, cap in PERSONAL_CAPABILITIES.items():
            self.assertEqual(cfg[key], cap.headless_default)

    def test_explicit_caller_value_wins(self):
        from lib.agent_core.personal_scope import (
            PERSONAL_CAPABILITIES, apply_headless_personal_defaults,
        )
        # An explicit opt-in is never overwritten by the fail-closed default.
        cfg = {key: True for key in PERSONAL_CAPABILITIES}
        apply_headless_personal_defaults(cfg)
        for key in PERSONAL_CAPABILITIES:
            self.assertTrue(cfg[key], f'{key} explicit True must survive')

    def test_resolve_preferences_decoupled_from_memory(self):
        from lib.agent_core.personal_scope import resolve_preferences_enabled
        # Explicit flag wins both ways, regardless of memory.
        self.assertTrue(resolve_preferences_enabled(
            {'preferencesEnabled': True}, memory_enabled=False))
        self.assertFalse(resolve_preferences_enabled(
            {'preferencesEnabled': False}, memory_enabled=True))
        # Absent → UI back-compat fallback to the memory flag.
        self.assertTrue(resolve_preferences_enabled({}, memory_enabled=True))
        self.assertFalse(resolve_preferences_enabled({}, memory_enabled=False))
        self.assertFalse(resolve_preferences_enabled(None, memory_enabled=False))


class HeadlessBuildersFailClosedTest(unittest.TestCase):
    """Every headless cfg-builder must apply the fail-closed defaults."""

    def _assert_fail_closed(self, cfg: dict, surface: str):
        from lib.agent_core.personal_scope import PERSONAL_CAPABILITIES
        for key, cap in PERSONAL_CAPABILITIES.items():
            self.assertIn(key, cfg,
                          f'{surface}: {key} must be present (set fail-closed)')
            self.assertEqual(
                cfg[key], cap.headless_default,
                f'{surface}: {key} must default to {cap.headless_default}')

    def test_build_chat_config_bare(self):
        from lib.tasks_pkg.entry import build_chat_config
        cfg = build_chat_config('some-model', None)
        self._assert_fail_closed(cfg, 'build_chat_config (bare)')

    def test_build_chat_config_explicit_opt_in_wins(self):
        from lib.tasks_pkg.entry import build_chat_config
        cfg = build_chat_config('m', {'memoryEnabled': True,
                                       'preferencesEnabled': True})
        self.assertTrue(cfg['memoryEnabled'])
        self.assertTrue(cfg['preferencesEnabled'])

    def test_agent_run_build_cfg_bare(self):
        from routes.api_v1.agent_run import _build_cfg
        cfg = _build_cfg('m', None, None)
        self._assert_fail_closed(cfg, 'agent_run._build_cfg (bare)')

    def test_agent_run_memory_alias_opts_in(self):
        from routes.api_v1.agent_run import _build_cfg
        cfg = _build_cfg('m', {'memory': True}, None)
        self.assertTrue(cfg['memoryEnabled'])
        # preferences NOT implied by memory — still fail-closed.
        self.assertFalse(cfg['preferencesEnabled'])

    def test_agent_run_preferences_alias_opts_in(self):
        from routes.api_v1.agent_run import _build_cfg
        cfg = _build_cfg('m', {'preferences': True}, None)
        self.assertTrue(cfg['preferencesEnabled'])
        self.assertFalse(cfg['memoryEnabled'])

    def test_compat_openai_bare_fails_closed(self):
        from lib.compat.openai import translate_openai_request
        _m, cfg, _o = translate_openai_request(
            {'model': 'x', 'messages': [{'role': 'user', 'content': 'hi'}]})
        self._assert_fail_closed(cfg, 'compat.openai (no tools)')

    def test_compat_openai_with_tools_fails_closed(self):
        from lib.compat.openai import translate_openai_request
        _m, cfg, _o = translate_openai_request({
            'model': 'x', 'messages': [],
            'tools': [{'type': 'function', 'function': {'name': 'foo'}}]})
        self._assert_fail_closed(cfg, 'compat.openai (with tools)')

    def test_compat_anthropic_bare_fails_closed(self):
        from lib.compat.anthropic import translate_anthropic_request
        _m, cfg, _o = translate_anthropic_request(
            {'model': 'x', 'messages': [{'role': 'user', 'content': 'hi'}]})
        self._assert_fail_closed(cfg, 'compat.anthropic (no tools)')


class PromptDescribesNoUngivenCapabilityTest(unittest.TestCase):
    """The prompt side must not describe a personal capability that the cfg
    did not enable — closing the hallucination half of the contract."""

    def _inject(self, cfg_extra: dict) -> list:
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'user', 'content': 'hello'}]
        task = {'id': 't1', 'config': dict(cfg_extra)}
        memory_enabled = bool(cfg_extra.get('memoryEnabled', False))
        _inject_system_contexts(
            messages,
            project_path='', project_enabled=False,
            memory_enabled=memory_enabled,
            search_enabled=False, swarm_enabled=False,
            has_real_tools=True,
            conv_id='', task=task, model='m',
        )
        return messages

    def _all_text(self, messages: list) -> str:
        parts = []
        for m in messages:
            c = m.get('content', '')
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get('type') == 'text':
                        parts.append(b.get('text', ''))
        return '\n'.join(parts)

    def test_memory_off_omits_memory_block(self):
        text = self._all_text(self._inject({'memoryEnabled': False,
                                            'preferencesEnabled': False}))
        self.assertNotIn('<memory_accumulation>', text)

    def test_preferences_decoupled_from_memory_on_headless(self):
        # memoryEnabled True but preferences explicitly False (the headless
        # opt-in-memory-only case) → NO preference profile block.
        from lib.memory.user_profile import save_profile, profile_path
        import os
        # Seed a profile so the block WOULD inject if the gate let it.
        _seeded = False
        if not os.path.isfile(profile_path()):
            save_profile('## Preferences\n- Always answer in Chinese')
            _seeded = True
        try:
            text = self._all_text(self._inject({'memoryEnabled': True,
                                                'preferencesEnabled': False}))
            self.assertNotIn('[USER PREFERENCE PROFILE]', text)
        finally:
            if _seeded:
                save_profile('')  # clean up the seeded file


if __name__ == '__main__':
    unittest.main()
