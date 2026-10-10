"""Current fast HTML upload baseline vs reduced repeated token work."""
import inspect
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmarks.knowledge_html import durable_measure


def worker(args):
    source = inspect.getsource(durable_measure.worker)
    original = 'from benchmarks.knowledge_html.durable import DurableHtmlCorpus, semantic_projection'
    assert source.count(original) == 1
    source = source.replace(original,
        'from benchmarks.knowledge_html.token_projection import IndexedHtmlCorpus as DurableHtmlCorpus, semantic_projection')
    namespace = dict(durable_measure.worker.__globals__)
    exec(compile(source, '<canonical-token-upload-worker>', 'exec'), namespace)
    return namespace['worker'](args)


def main():
    source = inspect.getsource(durable_measure.main)
    replacements = {
        "format='tofu.html-native-upload/v1'": "format='tofu.knowledge-token-upload/v1'",
        "('durable.py', 'check_durable.py', 'durable_measure.py', 'measure.py', 'reference.py')":
        "('token_projection.py', 'check_tokens.py', 'token_measure.py', 'durable.py', 'check_durable.py', 'durable_measure.py', 'measure.py', 'reference.py')",
        "scope='actual add_document, original file, extraction, chunk/search indexing, native commit and public content read'":
        "scope='actual upload/readback with current fast HTML in both arms; immutable old search tokens vs bounded per-call deduplication'",
    }
    for original, replacement in replacements.items():
        assert source.count(original) == 1
        source = source.replace(original, replacement)
    namespace = dict(durable_measure.main.__globals__, __file__=__file__, __doc__=__doc__, worker=worker)
    exec(compile(source, '<canonical-token-upload-pairs>', 'exec'), namespace)
    namespace['main']()


if __name__ == '__main__':
    main()
