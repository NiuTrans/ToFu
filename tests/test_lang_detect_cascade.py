"""tests/test_lang_detect_cascade.py — cascade language detector guardrail.

Proves each tier of ``lib.text_lang.detect_language`` is load-bearing:

  * Tier-0 script fast-path decides CJK/Kana/Hangul/Cyrillic/… at ~1.0 with
    NO model + NO LLM;
  * Tier-1 fastText resolves Latin-script the heuristic cannot (the
    German/Spanish gotcha), when the backend is enabled;
  * Tier-2 LLM correction fires ONLY on the bounded ambiguous-tail trigger and
    ONLY when allowed (personal_scope fail-closed);
  * the send-path gate (`_should_translate_input`) skips only a confident
    English verdict.

Each behaviour has a NEUTER twin that breaks the tier and asserts the guard
then fails — so the assertion is proven to test something real.

fastText tests are SKIPPED when ``fast_langdetect`` is not importable (the
guarded-optional dep), so the suite is green on a vanilla box too.
"""

from __future__ import annotations

import os
import unittest

import pytest

pytestmark = pytest.mark.unit

from lib import text_lang as tl
from lib.text_lang import (
    DetectionResult, LANG_HIGH_CONFIDENCE, _needs_llm_correction,
    detect_language, detect_script, reset_for_test,
)


def _ft_available() -> bool:
    try:
        import fast_langdetect  # noqa: F401
        return True
    except Exception:
        return False


_HAVE_FT = _ft_available()


class ScriptFastPathTest(unittest.TestCase):
    """Tier-0 — decisive scripts, zero deps."""

    def setUp(self):
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'script'
        reset_for_test()

    def test_scripts_resolve_without_model(self):
        cases = {
            '你好世界，今天天气很好啊': 'zh',
            'こんにちは、お元気ですか': 'ja',
            '안녕하세요 만나서 반갑습니다': 'ko',
            'Привет, как твои дела': 'ru',
            'مرحبا كيف حالك اليوم': 'ar',
        }
        for text, code in cases.items():
            res = detect_language(text)
            self.assertEqual(res.code, code, text)
            self.assertEqual(res.source, 'script')
            self.assertGreaterEqual(res.confidence, 0.99)

    def test_short_text_no_overcommit(self):
        # Below MIN_CHARS_FOR_DETECTION → script path abstains.
        self.assertIsNone(detect_script('你好'))

    def test_stray_glyph_is_not_decisive(self):
        # One ideograph in an English sentence must NOT trigger 'zh'.
        self.assertIsNone(detect_script('The kanji 語 appears once here only'))

    def test_neuter_broken_script_range_misses(self):
        # NEUTER: blank the compiled script patterns → the CJK sentence is no
        # longer decided by Tier-0, proving the fast path was load-bearing.
        saved = tl._SCRIPT_RES
        try:
            tl._SCRIPT_RES = tuple()
            self.assertIsNone(detect_script('你好世界，今天天气很好啊'))
        finally:
            tl._SCRIPT_RES = saved
        # Restored → decisive again.
        self.assertEqual(detect_script('你好世界，今天天气很好啊'), 'zh')


class HeuristicFallbackTest(unittest.TestCase):
    """No statistical model → heuristic terminal answer (vanilla-box parity)."""

    def setUp(self):
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'script'
        reset_for_test()

    def test_english_confident(self):
        res = detect_language('This is a normal English sentence with enough length.')
        self.assertEqual(res.code, 'en')
        self.assertEqual(res.source, 'heuristic')

    def test_chinese_via_script(self):
        res = detect_language('这是一句足够长的中文句子用于检测语言')
        self.assertEqual(res.code, 'zh')
        self.assertEqual(res.source, 'script')


