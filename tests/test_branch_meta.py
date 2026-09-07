"""tests/test_branch_meta.py — port-parity tests for lib.branch_meta."""

from __future__ import annotations

import unittest

from lib.branch_meta import branch_auto_icon, classify_branch_title


class BranchAutoIconTest(unittest.TestCase):

    def test_empty_returns_empty(self):
        self.assertEqual(branch_auto_icon(''), '')
        self.assertEqual(branch_auto_icon(None), '')
        self.assertEqual(branch_auto_icon(123), '')  # type: ignore[arg-type]

    def test_paper_keywords(self):
        for t in ['Paper review', 'arXiv 2401.xxx', '论文笔记',
                   'paper · 阅读']:
            # Even though the icon is empty for parity with JS, the
            # function must not raise.
            self.assertEqual(branch_auto_icon(t), '')

    def test_returns_string_for_non_string_inputs(self):
        # Must NOT raise on weird inputs.
        self.assertEqual(branch_auto_icon(0), '')
        self.assertEqual(branch_auto_icon([]), '')


class ClassifyBranchTitleTest(unittest.TestCase):

    def test_generic_for_empty(self):
        self.assertEqual(classify_branch_title(''),
                          {'icon': '', 'kind': 'generic'})
        self.assertEqual(classify_branch_title(None),
                          {'icon': '', 'kind': 'generic'})

    def test_paper_kind(self):
        self.assertEqual(classify_branch_title('Paper review')['kind'],
                          'paper')
        self.assertEqual(classify_branch_title('论文笔记')['kind'], 'paper')
        self.assertEqual(classify_branch_title('arXiv 2401')['kind'], 'paper')

    def test_code_kind(self):
        self.assertEqual(classify_branch_title('Code review')['kind'],
                          'code')
        self.assertEqual(classify_branch_title('实现 X')['kind'], 'code')
        self.assertEqual(classify_branch_title('代码 patch')['kind'],
                          'code')

    def test_data_kind(self):
        self.assertEqual(classify_branch_title('dataset analysis')['kind'],
                          'data')
        self.assertEqual(classify_branch_title('数据清洗')['kind'], 'data')

    def test_math_kind(self):
        self.assertEqual(classify_branch_title('proof outline')['kind'],
                          'math')
        self.assertEqual(classify_branch_title('数学公式推导')['kind'],
                          'math')
        self.assertEqual(classify_branch_title('证明过程')['kind'],
                          'math')

    def test_image_kind(self):
        self.assertEqual(classify_branch_title('image gen')['kind'],
                          'image')
        self.assertEqual(classify_branch_title('Visual analysis')['kind'],
                          'image')
        self.assertEqual(classify_branch_title('vision pipeline')['kind'],
                          'image')

    def test_compare_kind(self):
        self.assertEqual(classify_branch_title('compare A B')['kind'],
                          'compare')
        self.assertEqual(classify_branch_title('A vs B')['kind'],
                          'compare')
        self.assertEqual(classify_branch_title('对比方案')['kind'],
                          'compare')

    def test_bug_kind(self):
        self.assertEqual(classify_branch_title('bug repro')['kind'],
                          'bug')
        self.assertEqual(classify_branch_title('error trace')['kind'],
                          'bug')
        self.assertEqual(classify_branch_title('问题修复')['kind'],
                          'bug')

    def test_todo_kind(self):
        self.assertEqual(classify_branch_title('TODO list')['kind'],
                          'todo')
        self.assertEqual(classify_branch_title('plan v2')['kind'],
                          'todo')
        self.assertEqual(classify_branch_title('计划 Q1')['kind'],
                          'todo')

    def test_idea_kind(self):
        self.assertEqual(classify_branch_title('Idea sketch')['kind'],
                          'idea')
        self.assertEqual(classify_branch_title('想法集')['kind'], 'idea')
        self.assertEqual(classify_branch_title('thought dump')['kind'],
                          'idea')

    def test_summary_kind(self):
        self.assertEqual(classify_branch_title('Summary')['kind'],
                          'summary')
        self.assertEqual(classify_branch_title('总结')['kind'], 'summary')
        self.assertEqual(classify_branch_title('概述')['kind'], 'summary')

    def test_generic_fallback(self):
        self.assertEqual(classify_branch_title('Random title')['kind'],
                          'generic')
        self.assertEqual(classify_branch_title('hello world')['kind'],
                          'generic')

    def test_first_match_wins(self):
        # When a title matches multiple patterns, the FIRST one in
        # _PATTERNS order wins. paper precedes code so this is 'paper'.
        self.assertEqual(classify_branch_title('paper code')['kind'],
                          'paper')

    def test_case_insensitive(self):
        self.assertEqual(classify_branch_title('PAPER')['kind'], 'paper')
        self.assertEqual(classify_branch_title('Paper')['kind'], 'paper')
        self.assertEqual(classify_branch_title('paper')['kind'], 'paper')


if __name__ == '__main__':
    unittest.main()
