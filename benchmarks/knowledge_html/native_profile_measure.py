"""Owned article uploads with native request profiling off/on/on/off.

Uses the current fast HTML and immutable current token baseline in every arm.
Native profiles include cold/warmup dispatches, not only timed observations.
The checked native ledger diagnoses costs; it does not establish admission.
"""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmarks.knowledge_html import durable_measure
from scripts.tofudb_profile_report import analyze


def worker(args):
    assert args.variant == 'reference' and args.case == 'article'
    source = inspect.getsource(durable_measure.worker)
    replacements = {
        'from benchmarks.knowledge_html.durable import DurableHtmlCorpus, semantic_projection':
        'from benchmarks.knowledge_html.token_projection import IndexedHtmlCorpus as DurableHtmlCorpus, semantic_projection',
        'expected = encode(pages)':
        "native_profile = corpus.client.query('system.metrics', {})['requestProfile']\n            expected = encode(pages)",
        'return dict(pid=os.getpid(), raw_ms=walls, raw_cpu_ms=cpus,':
        'return dict(native_profile=native_profile, pid=os.getpid(), raw_ms=walls, raw_cpu_ms=cpus,',
    }
    for original, replacement in replacements.items():
        assert source.count(original) == 1
        source = source.replace(original, replacement)
    namespace = dict(durable_measure.worker.__globals__)
    exec(compile(source, '<canonical-native-request-profile-worker>', 'exec'), namespace)
    result = namespace['worker'](args)
    profile = result['native_profile']
    if args.profile == 'on':
        result['native_profile_analysis'] = analyze(profile)
        assert profile['sampleEvery'] == 1 and profile['dropped'] == 0
    else:
        assert profile == {'enabled': False}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--profile', choices=['off', 'on'])
    parser.add_argument('--variant', default='reference', choices=['reference'])
    parser.add_argument('--case', default='article', choices=['article'])
    args = parser.parse_args()
    args.engine = args.engine.resolve()
    if args.samples < 1:
        parser.error('--samples must be positive')
    if args.worker:
        expected = '1' if args.profile == 'on' else '0'
        assert os.environ.get('TOFU_DB_REQUEST_PROFILE_EVERY') == expected
        print(json.dumps(worker(args)))
        return
    if args.output is None:
        parser.error('--output is required')
    names = ('benchmarks/knowledge_html/native_profile_measure.py',
             'benchmarks/knowledge_html/token_projection.py', 'benchmarks/knowledge_html/durable.py',
             'benchmarks/knowledge_html/durable_measure.py', 'benchmarks/knowledge_html/reference.py',
             'benchmarks/knowledge_html/measure.py', 'scripts/tofudb_profile_report.py',
             'lib/knowledge/store.py', 'lib/knowledge/ingest.py', 'lib/knowledge/html_text.py',
             'lib/knowledge/chunking.py', 'lib/knowledge/repository.py', 'tofu_db/embedded.py',
             'lib/storage/v2_client.py', 'lib/storage/v2_embedded_client.py')
    report = dict(format='tofu.html-native-request-profile/v1', whole_system_verdict='incomplete',
                  production_promoted=False, samples=args.samples,
                  scope='current fast HTML/current immutable tokens, owned article uploads; native profiling observer off/on/on/off',
                  limitations=['diagnostic only; does not waive any uninstrumented tail failure',
                               'profile includes cold and warmup dispatches; raw wall vectors exclude them',
                               'request profile excludes client conversion/delivery; background work is not foreground CPU',
                               'slow samples are bounded native sequence numbers, not upload correlation IDs'],
                  sources={n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in names}, trials=[])
    for mode in ('off', 'on', 'on', 'off'):
        def invoke():
            env = dict(os.environ, TOFU_DB_REQUEST_PROFILE_EVERY='1' if mode == 'on' else '0')
            command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), str(Path(__file__).resolve()),
                       '--worker', '--profile', mode, '--engine', str(args.engine), '--samples', str(args.samples)]
            completed = subprocess.run(command, env=env, check=True, capture_output=True, text=True, timeout=1800)
            return json.loads(completed.stdout)
        latency, memory = invoke(), invoke()
        assert latency['pid'] != memory['pid']
        for key in ('trace_sha256', 'fixture_sha256', 'library_sha256', 'native_build_identity', 'reference_source_sha256'):
            assert latency[key] == memory[key]
            if report['trials']:
                assert latency[key] == report['trials'][0]['measurement'][key]
        latency['memory_pid'] = memory['pid']
        latency['peak_process_rss_bytes'] = memory['peak_process_rss_bytes']
        report['trials'].append({'mode': mode, 'measurement': latency})
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(mode, {key: latency[key] for key in ('p50_ms', 'p99_ms', 'cpu_ms_per_operation', 'peak_process_rss_bytes')}, flush=True)


if __name__ == '__main__':
    main()
