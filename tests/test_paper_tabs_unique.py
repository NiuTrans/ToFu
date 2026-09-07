"""Guard: paper-reader tabs must be unique in index.html.

Root bug (2026-07-25, shipped at HEAD in f6f4d4bf): the podcast tab button,
its panel AND its <script> tag were all pasted TWICE (identical copies, incl.
a duplicate id="paperPodcastContent"), so the reader rendered two 播客 tabs.
An insert-anchor duplication during the video-tab wiring landed silently
because nothing checked tab uniqueness.

Checks (static, against the served index.html):
  1. each data-tab value appears exactly once among .paper-tab-btn;
  2. each data-tab value appears exactly once among .paper-tab-panel;
  3. every static/js <script defer src> appears exactly once;
  4. paper-region element ids (paper[A-Z]* / paperQ* …) are unique.
"""

import re
import unittest
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'


class TestPaperTabsUnique(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding='utf-8')

    def test_tab_buttons_unique(self):
        tabs = re.findall(
            r'<button class="paper-tab-btn[^"]*" data-tab="([^"]+)"', self.html)
        dups = {t: n for t, n in Counter(tabs).items() if n != 1}
        self.assertEqual(dups, {}, f'duplicate paper tab buttons: {dups}')
        self.assertTrue(tabs, 'no paper tab buttons found — markup drifted?')

    def test_tab_panels_unique(self):
        panels = re.findall(
            r'<div class="paper-tab-panel" data-tab="([^"]+)"', self.html)
        dups = {t: n for t, n in Counter(panels).items() if n != 1}
        self.assertEqual(dups, {}, f'duplicate paper tab panels: {dups}')
        self.assertTrue(panels, 'no paper tab panels found — markup drifted?')

    def test_buttons_and_panels_match(self):
        buttons = set(re.findall(
            r'<button class="paper-tab-btn[^"]*" data-tab="([^"]+)"', self.html))
        panels = set(re.findall(
            r'<div class="paper-tab-panel" data-tab="([^"]+)"', self.html))
        self.assertEqual(buttons, panels,
                         'tab buttons and panels disagree — a tab would render '
                         'blank or unreachable')

    def test_script_tags_unique(self):
        srcs = re.findall(
            r'<script defer src="(static/js/[^"]+)"', self.html)
        dups = {s: n for s, n in Counter(srcs).items() if n != 1}
        self.assertEqual(dups, {}, f'duplicate <script> tags: {dups}')

    def test_paper_region_ids_unique(self):
        ids = re.findall(r'id="(paper[A-Za-z0-9]+)"', self.html)
        dups = {i: n for i, n in Counter(ids).items() if n != 1}
        self.assertEqual(dups, {}, f'duplicate paper-region element ids: {dups}')


if __name__ == '__main__':
    unittest.main()
