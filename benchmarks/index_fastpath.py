#!/usr/bin/env python3
"""Compare identical project-index persistence/load with automatic placement.

Uses only disposable directories below explicit --source and --local bases.
Alternating pairs execute the production serializer/loader, with identical
synthetic columns verified after every read. No application database is opened.
Reports raw milliseconds, placement evidence and a source digest; this measures
index storage only, not command end-to-end latency or database transactions.
Run: python3 benchmarks/index_fastpath.py --source <network-dir> --local <local-dir>
"""
from __future__ import annotations

import argparse
import array
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(source: Path, local: Path, *, entries: int, pairs: int) -> dict:
    from lib.project_mod import tree_index as index
    from serverctl_pkg.local_placement import index_cache_placement

    previous = {key: os.environ.get(key) for key in ('TOFU_INDEX_FASTPATH', 'TOFU_LOCAL_DIR')}
    old_sessions, old_mem = index.SESSIONS_DIR, index._mem
    try:
        with ExitStack() as stack:
            slow = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.tofu-index-measure-', dir=source)))
            fast = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.tofu-index-measure-', dir=local)))
            index.SESSIONS_DIR = str(slow)
            os.environ['TOFU_LOCAL_DIR'] = str(fast)
            project = slow / 'project'
            project.mkdir()
            names = [f'src/module-{i:08d}.py' for i in range(entries)]
            sizes = array.array('q', (i + 17 for i in range(entries)))
            mtimes = array.array('q', (1700000000 + i for i in range(entries)))
            snapshot = index.TreeIndex(str(project), names, sizes, mtimes, time.time(), True)
            index._mem = {str(project): snapshot}
            os.environ['TOFU_INDEX_FASTPATH'] = 'auto'
            start = time.perf_counter()
            decision = index_cache_placement(slow)
            selection_ms = (time.perf_counter() - start) * 1000
            if not decision.selected:
                raise RuntimeError(f'no automatic fast path on these volumes: {decision.as_status()}')
            samples = {mode: {'publish_ms': [], 'load_ms': []} for mode in ('off', 'auto')}
            for pair in range(pairs + 1):
                for mode in (('off', 'auto') if pair % 2 == 0 else ('auto', 'off')):
                    os.environ['TOFU_INDEX_FASTPATH'] = mode
                    start = time.perf_counter()
                    if not index._persist(snapshot):
                        raise RuntimeError('index publication failed')
                    publish = (time.perf_counter() - start) * 1000
                    start = time.perf_counter()
                    restored = index._load_disk(str(project))
                    load = (time.perf_counter() - start) * 1000
                    if (restored is None or list(restored.paths) != names
                            or restored.sizes != sizes or restored.mtimes != mtimes):
                        raise AssertionError('roundtrip changed index work')
                    if pair:  # warm both modes equally; cold selection is separate
                        samples[mode]['publish_ms'].append(publish)
                        samples[mode]['load_ms'].append(load)
            blob_bytes = Path(index._disk_path(str(project))).stat().st_size
            medians = {mode: {key: statistics.median(values) for key, values in group.items()}
                       for mode, group in samples.items()}
            source_hash = hashlib.sha256()
            for relative in ('serverctl_pkg/local_placement.py', 'lib/project_mod/tree_index.py',
                             'lib/project_mod/tree_index_storage.py', 'runtime_guards.py'):
                source_hash.update(relative.encode() + b'\0' + (ROOT / relative).read_bytes())
            return {'entries': entries, 'blob_bytes': blob_bytes, 'pairs': pairs,
                    'verified_roundtrips': 2 * (pairs + 1), 'selection_ms': selection_ms,
                    'source_sha256': source_hash.hexdigest(), 'placement': decision.as_status(),
                    'samples': samples, 'median_ms': medians,
                    'speedup': {key: medians['off'][key] / medians['auto'][key]
                                for key in medians['off']}}
    finally:
        index.SESSIONS_DIR, index._mem = old_sessions, old_mem
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--local', type=Path, required=True)
    parser.add_argument('--entries', type=int, default=50000)
    parser.add_argument('--pairs', type=int, default=9)
    args = parser.parse_args()
    if args.entries < 1 or args.pairs < 2:
        parser.error('entries must be positive and pairs at least 2')
    print(json.dumps(run(args.source, args.local, entries=args.entries, pairs=args.pairs), indent=2))


if __name__ == '__main__':
    main()
