"""Offline request preparation: authority copy, routing, real slots and disposal.

Run identical fixtures against --source-root in alternating fresh processes.
The authority and audit sink are in-memory; no storage, network or inference is
measured. Timing and tracemalloc run separately. Ordered route receipts and empty
registries gate observations; these numbers are not end-to-end chat latency.
"""

from __future__ import annotations

import argparse
import copy
import gc
import hashlib
import json
import logging
from pathlib import Path
import statistics
import subprocess
import sys
import time
import tracemalloc
from types import SimpleNamespace
from unittest.mock import patch


CASES = {'small': (2, 1, None), 'full': (32, 2, None),
         'shortlist': (128, 2, 8), 'primary_only': (128, 2, 1)}


def synthetic_catalog(model_count, credential_count):
    from lib.model_routing import empty_document
    document = empty_document()
    refs = [dict(creator_id='benchmark', model_id=f'model-{i:04d}') for i in range(model_count)]
    document['creators'] = [dict(creator_id='benchmark', name='Synthetic benchmark')]
    prices = dict(input=1, output=2, currency='USD', unit='per_million_tokens')
    document['models'] = [dict(**ref, display_name=ref['model_id'], capabilities=['text'],
        context_window=200_000, quality_rank=model_count-i, list_pricing=prices,
        registration_metadata=dict(release_date='2026-01-01', checked_at='2026-09-18',
                                   source_urls=['https://example.invalid/benchmark']),
        inference_profile={'reasoning': {'support': 'unsupported'}}) for i, ref in enumerate(refs)]
    for provider in ('a', 'b'):
        document['providers'].append(dict(provider_id=provider, name=provider, scope='owner'))
        document['provider_accesses'].append(dict(provider_access_id=provider,
            provider_id=provider, enabled=True, quota_policy={}))
        document['connections'].append(dict(connection_id=provider, provider_access_id=provider,
            base_url='http://127.0.0.1:1/v1', protocol='openai', enabled=True,
            priority=0, extra_headers={}))
        for i in range(credential_count):
            document['credentials'].append(dict(credential_id=f'{provider}-{i}',
                provider_access_id=provider, kind='api_key', secret_reference=f'{provider}-{i}',
                key_hint='', enabled=True, quota_policy={},
                authorization=dict(connection_ids=[provider], models=refs)))
        for i, ref in enumerate(refs):
            identity = f'{provider}-{i:04d}'
            document['offerings'].append(dict(offering_id=identity, provider_access_id=provider,
                identity_state='confirmed', model=ref, enabled=True, capabilities=['text'],
                context_window=200_000, priority=0, actual_pricing=prices))
            document['deployments'].append(dict(deployment_id=identity, offering_id=identity,
                connection_id=provider, wire_model_id=ref['model_id'], enabled=True,
                identity_confidence='high', probe_status='passed', priority=0, max_output_tokens=8192))
    return document


def measure_case(model_count, credential_count, limit, samples):
    from lib.llm_dispatch import ephemeral, factory
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.model_routing import ModelRef, NativeModelSelection, OwnerBoundary
    from lib.model_routing import dispatch_adapter as adapter

    document = synthetic_catalog(model_count, credential_count)
    boundary = OwnerBoundary.create(23)
    selection = NativeModelSelection(ModelRef('benchmark', 'model-0000'), None, 'a')
    dispatcher = LLMDispatcher()
    counts = dict(read=0, secret=0, minted=0, disposed=0)
    def get(owner):
        assert owner == boundary
        counts['read'] += 1
        return SimpleNamespace(document=copy.deepcopy(document), revision=7)
    def secret(owner, reference):
        assert owner == boundary
        counts['secret'] += 1
        return 'synthetic-' + reference
    def audit(event, **details):
        # Older source roots audit each slot; current ones audit each route
        # group with its slot and withdrawal counts.
        if event == 'ephemeral_slot_mint':
            counts['minted'] += details.get('slots', 1)
        elif event == 'ephemeral_slot_dispose':
            counts['disposed'] += int(details['removed'])
    repository = SimpleNamespace(get=get, resolve_secret=secret)
    expected_count = model_count * 2 * credential_count
    if limit is not None:
        expected_count = min(limit, expected_count)
    receipt_hash = None
    def run():
        nonlocal receipt_hash
        counts.update(read=0, secret=0, minted=0, disposed=0)
        started = time.perf_counter_ns()
        group = adapter.mint_routed_slot_group(repository, boundary, selection, max_candidates=limit)
        try:
            rows = [(c.model['model_id'], c.provider_id, c.deployment['deployment_id'],
                     c.credential['credential_id'], h.slot.route_rank, h.slot.model,
                     h.slot.context_window, h.slot.max_output_tokens, h.slot.cost_per_1k_tokens)
                    for c, h in zip(group.candidates, group.handles)]
        finally:
            adapter.dispose_routed_slot_group(group)
        elapsed = time.perf_counter_ns() - started
        assert len(rows) == expected_count
        assert not dispatcher.slots and ephemeral.count_ephemeral_slots() == 0
        assert all(not h.slot.api_key and not h.slot.extra_headers for h in group.handles)
        assert counts['read'] == 1
        assert counts['minted'] == counts['disposed'] == expected_count
        digest = hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()
        assert receipt_hash in (None, digest)
        receipt_hash = digest
        return elapsed
    with patch.object(factory, 'get_dispatcher', lambda: dispatcher), \
            patch.object(ephemeral, '_handles', {}), patch.object(ephemeral, 'audit_log', audit), \
            patch.object(ephemeral, '_preflight_enabled', lambda: False):
        run()  # One full untimed request warms common imports and metadata.
        times = [run() for _ in range(samples)]
        peaks = []
        for _ in range(3):
            gc.collect()
            tracemalloc.start()
            try:
                run()
                peaks.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
    return dict(models=model_count, credentials_per_provider=credential_count,
        shortlist=limit, candidate_count=expected_count, receipt_sha256=receipt_hash,
        elapsed_ns=times, median_ns=statistics.median(times),
        p95_ns=sorted(times)[min(len(times)-1, int(len(times)*.95))],
        peak_bytes=peaks, median_peak_bytes=statistics.median(peaks), counts=counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=15)
    parser.add_argument('--case', choices=list(CASES), action='append')
    args = parser.parse_args()
    if args.samples < 3:
        parser.error('--samples must be at least 3')
    source = args.source_root.resolve()
    sys.path.insert(0, str(source))
    logging.disable(logging.CRITICAL)
    paths = sorted((source/'lib/model_routing').glob('*.py'))
    paths += sorted((source/'lib/llm_dispatch').glob('*.py'))
    source_hashes = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    result = dict(scope=__doc__, source=str(source),
        revision=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip(),
        source_hashes=source_hashes,
        cases={name: measure_case(*CASES[name], args.samples) for name in args.case or CASES})
    assert source_hashes == {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    args.output.write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':
    main()
