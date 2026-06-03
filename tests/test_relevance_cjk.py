"""tests/test_relevance_cjk.py — CJK tokenization for memory BM25 search.

Guards the fix where the Latin-only tokenizer (`[^a-z0-9_]+`) stripped all
CJK characters, making every Chinese/Japanese memory near-invisible to BM25.
CJK runs are now tokenized into overlapping bigrams.
"""

import unittest

from lib.memory.relevance import _cjk_tokens, _tokenize, filter_relevant_memories


class CJKTokenizeTest(unittest.TestCase):

    def test_bigrams(self):
        self.assertEqual(_cjk_tokens('中文海报'), ['中文', '文海', '海报'])

    def test_single_char_unigram(self):
        self.assertEqual(_cjk_tokens('好'), ['好'])

    def test_empty(self):
        self.assertEqual(_cjk_tokens(''), [])

    def test_latin_only_yields_no_cjk_tokens(self):
        self.assertEqual(_cjk_tokens('hello world 123'), [])

    def test_kana_handled(self):
        # Japanese hiragana run → bigrams (searchability, not linguistics).
        self.assertEqual(_cjk_tokens('こんにち'), ['こん', 'んに', 'にち'])

    def test_mixed_text_keeps_both_streams(self):
        toks = _tokenize('生成海报 generate_image NO-TEXT')
        # Latin sub-tokens preserved (snake_case split, stop words dropped)
        self.assertIn('generate', toks)
        self.assertIn('image', toks)
        self.assertIn('text', toks)
        # CJK bigrams present
        self.assertIn('生成', toks)
        self.assertIn('海报', toks)

    def test_latin_tokenization_unchanged(self):
        # Regression: the pre-existing Latin behavior must not drift.
        self.assertEqual(
            _tokenize('flask_migration-test'),
            ['flask', 'migration', 'test'],
        )


class CJKRetrievalTest(unittest.TestCase):

    def _corpus(self):
        return [
            {'id': 'cn', 'name': 'image gen chinese',
             'description': 'generate_image 无法准确渲染中文字符 海报标题应生成纯背景',
             'tags': ['image-gen', 'chinese'], 'body': '中文海报渲染缺陷'},
            {'id': 'en1', 'name': 'proxy install',
             'description': 'bake corp proxy into install.sh', 'tags': [], 'body': ''},
            {'id': 'en2', 'name': 'streaming output',
             'description': 'run_command streaming via tool_progress sse', 'tags': [], 'body': ''},
            {'id': 'en3', 'name': 'cache breakpoints',
             'description': 'anthropic prompt cache breakpoints mixed ttl', 'tags': [], 'body': ''},
        ]

    def test_pure_cjk_query_retrieves_cjk_memory(self):
        corpus = self._corpus()
        # top_k < len(corpus) so BM25 actually ranks (no passthrough).
        res = filter_relevant_memories(corpus, '中文海报渲染', top_k=2)
        self.assertEqual(res[0]['id'], 'cn')

    def test_latin_query_unaffected_by_cjk_support(self):
        corpus = self._corpus()
        res = filter_relevant_memories(corpus, 'run_command streaming tool_progress', top_k=2)
        self.assertEqual(res[0]['id'], 'en2')


if __name__ == '__main__':
    unittest.main()
