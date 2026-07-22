"""tests/test_orchestration_composer_timeout.py — composer LLM-call bound.

Regression guard: the NL→graph composer must pass an explicit ``timeout``
to ``smart_chat`` so a hung upstream cannot block the /compose request
thread indefinitely (smart_chat's default is ``timeout=None``).
"""

import unittest

import pytest

import lib.orchestration_composer as composer


@pytest.mark.unit
class ComposerTimeoutTest(unittest.TestCase):
    def test_compose_passes_explicit_timeout_to_smart_chat(self):
        captured = {}

        def fake_smart_chat(**kwargs):
            captured.update(kwargs)
            # Return a minimal valid graph so compose() proceeds normally.
            return (
                '{"reply":"ok","definition":{"schema":"tofu.orchestration/v1",'
                '"name":"T","nodes":[{"id":"s","type":"control","kind":"start"},'
                '{"id":"w","type":"role","role":"worker","params":{}},'
                '{"id":"e","type":"control","kind":"stop"}],'
                '"edges":[{"from":"s","to":"w"},{"from":"w","to":"e"}]}}',
                {},
            )

        # compose() does `from lib.llm_dispatch import smart_chat` at call
        # time, so patch the name on that package namespace.
        import lib.llm_dispatch as _disp
        orig = _disp.smart_chat
        try:
            _disp.smart_chat = fake_smart_chat
            res = composer.compose('build a simple worker flow')
        finally:
            _disp.smart_chat = orig

        self.assertIn('timeout', captured,
                      'composer must pass timeout= to smart_chat')
        self.assertIsInstance(captured['timeout'], (int, float))
        self.assertGreater(captured['timeout'], 0)
        # Sanity: the graph still composed.
        self.assertTrue(res['ok'])


if __name__ == '__main__':
    unittest.main()
