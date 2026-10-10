"""Actual article upload phase diagnosis; instrumented, no performance admission."""
import inspect
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmarks.knowledge_html import durable_measure


def worker(args):
    source = inspect.getsource(durable_measure.worker)
    replacements = {
        'from benchmarks.knowledge_html.durable import DurableHtmlCorpus, semantic_projection':
        'from benchmarks.knowledge_html.upload_phases import ProfiledHtmlCorpus as DurableHtmlCorpus, semantic_projection',
        'return dict(pid=os.getpid(), raw_ms=walls, raw_cpu_ms=cpus,':
        'return dict(phase_samples=corpus.phase_samples[21:], pid=os.getpid(), raw_ms=walls, raw_cpu_ms=cpus,',
    }
    for original, replacement in replacements.items():
        assert source.count(original) == 1
        source = source.replace(original, replacement)
    namespace = dict(durable_measure.worker.__globals__)
    exec(compile(source, '<canonical-upload-phase-worker>', 'exec'), namespace)
    result = namespace['worker'](args)
    assert args.case == 'article' and len(result['phase_samples']) == args.samples
    required = {'digest_lookup', 'extract', 'chunk', 'search_index', 'source_write_fsync', 'native_visual_settings',
                'native_create', 'native_content_read', 'upload_total', 'public_read_total'}
    assert all(set(sample) == required for sample in result['phase_samples'])
    return result


def main():
    source = inspect.getsource(durable_measure.main)
    replacements = {
        "format='tofu.html-native-upload/v1'": "format='tofu.html-upload-phase-diagnosis/v1'",
        "for case in ('article', 'table', 'batch'):": "for case in ('article',):",
        "('durable.py', 'check_durable.py', 'durable_measure.py', 'measure.py', 'reference.py')":
        "('upload_phases.py', 'upload_phases_measure.py', 'durable.py', 'check_durable.py', 'durable_measure.py', 'measure.py', 'reference.py')",
        "limitations=['no HTTP/download/upload transport', 'synthetic HTML fixtures',":
        "limitations=['instrumented diagnosis only; original article tail rejection unchanged', 'no HTTP/download/upload transport', 'synthetic HTML fixtures',",
    }
    for original, replacement in replacements.items():
        assert source.count(original) == 1
        source = source.replace(original, replacement)
    namespace = dict(durable_measure.main.__globals__, __file__=__file__, __doc__=__doc__, worker=worker)
    exec(compile(source, '<canonical-upload-phase-pairs>', 'exec'), namespace)
    namespace['main']()


if __name__ == '__main__':
    main()
