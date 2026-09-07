"""tests/test_text_lang.py — lib.text_lang unit tests.

Includes JS port-parity for ``_isAlreadyChinese`` and broader coverage
of the new ``guess_language`` helper.
"""

from __future__ import annotations

import unittest

from lib.text_lang import (
    CHINESE_RATIO_THRESHOLD, MIN_CHARS_FOR_DETECTION,
    cjk_ratio, guess_language, is_predominantly_chinese, latin_ratio,
)


class CJKRatioTest(unittest.TestCase):

    def test_empty_returns_zero(self):
        self.assertEqual(cjk_ratio(''), 0.0)
        self.assertEqual(cjk_ratio(None), 0.0)  # type: ignore[arg-type]

    def test_pure_chinese(self):
        # CJK punctuation (。、！) is OUTSIDE the unified-ideograph
        # range, so we only count the ideographs themselves.
        self.assertGreaterEqual(cjk_ratio('你好世界今天天气不错呀'), 0.99)
        # With punctuation included in the denominator the ratio drops
        # but still clears the 0.30 threshold easily.
        self.assertGreater(cjk_ratio('你好世界。今天天气不错。'), 0.80)

    def test_pure_english(self):
        self.assertEqual(cjk_ratio('Hello, this is a long enough sentence.'), 0.0)

    def test_too_short(self):
        # Below MIN_CHARS_FOR_DETECTION → returns 0 to avoid false positives.
        self.assertEqual(cjk_ratio('你好'), 0.0)

    def test_mixed(self):
        text = 'Today is 2026年5月25日 周一。'
        r = cjk_ratio(text)
        self.assertGreater(r, 0.10)
        self.assertLess(r, 0.50)

    def test_extension_a_chars(self):
        # \u3400-\u4dbf range
        self.assertGreater(cjk_ratio('\u3400\u3400\u3400\u3400\u3400\u3400\u3400\u3400'), 0.99)


class IsPredominantlyChineseTest(unittest.TestCase):

    def test_pure_chinese_yes(self):
        self.assertTrue(is_predominantly_chinese('你好世界。今天天气不错。'))

    def test_pure_english_no(self):
        self.assertFalse(is_predominantly_chinese(
            'This is a perfectly normal English sentence with enough chars.'))

    def test_30pct_threshold_boundary(self):
        # JS port-parity: exactly the documented threshold.
        # Construct a string where ~30% of non-whitespace chars are CJK.
        text = '中文text部分英文text的mixed内容content here'
        # If predominantly Chinese, the cjk ratio is >= 0.30.
        result = is_predominantly_chinese(text)
        # Assert the contract: True iff cjk_ratio >= 0.30.
        self.assertEqual(result, cjk_ratio(text) >= CHINESE_RATIO_THRESHOLD)

    def test_short_text_returns_false(self):
        self.assertFalse(is_predominantly_chinese('hi'))
        self.assertFalse(is_predominantly_chinese('你好'))  # 2 < 8

    def test_empty_returns_false(self):
        self.assertFalse(is_predominantly_chinese(''))
        self.assertFalse(is_predominantly_chinese(None))  # type: ignore[arg-type]

    def test_non_string_safe(self):
        self.assertFalse(is_predominantly_chinese(123))  # type: ignore[arg-type]
        self.assertFalse(is_predominantly_chinese([]))   # type: ignore[arg-type]


class LatinRatioTest(unittest.TestCase):

    def test_pure_english(self):
        self.assertGreater(latin_ratio('Hello there friends'), 0.90)

    def test_pure_chinese_zero(self):
        self.assertEqual(latin_ratio('你好世界今天天气不错啊'), 0.0)

    def test_handles_accented(self):
        # Latin-1 supplement letters count.
        self.assertGreater(latin_ratio('Café résumé naïve façade'), 0.90)


class GuessLanguageTest(unittest.TestCase):

    def test_short_unknown(self):
        self.assertEqual(guess_language('hi'), 'unknown')
        self.assertEqual(guess_language(''), 'unknown')
        self.assertEqual(guess_language('   '), 'unknown')

    def test_chinese(self):
        self.assertEqual(
            guess_language('你好世界，今天天气不错，我们去散步吧'), 'zh')

    def test_english(self):
        self.assertEqual(
            guess_language('Hello world, today the weather is nice.'),
            'en')

    def test_mixed_returned_when_both_present(self):
        text = '今天的会议讨论了deep learning和transformer architecture的最新进展和应用'
        # Both ratios non-trivial.
        self.assertEqual(guess_language(text), 'mixed')

    def test_numbers_only_unknown(self):
        # No CJK, no Latin → unknown.
        self.assertEqual(guess_language('1234567890 !@#$%^&*()'), 'unknown')


if __name__ == '__main__':
    unittest.main()