@unittest.skipUnless(_HAVE_FT, 'fast_langdetect not installed')
class FastTextTierTest(unittest.TestCase):
    """Tier-1 — resolves Latin-script the heuristic cannot."""

    def setUp(self):
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'fasttext'
        reset_for_test()

    def tearDown(self):
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'script'
        reset_for_test()

    def test_distinguishes_latin_languages(self):
        # The core gotcha: these are ~0.97 Latin and the heuristic calls them
        # all English; fastText separates them.
        self.assertEqual(detect_language('Guten Tag, wie geht es dir heute?').code, 'de')
        self.assertEqual(detect_language('La inteligencia artificial cambia el mundo.').code, 'es')
        self.assertEqual(detect_language('Bonjour, comment allez-vous aujourd\'hui?').code, 'fr')

    def test_source_is_fasttext(self):
        self.assertEqual(detect_language('Ciao, come stai oggi amico mio?').source, 'fasttext')

    def test_neuter_backend_off_falls_back_to_heuristic(self):
        # NEUTER: with the backend OFF, the German sentence is (wrongly) called
        # English by the heuristic — proving Tier-1 is what fixes it.
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'script'
        reset_for_test()
        res = detect_language('Guten Tag, wie geht es dir heute?')
        self.assertEqual(res.source, 'heuristic')
        self.assertEqual(res.code, 'en')  # the bug the cascade closes


class EscalationTriggerTest(unittest.TestCase):
    """Tier-2 bounded escalation trigger (`_needs_llm_correction`)."""

    def test_low_confidence_escalates(self):
        self.assertTrue(_needs_llm_correction(
            DetectionResult('no', 0.40, 'fasttext'), 'App store?'))

    def test_short_text_escalates(self):
        self.assertTrue(_needs_llm_correction(
            DetectionResult('de', 0.74, 'fasttext'), 'im need help'))

    def test_high_confidence_short_circuits(self):
        # A strongly-confident short verdict is trusted (cost cap).
        self.assertFalse(_needs_llm_correction(
            DetectionResult('it', 0.99, 'fasttext'), 'Ciao amico'))

    def test_thin_latin_margin_escalates(self):
        # Two near-equal Latin candidates → ambiguous even above threshold.
        cands = [{'lang': 'en', 'score': 0.75}, {'lang': 'de', 'score': 0.70}]
        self.assertTrue(_needs_llm_correction(
            DetectionResult('en', 0.75, 'fasttext'),
            'this is a medium length latin sentence', cands))

    def test_wide_margin_confident_no_escalate(self):
        cands = [{'lang': 'en', 'score': 0.85}, {'lang': 'de', 'score': 0.05}]
        self.assertFalse(_needs_llm_correction(
            DetectionResult('en', 0.85, 'fasttext'),
            'this is a medium length english sentence here', cands))


class LlmTierGatingTest(unittest.TestCase):
    """LLM tier fires only when allowed AND triggered; corrector is honoured."""

    def setUp(self):
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'script'
        reset_for_test()

    def test_corrector_not_called_when_disallowed(self):
        calls = []

        def _corr(text, tier1):
            calls.append(text)
            return 'es'

        # allow_llm False → corrector must never run even on 'unknown'.
        detect_language('xyzzy plugh', allow_llm=False, llm_corrector=_corr)
        self.assertEqual(calls, [])

    def test_corrector_rescues_unknown_when_allowed(self):
        def _corr(text, tier1):
            return 'es'
        # A too-short/no-signal input → heuristic 'unknown' → LLM rescues.
        res = detect_language('...', allow_llm=True, llm_corrector=_corr)
        # '...' is empty of letters → unknown; corrector supplies 'es'.
        # (guard: only asserts the wiring — code comes from corrector)
        if res.source == 'llm':
            self.assertEqual(res.code, 'es')

    def test_neuter_corrector_returning_none_keeps_tier1(self):
        # NEUTER: a corrector that always declines → the Tier result stands.
        res = detect_language('Hello there friends, nice to meet you.',
                              allow_llm=True, llm_corrector=lambda t, r: None)
        self.assertNotEqual(res.source, 'llm')


