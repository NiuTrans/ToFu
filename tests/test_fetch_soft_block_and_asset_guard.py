"""Guards for the fetch_url soft-failure → bogus-"file asset" retry loop.

Symptom this pins (2026-07-25): a geo-blocked doc host answers **HTTP 200,
``text/html``, 476,954 bytes** whose body is "App unavailable — Claude is only
available in certain regions". The text pipeline extracts the error shell, the
LLM content filter correctly rules it ``[IRRELEVANT]`` → ``page_content=None``,
and ``_fetch_url_one`` then treated that empty result as "extraction failed"
and fell into ``_stage_binary_asset``. That re-downloaded the same 476 KB and
handed the model a self-contradictory note:

    This URL is a file asset (text/html; charset=utf-8, 476,954 bytes),
    not a readable web page …

The model concluded the tool was broken and retried sibling host variants
(docs.anthropic.com → docs.claude.com → platform.claude.com), each burning a
fresh 476 KB download, a fresh disk write, and a fresh content-filter LLM call.

The four invariants asserted here:
  1. ``_stage_binary_asset`` REFUSES textual content types outright — an HTML
     body is never a "file asset".
  2. ``_fetch_url_one`` carries a TYPED ``reason``; only a genuine extraction
     failure may reach asset staging, and an ``irrelevant`` verdict is reported
     as such instead of being laundered into an asset note.
  3. A soft block (HTTP 200 + region/unavailability shell) is detected and the
     message tells the model the whole HOST is unreachable — killing the
     host-variant retry loop, not just the one URL.
  4. A real binary asset whose URL has no extension still gets a sane
     extension, derived from the content type.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tasks_pkg.handlers.search import _core  # noqa: E402


#: The real geo-block shell, as extracted by fetch_page_content from the live
#: 476 KB SPA response (verified 2026-07-25 against docs.anthropic.com).
GEO_BLOCK_TEXT = (
    '# App unavailable\n\n'
    'Unfortunately, Claude is only available in certain regions right now. '
    'Please contact support if you think you\u2019re getting this message in error.\n\n'
    'View supported countries\n\n'
    'Thank you! Your submission has been received!\n\n'
    'Oops! Something went wrong while submitting the form.\n'
)

GEO_URL = 'https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching'


class TestStageBinaryAssetRefusesText(unittest.TestCase):
    """Invariant 1 — textual content types are never "file assets"."""

    def test_html_content_type_is_refused(self):
        html = b'<!doctype html><html><body>App unavailable</body></html>' * 100
        with patch.object(_core, 'fetch_url_bytes',
                          return_value=(html, 'text/html; charset=utf-8')):
            got = _core._stage_binary_asset(GEO_URL)
        self.assertIsNone(
            got, '_stage_binary_asset must refuse text/html — an HTML body is '
                 'never a file asset (this is the bogus note the model saw)')

    def test_other_textual_types_refused(self):
        for ct in ('text/plain', 'application/xhtml+xml',
                   'text/html', 'TEXT/HTML; charset=utf-8'):
            with patch.object(_core, 'fetch_url_bytes',
                              return_value=(b'x' * 500, ct)):
                self.assertIsNone(
                    _core._stage_binary_asset('https://x.test/thing'),
                    f'content type {ct!r} must be refused as an asset')

    def test_nothing_is_written_to_disk_when_refused(self):
        """The refusal must happen BEFORE the write — no 476 KB leak per retry.

        Asserts the observable outcome (no new file in the staging dir) rather
        than patching ``builtins.open``: that patch also captures logging's own
        file writes, which makes the test pass alone but fail once another
        suite has initialized log handlers first.
        """
        from lib.config_dir import fetched_path
        staging_dir = os.path.dirname(fetched_path('probe'))
        before = set(os.listdir(staging_dir)) if os.path.isdir(staging_dir) else set()

        html = b'<html>' + b'y' * 5000
        with patch.object(_core, 'fetch_url_bytes',
                          return_value=(html, 'text/html')):
            got = _core._stage_binary_asset(GEO_URL)

        after = set(os.listdir(staging_dir)) if os.path.isdir(staging_dir) else set()
        self.assertIsNone(got)
        self.assertEqual(
            after - before, set(),
            'a refused textual body must leave NOTHING on disk — this is the '
            '476 KB-per-retry leak')

    def test_real_binary_asset_still_staged(self):
        png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 2048
        with patch.object(_core, 'fetch_url_bytes',
                          return_value=(png, 'image/png')):
            got = _core._stage_binary_asset('https://x.test/pic.png')
        self.assertIsNotNone(got, 'a genuine binary asset must still stage')
        self.assertTrue(got['is_asset'])
        self.assertIn('file asset', got['page_content'])
        if got.get('saved_path'):
            try:
                os.unlink(got['saved_path'])
            except OSError:
                pass


class TestAssetExtensionFromContentType(unittest.TestCase):
    """Invariant 4 — extensionless URL + known content type ⇒ sane extension."""

    def test_extension_derived_when_url_has_none(self):
        png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 512
        with patch.object(_core, 'fetch_url_bytes',
                          return_value=(png, 'image/png')):
            got = _core._stage_binary_asset('https://x.test/media/no-ext-here')
        self.assertIsNotNone(got)
        saved = got['saved_path']
        self.assertTrue(
            saved.endswith('.png'),
            f'staged path {saved!r} must carry a content-type-derived extension '
            'so read_files can dispatch on it')
        try:
            os.unlink(saved)
        except OSError:
            pass

    def test_url_extension_wins_when_present(self):
        pdf = b'%PDF-1.4' + b'\x00' * 512
        with patch.object(_core, 'fetch_url_bytes',
                          return_value=(pdf, 'application/pdf')):
            got = _core._stage_binary_asset('https://x.test/paper.pdf')
        self.assertIsNotNone(got)
        self.assertTrue(got['saved_path'].endswith('.pdf'))
        try:
            os.unlink(got['saved_path'])
        except OSError:
            pass


class TestSoftBlockDetection(unittest.TestCase):
    """Invariant 3 — HTTP 200 + unavailability shell ⇒ host-level verdict."""

    def test_geo_block_shell_is_detected(self):
        self.assertTrue(_core._looks_soft_blocked(GEO_BLOCK_TEXT),
                        'the live geo-block shell must be recognised')

    def test_real_content_is_not_flagged(self):
        real = (
            '# Prompt caching\n\nPrompt caching is a feature that optimizes '
            'your API usage by allowing resuming from specific prefixes in '
            'your prompts. Cache breakpoints are declared with '
            'cache_control. The cache has a 5-minute lifetime, refreshed '
            'each time the cached content is used.\n'
        ) * 6
        self.assertFalse(_core._looks_soft_blocked(real),
                         'genuine documentation must never be flagged as a '
                         'soft block (false positive would break fetching)')

    def test_empty_and_short_text_not_flagged(self):
        self.assertFalse(_core._looks_soft_blocked(''))
        self.assertFalse(_core._looks_soft_blocked(None))

    def test_long_shell_still_detected(self):
        """The REAL shell is ~12.7 K chars, not short.

        The live SPA drags its nav/footer junk into the extraction, so the
        geo-block page came back at 12,692 chars. A total-length ceiling
        therefore silently misses the exact page this fix exists for; the
        marker must be matched in the document HEAD instead.
        """
        bloated = GEO_BLOCK_TEXT + ('\nsupported countries navigation footer link ' * 400)
        self.assertGreater(len(bloated), 12000)
        self.assertTrue(
            _core._looks_soft_blocked(bloated),
            'a 12.7 K-char geo-block shell must still be detected — this is '
            'the live docs.anthropic.com response shape')

    def test_article_mentioning_geoblocking_in_body_not_flagged(self):
        """False-positive guard: the notice must LEAD, not merely appear."""
        article = (
            '# Designing resilient fetch pipelines\n\n'
            + ('Real technical prose about retries and backoff strategy. ' * 60)
            + '\n\nSome services reply that a product is only available in '
              'certain regions, which clients must handle explicitly.\n'
            + ('More real analysis of caching and content extraction. ' * 60)
        )
        self.assertFalse(
            _core._looks_soft_blocked(article),
            'a genuine article discussing geo-blocking in its BODY must not '
            'be dropped as a block interstitial')

    def test_soft_block_names_the_host_and_forbids_retry(self):
        """The model tried 3 host variants — the message must kill the HOST."""
        with patch.object(_core._facade_mod(), 'fetch_page_content',
                          return_value=GEO_BLOCK_TEXT):
            with patch.object(_core, '_stage_binary_asset') as m_stage:
                item = _core._fetch_url_one(GEO_URL, 'prompt caching', 'read docs')

        self.assertEqual(m_stage.call_count, 0,
                         'a soft-blocked page must NEVER reach asset staging')
        self.assertEqual(item['reason'], 'soft_blocked')
        self.assertIsNone(item['page_content'])
        self.assertFalse(item['is_asset'])
        msg = item['error_msg'] or ''
        self.assertIn('docs.anthropic.com', msg,
                      'the verdict must name the unreachable HOST')
        low = msg.lower()
        self.assertTrue(
            'do not retry' in low or 'don\'t retry' in low,
            f'the verdict must tell the model to stop retrying this host: {msg!r}')


class TestTypedReasonGatesAssetStaging(unittest.TestCase):
    """Invariant 2 — irrelevant ≠ extraction-failed; only the latter stages."""

    def test_irrelevant_does_not_stage_an_asset(self):
        from tofu_search.fetch.content_filter import IRRELEVANT_SENTINEL
        with patch.object(_core._facade_mod(), 'fetch_page_content',
                          return_value='some genuinely off-topic prose ' * 40):
            with patch.object(_core, 'filter_web_content',
                              return_value=IRRELEVANT_SENTINEL):
                with patch.object(_core, '_stage_binary_asset') as m_stage:
                    item = _core._fetch_url_one(
                        'https://x.test/some/page', 'q', 'reason')

        self.assertEqual(
            m_stage.call_count, 0,
            'an IRRELEVANT verdict is a SEMANTIC judgement, not an extraction '
            'failure — it must not be laundered into a "file asset" note')
        self.assertEqual(item['reason'], 'irrelevant')
        self.assertFalse(item['is_asset'])
        self.assertIn('irrelevant', (item['error_msg'] or '').lower())

    def test_genuine_extraction_failure_still_stages(self):
        with patch.object(_core._facade_mod(), 'fetch_page_content',
                          return_value=None):
            with patch.object(_core, '_stage_binary_asset',
                              return_value={'page_content': 'note',
                                            'raw_chars': 10,
                                            'filtered_chars': 4,
                                            'saved_path': '/tmp/x.png',
                                            'is_asset': True}) as m_stage:
                item = _core._fetch_url_one('https://x.test/pic.png', 'q', 'r')
        self.assertEqual(m_stage.call_count, 1,
                         'a real extraction failure must still try staging')
        self.assertTrue(item['is_asset'])
        self.assertEqual(item['reason'], 'asset')

    def test_success_carries_extracted_ok(self):
        with patch.object(_core._facade_mod(), 'fetch_page_content',
                          return_value='real page text ' * 50):
            with patch.object(_core, 'filter_web_content',
                              side_effect=lambda txt, **kw: txt):
                item = _core._fetch_url_one('https://x.test/ok', 'q', 'r')
        self.assertEqual(item['reason'], 'extracted_ok')
        self.assertTrue(item['page_content'])
        self.assertIsNone(item['error_msg'])

    def test_reason_key_always_present(self):
        """Every return path must carry `reason` — consumers may rely on it."""
        item = _core._fetch_url_one('ftp://x.test/nope', 'q', 'r')
        self.assertIn('reason', item)
        self.assertEqual(item['reason'], 'rejected')


if __name__ == '__main__':
    unittest.main(verbosity=2)
