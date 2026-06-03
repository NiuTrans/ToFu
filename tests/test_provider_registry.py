"""tests/test_provider_registry.py — Pluggable LLM body-dialect registry.

Provider-side mirror of tests/test_tool_registry.py.  Pins:
  * Built-in dialects (none / chat_template_kwargs / enable_thinking /
    thinking_type) produce UNCHANGED bodies after the registry refactor.
  * A plugin BodyDialect contributes a custom body shape via both build_body
    and _readjust_thinking_params.
  * Slot() and the BYO-provider validator accept registered plugin dialects.
  * Typos / unknown formats still raise (the incident-driven strict check).
"""

from __future__ import annotations

import unittest

from lib.llm.body import build_body
from lib.llm_dispatch.api import _readjust_thinking_params
from lib.llm_dispatch.provider_registry import (
    BodyDialect,
    get_dialect,
    is_valid_thinking_format,
    register_dialect,
)
from lib.llm_dispatch.slot import Slot

_MSGS = [{'role': 'user', 'content': 'hi'}]


def _keep(body):
    return {k: body[k] for k in (
        'temperature', 'enable_thinking', 'thinking', 'chat_template_kwargs',
        'reasoning_split', 'effort') if k in body}


class TestBuiltinParity(unittest.TestCase):
    """Built-in dialects must produce exactly the documented bodies."""

    def test_none(self):
        b = build_body('m', _MSGS, thinking_enabled=True, temperature=0.5,
                       thinking_format='none')
        self.assertEqual(_keep(b), {'temperature': 0.5})

    def test_chat_template_kwargs(self):
        b = build_body('m', _MSGS, thinking_enabled=True, temperature=0.5,
                       thinking_format='chat_template_kwargs')
        self.assertEqual(_keep(b), {
            'chat_template_kwargs': {'enable_thinking': True},
            'temperature': 0.5})

    def test_enable_thinking(self):
        b = build_body('m', _MSGS, thinking_enabled=False, temperature=0.5,
                       thinking_format='enable_thinking')
        self.assertEqual(_keep(b), {'enable_thinking': False, 'temperature': 0.5})

    def test_thinking_type(self):
        b = build_body('m', _MSGS, thinking_enabled=True, temperature=0.5,
                       thinking_format='thinking_type')
        self.assertEqual(_keep(b), {
            'thinking': {'type': 'enabled'}, 'temperature': 0.5})

    def test_longcat_temperature_override_preserved(self):
        b = build_body('longcat-flash', _MSGS, thinking_enabled=True,
                       temperature=0.5, thinking_format='enable_thinking')
        self.assertEqual(_keep(b), {'enable_thinking': True, 'temperature': 1.0})

    def test_readjust_thinking_type(self):
        body = {'model': 'm', 'messages': _MSGS,
                'thinking': {'type': 'enabled'}, 'temperature': 0.5}
        _readjust_thinking_params(body, 'm', 'thinking_type')
        self.assertEqual(_keep(body), {
            'thinking': {'type': 'enabled'}, 'temperature': 0.5})

    def test_get_dialect_none_for_builtins(self):
        for k in ('', 'none', 'enable_thinking', 'thinking_type',
                  'chat_template_kwargs'):
            self.assertIsNone(get_dialect(k),
                              f'built-in {k!r} must NOT be in the plugin registry')


class TestPluginDialect(unittest.TestCase):
    def setUp(self):
        def _build(body, *, thinking_enabled, temperature, model, effort):
            body['reasoning'] = {'enabled': bool(thinking_enabled)}
            body['temperature'] = temperature if temperature is not None else 0.7

        def _readj(body, *, is_enabled, model, effort):
            body['reasoning'] = {'enabled': bool(is_enabled)}

        self.key = '_test_engine_v1'
        register_dialect(BodyDialect(self.key, apply_build=_build,
                                     apply_readjust=_readj, description='test'))

    def tearDown(self):
        from lib.llm_dispatch import provider_registry as _pr
        _pr._DIALECTS.pop(self.key, None)

    def test_build_uses_plugin(self):
        b = build_body('any', _MSGS, thinking_enabled=True, temperature=0.3,
                       thinking_format=self.key)
        self.assertEqual(b.get('reasoning'), {'enabled': True})
        self.assertEqual(b.get('temperature'), 0.3)

    def test_build_preserves_tools_and_extra(self):
        tools = [{'type': 'function', 'function': {'name': 'foo'}}]
        b = build_body('any', _MSGS, thinking_enabled=False, temperature=0.3,
                       thinking_format=self.key, tools=tools,
                       extra={'top_p': 0.9})
        self.assertEqual(b.get('tools'), tools)
        self.assertEqual(b.get('top_p'), 0.9)
        self.assertEqual(b.get('reasoning'), {'enabled': False})

    def test_readjust_uses_plugin_and_clears_old(self):
        body = {'model': 'm', 'messages': _MSGS, 'thinking': {'type': 'enabled'}}
        _readjust_thinking_params(body, 'any', self.key)
        self.assertEqual(body.get('reasoning'), {'enabled': True})
        self.assertNotIn('thinking', body)

    def test_slot_accepts_plugin_dialect(self):
        s = Slot(key_name='k0', api_key='x', model='any',
                 capabilities={'text'}, thinking_format=self.key)
        self.assertEqual(s.thinking_format, self.key)

    def test_byo_validator_accepts_plugin_dialect(self):
        from lib.byo_providers import _validate_thinking_format
        self.assertEqual(_validate_thinking_format(self.key), self.key)


class TestStrictValidation(unittest.TestCase):
    def test_slot_rejects_typo(self):
        with self.assertRaises(ValueError):
            Slot(key_name='k0', api_key='x', model='m',
                 capabilities={'text'}, thinking_format='chat_template_kwarg')

    def test_byo_rejects_typo(self):
        from lib.byo_providers import _validate_thinking_format
        with self.assertRaises(ValueError):
            _validate_thinking_format('bogus_dialect')

    def test_is_valid_accepts_builtins(self):
        for k in ('', 'none', 'enable_thinking', 'thinking_type',
                  'chat_template_kwargs'):
            self.assertTrue(is_valid_thinking_format(k))

    def test_register_rejects_builtin_collision(self):
        from lib.llm_dispatch import provider_registry as _pr
        before = dict(_pr._DIALECTS)
        register_dialect(BodyDialect('none', apply_build=lambda **k: None))
        self.assertEqual(_pr._DIALECTS, before, 'must not override built-in key')


if __name__ == '__main__':
    unittest.main(verbosity=2)