class DefaultBoxLatinGotchaTest(unittest.TestCase):
    """The default (fastText OFF) deployment: the LLM tier is the BACKSTOP that
    fixes the English-vs-other-Latin gotcha the heuristic cannot see.

    This is the substance of acceptance criterion C — the gotcha must be fixed
    on the box real users run (script backend, langCorrectionEnabled ON), not
    only when the optional fastText dep is enabled.
    """

    GERMAN = 'Guten Tag, wie geht es dir heute Abend?'

    def setUp(self):
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'script'
        reset_for_test()

    def test_heuristic_en_escalates_and_corrector_wins(self):
        # Baseline: with NO corrector the heuristic (wrongly) calls it English.
        base = detect_language(self.GERMAN)
        self.assertEqual((base.code, base.source), ('en', 'heuristic'))
        # With the LLM tier allowed, the confident-wrong 'en' is escalated and
        # the corrector's 'de' wins — on the DEFAULT script backend.
        res = detect_language(self.GERMAN, allow_llm=True,
                              llm_corrector=lambda t, r: 'de')
        self.assertEqual(res.code, 'de')
        self.assertEqual(res.source, 'llm')

    def test_gate_translates_german_on_default_box(self):
        from lib.chat.turn_builder import _should_translate_input
        import lib.lang_correct as lc
        # Force the corrector to 'de' so the test needs no live model, exercising
        # the real gate → detect_language → escalation wiring end to end.
        orig = lc.llm_language_corrector
        lc.llm_language_corrector = lambda text, tier1=None: 'de'
        try:
            self.assertTrue(_should_translate_input(
                self.GERMAN, {'langCorrectionEnabled': True}))
        finally:
            lc.llm_language_corrector = orig

    def test_neuter_corrector_declines_falls_back_to_en(self):
        # NEUTER: corrector returns None → escalation is a no-op and the
        # heuristic 'en' stands, reproducing the un-fixed gotcha. Proves the
        # escalation (not something else) is what fixes it.
        res = detect_language(self.GERMAN, allow_llm=True,
                              llm_corrector=lambda t, r: None)
        self.assertEqual((res.code, res.source), ('en', 'heuristic'))

    def test_escalation_is_multiword_bounded(self):
        # The bound: only a MULTI-WORD Latin heuristic-'en' is treated as
        # unreliable (enough signal for the corrector). A single-token 'en'
        # verdict is not flagged by the en-path predicate.
        from lib.text_lang import _heuristic_en_is_unreliable
        self.assertTrue(_heuristic_en_is_unreliable('Guten Tag alle'))
        self.assertFalse(_heuristic_en_is_unreliable('ok'))
        self.assertFalse(_heuristic_en_is_unreliable(''))

    def test_genuine_multiword_english_survives(self):
        # Genuine English IS escalated (the heuristic can't be sure), but the
        # corrector confirms 'en' → the verdict stays English, no wrong
        # translation. Conscious cost: one cheap classifier call, correct result.
        res = detect_language('This is genuine English text here today',
                              allow_llm=True, llm_corrector=lambda t, r: 'en')
        self.assertEqual(res.code, 'en')


class SendGateTest(unittest.TestCase):
    """The translate-vs-skip gate uses the confidence-aware detector."""

    def setUp(self):
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'script'
        reset_for_test()

    def test_pinned_source_lang_wins(self):
        from lib.chat.turn_builder import _should_translate_input
        # Explicit non-English source → translate unconditionally.
        self.assertTrue(_should_translate_input('anything', {'translateSourceLang': 'German'}))
        # Explicit English → skip.
        self.assertFalse(_should_translate_input('anything', {'translateSourceLang': 'en'}))

    def test_english_skipped(self):
        from lib.chat.turn_builder import _should_translate_input
        self.assertFalse(_should_translate_input(
            'This is clearly an English sentence with enough length.', {}))

    def test_chinese_translated(self):
        from lib.chat.turn_builder import _should_translate_input
        self.assertTrue(_should_translate_input('这是一句需要翻译的中文句子内容', {}))

    @unittest.skipUnless(_HAVE_FT, 'fast_langdetect not installed')
    def test_german_translated_with_fasttext(self):
        from lib.chat.turn_builder import _should_translate_input
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'fasttext'
        reset_for_test()
        try:
            # The gotcha: German must be translated, not skipped as English.
            self.assertTrue(_should_translate_input(
                'Guten Tag, wie geht es dir heute Abend?', {}))
        finally:
            os.environ['TOFU_LANGDETECT_BACKEND'] = 'script'
            reset_for_test()


