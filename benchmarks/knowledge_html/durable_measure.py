"""Paired actual HTML uploads/readback against private native durable stores."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def fixtures(case, index):
    from benchmarks.knowledge_html.measure import inputs
    # Distinct digests force every batch member through extraction and commit.
    return [(raw + f'<!--document {ordinal}-->'.encode(), name)
            for ordinal, (raw, name) in enumerate(inputs(case, index))]


def worker(args):
    from benchmarks.knowledge_html.durable import DurableHtmlCorpus, semantic_projection
    with tempfile.TemporaryDirectory(prefix='tofu-html-native-measure-') as directory:
        corpus = DurableHtmlCorpus(Path(directory), args.engine, variant=args.variant)
        try:
            def run(documents):
                pages = []
                for raw, name in documents:
                    result = corpus.add_document(raw, name, user_id=1)
                    assert not result['duplicate']
                    page = corpus.get_document_content(result['id'], user_id=1, limit=200)
                    assert len(page['chunks']) == result['chunk_count']
                    pages.append((raw, result, page))
                return pages
            def encode(pages):
                for raw, result, _ in pages:
                    sources = list((corpus.root / 'sources' / '1' / 'sources').glob(
                        f'{hashlib.sha256(raw).hexdigest()}*'))
                    assert len(sources) == 1 and sources[0].read_bytes() == raw
                return json.dumps([semantic_projection(page) for _, _, page in pages],
                                  sort_keys=True, ensure_ascii=True).encode()
            cold_inputs = fixtures(args.case, -1)
            started = time.perf_counter_ns()
            cold = run(cold_inputs)
            cold_ms = (time.perf_counter_ns() - started) / 1e6
            trace = hashlib.sha256(encode(cold))
            fixture_hash = hashlib.sha256()
            walls, cpus = [], []
            for index in range(20 + args.samples):
                documents = fixtures(args.case, index)
                for raw, name in documents:
                    fixture_hash.update(len(raw).to_bytes(8, 'little') + raw + name.encode())
                cpu_started = time.process_time_ns()
                started = time.perf_counter_ns()
                pages = run(documents)
                elapsed = (time.perf_counter_ns() - started) / 1e6
                cpu = (time.process_time_ns() - cpu_started) / 1e6
                if index >= 20:
                    walls.append(elapsed)
                    cpus.append(cpu)
                trace.update(encode(pages))
            expected = encode(pages)
            ids = [result['id'] for _, result, _ in pages]
            corpus.close()
            corpus.open()
            reopened = [corpus.get_document_content(identity, user_id=1, limit=200) for identity in ids]
            assert json.dumps([semantic_projection(page) for page in reopened],
                              sort_keys=True, ensure_ascii=True).encode() == expected
            ordered = sorted(walls)
            return dict(pid=os.getpid(), raw_ms=walls, raw_cpu_ms=cpus,
                        warmup_operations=20, cold_ms=cold_ms,
                        p50_ms=statistics.median(walls),
                        p95_ms=ordered[max(0, int(len(ordered) * .95) - 1)],
                        p99_ms=ordered[max(0, int(len(ordered) * .99) - 1)],
                        cpu_ms_per_operation=statistics.mean(cpus),
                        throughput_per_second=1000 / statistics.mean(walls),
                        documents_per_operation=len(documents),
                        documents_per_second=len(documents) * 1000 / statistics.mean(walls),
                        peak_process_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                        trace_sha256=trace.hexdigest(), fixture_sha256=fixture_hash.hexdigest(),
                        native_build_identity=corpus.build_identity,
                        native_resource_budget=corpus.resource_budget,
                        library_sha256=corpus.library_sha256,
                        reference_source_sha256=corpus.reference_source_sha256)
        finally:
            corpus.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--variant', choices=['reference', 'candidate'])
    parser.add_argument('--case', choices=['article', 'table', 'batch'])
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    args = parser.parse_args()
    args.engine = args.engine.resolve()
    if args.samples < 1 or args.pairs < 1:
        parser.error('samples and pairs must be positive')
    if args.worker:
        print(json.dumps(worker(args)))
        return
    if args.output is None:
        parser.error('--output is required')
    report = dict(format='tofu.html-native-upload/v1', whole_system_verdict='incomplete',
                  production_promoted=False,
                  scope='actual add_document, original file, extraction, chunk/search indexing, native commit and public content read',
                  limitations=['no HTTP/download/upload transport', 'synthetic HTML fixtures',
                               'no PDF/cancellation/crash/scaled concurrency admission',
                               'peak RSS includes initialization and reopen; no retained-memory isolation'],
                  runtime=dict(python=sys.version, platform=platform.platform(), affinity=sorted(os.sched_getaffinity(0))),
                  sources={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in [ROOT / 'benchmarks/knowledge_html' / name for name in
                                        ('durable.py', 'check_durable.py', 'durable_measure.py', 'measure.py', 'reference.py')]
                           + [ROOT / path for path in ('lib/knowledge/ingest.py', 'lib/knowledge/html_text.py',
                              'lib/knowledge/store.py', 'lib/knowledge/chunking.py', 'lib/knowledge/repository.py',
                              'lib/storage/v2_embedded_client.py', 'lib/storage/v2_client.py', 'tofu_db/embedded.py')]},
                  samples=args.samples, required_pairs=args.pairs, cases=[])
    for case in ('article', 'table', 'batch'):
        cohort = dict(case=case, pairs=[])
        report['cases'].append(cohort)
        for pair in range(args.pairs):
            results = {}
            for variant in (('reference', 'candidate') if pair % 2 == 0 else ('candidate', 'reference')):
                def invoke():
                    completed = subprocess.run([
                        os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), str(Path(__file__).resolve()),
                        '--worker', '--engine', str(args.engine), '--case', case, '--variant', variant,
                        '--samples', str(args.samples)], check=True, capture_output=True, text=True, timeout=1800)
                    return json.loads(completed.stdout)
                latency, memory = invoke(), invoke()
                assert latency['pid'] != memory['pid']
                for key in ('trace_sha256', 'fixture_sha256', 'native_build_identity', 'library_sha256'):
                    assert latency[key] == memory[key]
                latency['memory_pid'] = memory['pid']
                latency['peak_process_rss_bytes'] = memory['peak_process_rss_bytes']
                results[variant] = latency
            for key in ('trace_sha256', 'fixture_sha256', 'native_build_identity', 'library_sha256', 'reference_source_sha256'):
                assert results['reference'][key] == results['candidate'][key]
            cohort['pairs'].append(results)
            args.output.write_text(json.dumps(report, indent=2) + '\n')
            print(case, pair, {key: round(results['candidate'][key] / results['reference'][key], 3)
                               for key in ('p50_ms', 'p99_ms', 'cpu_ms_per_operation', 'peak_process_rss_bytes')}, flush=True)


if __name__ == '__main__':
    main()
