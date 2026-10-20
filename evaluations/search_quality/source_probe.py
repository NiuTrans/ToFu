"""Paired free retrieval against frozen community source annotations.

Entry: --corpus LOCAL.csv prints an offline plan; --live executes that plan.
Reuses the existing disposable Workers and Artifacts owners, with no model,
agent loop, download, automatic retry or credential selection of its own.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .contracts import ARMS, DDGS_VERSION, RESULTS, TOOL_SECONDS, canonical, digest, python_tree_digest, url_key
from .runner import Artifacts, Workers, source_fingerprint
from .source_corpus import dataset_identity, load_corpus, read_bytes, select_cases

CONTRACT = 'tofu.community-source-probe/v1'


def successful_rows(response):
    """Failed operations cannot grant retrieval credit or follow-up authority."""
    rows = [] if response.get('error') else response.get('results')
    if not isinstance(rows, list) or len(rows) > RESULTS:
        raise ValueError('Unexpected search result budget or shape')
    for row in rows:
        key = url_key(row.get('url')) if isinstance(row, dict) else ''
        if not key:
            raise ValueError('Search returned an invalid URL')
    return rows


def source_metrics(case, response):
    """Known-URL coverage only: missing an annotation is not proof of irrelevance."""
    rows = successful_rows(response)
    references = {url_key(url) for url in case['reference_urls']}
    hosts = {urlsplit(url).hostname for url in references}
    keys = [url_key(row['url']) for row in rows]
    hits = [{'rank': rank, 'url': row['url']} for rank, (key, row) in
            enumerate(zip(keys, rows), 1) if key in references]
    trace = response.get('source_trace') or {}
    return {'reference_hit_at_1': bool(hits and hits[0]['rank'] == 1),
            'reference_hit_at_6': bool(hits),
            'reference_reciprocal_rank': 1 / hits[0]['rank'] if hits else 0.0,
            'reference_hits': hits, 'returned_count': len(rows),
            'raw_reference_url_seen': any(url_key(r.get('url')) in references
                                         for r in trace.get('candidates', [])),
            'source_trace_truncated': bool(trace.get('truncated')),
            'exact_reference_host_seen': any(urlsplit(key).hostname in hosts for key in keys)}


def plan(root, cases, seed, trials, fetch_top):
    operations = [{'trial': trial, 'task': case['id'], 'arm': arm}
                  for trial in range(trials) for index, case in enumerate(cases)
                  for arm in (ARMS if (trial + index) % 2 == 0 else ARMS[::-1])]
    return {'contract': CONTRACT, 'dataset': dataset_identity(), 'cases': cases,
            'selection': {'seed': seed, 'algorithm': 'SHA256(seed:row_id), equal count per topic'},
            'case_pack_sha256': digest(cases), 'operations': operations,
            'source_sha256': source_fingerprint(root),
            'tofu_source_sha256': python_tree_digest(root / 'packages/tofu-search/tofu_search'),
            'limits': {'results': RESULTS, 'operation_seconds': TOOL_SECONDS,
                       'fetch_top': fetch_top, 'trials': trials},
            'queries': 'Identical complete original question; no rewrite or reference hints',
            'source_trace': 'Bounded opt-in raw URLs, diagnostic only; never fetch targets',
            'arms': {'tofu': 'default free engines, no page fetch/filter, native ranking',
                     'ddgs': 'pinned DDGS auto, us-en, moderate safesearch'},
            'network': 'One explicit route, verified TLS, no direct-path racing',
            'common_fetch': 'Current Tofu fetcher, first returned URL only when requested',
            'infrastructure_retries': 0, 'model_calls': 0, 'leadership_claim': False,
            'limitations': [
                'Adapted source-annotation probe, not official SimpleQA or answer accuracy.',
                'Public English questions; not contamination-free or multilingual evidence.',
                'Source annotations may be stale, malformed or non-exhaustive.',
                'URL identity keeps scheme, path and query; no guessed alias/redirect repair.',
                'A reference URL or hostname match does not certify factual support.',
                'Raw engine traces have different upstream pool sizes; compare projected results only.',
                'Nonempty fetch output does not certify relevance or complete extraction.',
                'One literal search query does not represent adaptive agent browsing.']}


def summarize(registration, records, *, source_unchanged, corpus_unchanged, failure=None):
    expected = [(r['trial'], r['task'], r['arm']) for r in registration['operations']]
    keys = [(r['trial'], r['task'], r['arm']) for r in records]
    if len(set(keys)) != len(keys) or any(key not in expected for key in keys):
        raise ValueError('Duplicate or unplanned probe record')
    complete = set(keys) == set(expected)
    comparable = (complete and source_unchanged and corpus_unchanged and failure is None
                  and all(r['worker_settled'] for r in records))
    arms = {}
    for arm in ARMS:
        rows = [r for r in records if r['arm'] == arm]
        arms[arm] = {
            'planned': sum(r['arm'] == arm for r in registration['operations']),
            'attempts': len(rows),
            'reference_hits_at_1': sum(r['metrics']['reference_hit_at_1'] for r in rows),
            'reference_hits_at_6': sum(r['metrics']['reference_hit_at_6'] for r in rows),
            'reference_reciprocal_rank_sum': sum(r['metrics']['reference_reciprocal_rank'] for r in rows),
            'search_errors': dict(Counter(r['search_error'] for r in rows if r['search_error'])),
            'empty_searches': sum(r['metrics']['returned_count'] == 0 for r in rows),
            'search_seconds': [r['search_seconds'] for r in rows],
            'fetch_attempts': sum(r['fetch_attempts'] for r in rows),
            'nonempty_fetches': sum(r['nonempty_fetches'] for r in rows),
            'fetch_errors': dict(Counter(error for r in rows for error in r['fetch_errors'])),
            'topics': {topic: {'attempts': sum(r['topic'] == topic for r in rows),
                               'reference_hits_at_6': sum(r['topic'] == topic and
                                                         r['metrics']['reference_hit_at_6'] for r in rows)}
                       for topic in sorted({c['topic'] for c in registration['cases']})}}
    return {'contract': CONTRACT, 'complete': complete, 'comparable': comparable,
            'source_unchanged': source_unchanged, 'corpus_unchanged': corpus_unchanged,
            'failure': failure, 'arms': arms, 'leadership_claim': False,
            'scope': 'Recorded-source coverage and fetch availability; no answer or relevance grade'}


def live(root, args, registration, workers):
    output = Artifacts(args.output)
    output.write('plan.json', registration)
    records, failure = [], None
    by_id = {case['id']: case for case in registration['cases']}
    try:
        identities = {}
        for arm in ARMS:
            receipt = workers.call(arm, 'identity', {})
            output.write(f'identity-{arm}.json', receipt)
            if receipt.get('error') or receipt.get('worker_settled') is not True:
                raise ValueError('Worker identity or cleanup not verified')
            identities[arm] = receipt['identity']
        if identities['tofu'].get('tofu_source_sha256') != registration['tofu_source_sha256']:
            raise ValueError('Worker source differs from registered source')
        if (identities['ddgs'].get('ddgs') != DDGS_VERSION or not re.fullmatch(
                r'[0-9a-f]{64}', identities['ddgs'].get('ddgsSourceSha256', ''))):
            raise ValueError('DDGS version or source identity not verified')
        registration = {**registration, 'identities': identities,
                        'proxy_sha256': hashlib.sha256(workers.proxy.encode()).hexdigest()}
        output.write('manifest.json', registration)
        for operation in registration['operations']:
            case, arm = by_id[operation['task']], operation['arm']
            artifact = Artifacts(args.output / f'{operation["trial"]}-{case["id"]}-{arm}')
            response = workers.call(arm, 'search', {'query': case['query'], 'include_source_trace': True}, region='us-en',
                                    seed=registration['selection']['seed'])
            artifact.write('search.json', response)
            if response.get('worker_settled') is not True:
                raise RuntimeError('Search worker cleanup not proven')
            infrastructure_failed = response.get('error') in {'worker_exit', 'worker_output_budget'}
            metrics = source_metrics(case, response)
            fetched = []
            for index, row in enumerate(successful_rows(response)[:registration['limits']['fetch_top']]):
                receipt = workers.call(arm, 'fetch', {'url': row['url']},
                                       seed=registration['selection']['seed'])
                artifact.write(f'fetch-{index + 1}.json', receipt)
                if receipt.get('worker_settled') is not True:
                    raise RuntimeError('Fetch worker cleanup not proven')
                if receipt.get('error') in {'worker_exit', 'worker_output_budget'}:
                    infrastructure_failed = True
                fetched.append(receipt)
            record = {**operation, 'topic': case['topic'], 'metrics': metrics,
                      'search_error': response.get('error'), 'worker_settled': True,
                      'search_seconds': response['worker_seconds'],
                      'fetch_attempts': len(fetched),
                      'fetch_errors': [r['error'] for r in fetched if r.get('error')],
                      'nonempty_fetches': sum(bool(r.get('text')) and not r.get('error') for r in fetched)}
            artifact.write('record.json', record)
            records.append(record)
            print(canonical({'task': case['id'], 'arm': arm,
                             'reference_hit_at_6': metrics['reference_hit_at_6'],
                             'search_error': record['search_error'],
                             'nonempty_fetches': record['nonempty_fetches']}), flush=True)
            if infrastructure_failed:
                raise RuntimeError('Worker infrastructure failed; stopping later dispatch')
    except BaseException as exc:
        failure = type(exc).__name__
        raise
    finally:
        try:
            corpus_unchanged = hashlib.sha256(read_bytes(args.corpus)).hexdigest() == registration['dataset']['csv_sha256']
        except (OSError, ValueError):
            corpus_unchanged = False
        try:
            source_unchanged = source_fingerprint(root) == registration['source_sha256']
        except OSError:
            source_unchanged = False
        output.write('summary.json', summarize(
            registration, records, source_unchanged=source_unchanged,
            corpus_unchanged=corpus_unchanged, failure=failure))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--per-topic', type=int, choices=range(1, 6), default=2)
    parser.add_argument('--seed', type=int, default=20261021)
    parser.add_argument('--trials', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--fetch-top', type=int, choices=(0, 1), default=0)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--tofu-python')
    parser.add_argument('--ddgs-python')
    parser.add_argument('--proxy-env', default='TOFU_SEARCH_EVAL_PROXY')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    cases = select_cases(load_corpus(args.corpus), args.per_topic, args.seed)
    registration = plan(root, cases, args.seed, args.trials, args.fetch_top)
    if not args.live:
        print(canonical(registration))
        return 0
    if not all((args.output, args.tofu_python, args.ddgs_python)):
        parser.error('--live requires output and both worker interpreters')
    if not args.output.is_absolute() or args.output.exists():
        parser.error('--output must be an absolute new directory')
    if args.proxy_env not in os.environ:
        parser.error('Set the explicit proxy environment variable (empty means direct)')
    workers = Workers(root, args.tofu_python, args.ddgs_python, os.environ[args.proxy_env])
    live(root, args, registration, workers)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