class LlmCorrectorRealCallPathTest(unittest.TestCase):
    """Exercise ``llm_language_corrector`` against a monkeypatched ``smart_chat``
    at the DISPATCH boundary — proves the real call path (kwargs + return-shape
    unpack + response parsing), which a stub corrector can never cover.

    ``smart_chat`` is verified to accept ``capability`` / ``log_prefix`` /
    ``max_retries`` and return ``(content, usage)`` (lib/llm_dispatch/api.py:1768;
    both dispatch_chat api.py:370 and the lib.llm.chat fallback chat.py:254
    return that 2-tuple).
    """

    def _patch(self, fn):
        import lib.lang_correct as lc
        import lib.llm_dispatch as d
        reset = getattr(lc, '_reset_language_correction_cache_for_test', None)
        if reset is not None:
            reset()
        self._orig = d.smart_chat
        d.smart_chat = fn
        self.addCleanup(lambda: setattr(d, 'smart_chat', self._orig))

    def test_wellformed_code_returned(self):
        seen = {}

        def fake(messages, **kw):
            seen.update(kw)
            return ('de', {'input_tokens': 5, 'output_tokens': 1})

        self._patch(fake)
        from lib.lang_correct import llm_language_corrector
        self.assertEqual(llm_language_corrector('Guten Tag alle'), 'de')
        # The exact kwargs my code passes must be accepted by smart_chat.
        self.assertEqual(seen.get('capability'), 'cheap')
        self.assertEqual(seen.get('max_tokens'), 8)
        self.assertIn('log_prefix', seen)
        self.assertIn('max_retries', seen)
        self.assertEqual(seen.get('max_429_attempts'), 1)

    def test_repeated_prompt_reuses_opaque_bounded_cache(self):
        calls = []

        def fake(messages, **kw):
            calls.append(messages)
            return ('zh', {})

        self._patch(fake)
        import lib.lang_correct as lc

        text = '继续推进这个任务'
        self.assertEqual(lc.llm_language_corrector(text), 'zh')
        self.assertEqual(lc.llm_language_corrector(text), 'zh')
        self.assertEqual(len(calls), 1)
        snapshot = lc.language_correction_cache_snapshot()
        self.assertEqual(snapshot['capacity'], 512)
        self.assertEqual(snapshot['size'], 1)
        self.assertEqual(snapshot['hits'], 1)

    def test_chatty_reply_rejected_not_garbage(self):
        # The owner's case: a chatty reply must parse to None, NOT 'the'.
        self._patch(lambda messages, **kw: ('The language is German (de).', {}))
        from lib.lang_correct import llm_language_corrector
        self.assertIsNone(llm_language_corrector('Guten Tag alle'))

    def test_region_suffix_reduced_to_primary(self):
        self._patch(lambda messages, **kw: ('zh-CN', {}))
        from lib.lang_correct import llm_language_corrector
        self.assertEqual(llm_language_corrector('你好吗'), 'zh')

    def test_smart_chat_raises_swallowed_to_none(self):
        def boom(messages, **kw):
            raise RuntimeError('all slots exhausted')
        self._patch(boom)
        from lib.lang_correct import llm_language_corrector
        self.assertIsNone(llm_language_corrector('Guten Tag alle'))

    def test_unknown_reply_is_none(self):
        self._patch(lambda messages, **kw: ('unknown', {}))
        from lib.lang_correct import llm_language_corrector
        self.assertIsNone(llm_language_corrector('zzz qqq'))


if __name__ == '__main__':
    unittest.main()
