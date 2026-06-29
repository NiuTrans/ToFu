"""Tests for lib.conv_config.resolve_auto_translate — the single backend
source of truth for the per-conversation ``autoTranslate`` trigger decision.

Historically the flag was read with three divergent defaults (the safety net
defaulted TRUE at auto_translate.py:73; the input/config paths defaulted FALSE;
the incremental gate read a bare falsy default), which made auto-translate fire
unpredictably. Phase-1 of the stabilisation routed EVERY backend trigger path
through this one resolver, whose canonical default is OPT-IN / OFF
(``AUTO_TRANSLATE_DEFAULT = False``).

These tests pin the unified default + the first-defined-wins precedence so a
future edit that re-introduces a divergent literal default is caught.
"""

import unittest

from lib.conv_config import AUTO_TRANSLATE_DEFAULT, resolve_auto_translate


class TestResolveAutoTranslate(unittest.TestCase):

    def test_canonical_default_is_off(self):
        """The keystone: absent everywhere → OFF (opt-in)."""
        self.assertFalse(AUTO_TRANSLATE_DEFAULT)
        self.assertFalse(resolve_auto_translate({}))
        self.assertFalse(resolve_auto_translate(None))
        self.assertFalse(resolve_auto_translate())
        # A dict that simply doesn't carry the key → default OFF.
        self.assertFalse(resolve_auto_translate({'model': 'x', 'searchMode': 'multi'}))

    def test_explicit_true_wins(self):
        self.assertTrue(resolve_auto_translate({'autoTranslate': True}))
        self.assertTrue(resolve_auto_translate({'autoTranslate': 1}))

    def test_explicit_false_wins(self):
        self.assertFalse(resolve_auto_translate({'autoTranslate': False}))
        self.assertFalse(resolve_auto_translate({'autoTranslate': 0}))

    def test_first_defined_source_wins(self):
        """Left-to-right precedence: the FIRST source defining the key wins,
        regardless of later sources."""
        # First source explicitly ON → ON even though second says OFF.
        self.assertTrue(resolve_auto_translate(
            {'autoTranslate': True}, {'autoTranslate': False}))
        # First source explicitly OFF → OFF even though second says ON.
        self.assertFalse(resolve_auto_translate(
            {'autoTranslate': False}, {'autoTranslate': True}))
        # First source SILENT → fall through to the second.
        self.assertTrue(resolve_auto_translate(
            {'model': 'x'}, {'autoTranslate': True}))
        self.assertFalse(resolve_auto_translate(
            {'model': 'x'}, {'autoTranslate': False}))
        # Both silent → default OFF.
        self.assertFalse(resolve_auto_translate({'a': 1}, {'b': 2}))

    def test_none_value_is_not_a_definition(self):
        """An explicit ``None`` is treated as 'not defined' (mirrors the JS
        ``!== undefined`` gate), so it falls through to the next source."""
        self.assertTrue(resolve_auto_translate(
            {'autoTranslate': None}, {'autoTranslate': True}))
        self.assertFalse(resolve_auto_translate({'autoTranslate': None}))


if __name__ == '__main__':
    unittest.main()
