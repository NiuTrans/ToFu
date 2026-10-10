"""Seeded token differential and actual native-upload correctness checks."""
import argparse
import inspect
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmarks.knowledge_html import check_durable
from benchmarks.knowledge_html.token_projection import IndexedHtmlCorpus, load_reference_tokens, optimized_tokens


def check_tokens():
    reference, _ = load_reference_tokens()
    randomizer = random.Random(20261008)
    parts = ['中文', '文档内容', 'a.b/c', 'A.B/C', 'x', 'identifier_42', 'a' * 129,
             'a' * 129 + '.good', '', ' \n\t', '日本語', 'क', '💡', '𠀀', '12.34', '#tag', '-x-y-', '你好世界']
    corpus = [None, '', '中文 中文', 'A.B a.b A.B', 'a' * 100000,
              '中文 ' * 20000, ' '.join(f'word{index}' for index in range(1000)),
              ''.join(chr(0x4e00 + index) for index in range(3000))]
    corpus.extend(' '.join(randomizer.choice(parts) for _ in range(randomizer.randrange(1, 50)))
                  for _ in range(1500))
    count = 0
    for text in corpus:
        for cap in (-1, 0, 1, 2, 16, 32, 256, 4096):
            assert optimized_tokens(text, cap=cap) == reference(text, cap=cap), (count, cap)
            count += 1
    print('PASS: exact token order, identifiers, CJK, long text and cap behavior', count)


def check_native(engine):
    source = inspect.getsource(check_durable.check)
    namespace = dict(check_durable.check.__globals__, DurableHtmlCorpus=IndexedHtmlCorpus)
    exec(compile(source, '<canonical-indexed-html-native-check>', 'exec'), namespace)
    namespace['check'](engine)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, type=Path)
    args = parser.parse_args()
    check_tokens()
    check_native(args.engine.resolve())
