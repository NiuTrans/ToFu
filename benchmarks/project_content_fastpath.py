#!/usr/bin/env python3
"""Paired complete-search measurement including cache freshness/rewriting cost.

Disposable fixtures only. --source-parent selects the real source filesystem;
--cache-parent selects local storage. Setup/copy and steady query clocks are
separate. Alternating pairs must have equal complete match records, counts and
remainder evidence before any latency ratio is reported. This is a component
measurement, not an application/task speedup claim.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def equivalent(result, expected):
    if result[1] or result[2] or result[3]:
        raise ValueError('incomplete search cannot certify performance')
    if sorted(result[0].splitlines()) != expected:
        raise ValueError('search changed complete match records')


def measure(source_parent, cache_parent, *, files=800, pairs=9):
    from lib.project_mod import content_shadow as shadow
    from lib.project_mod.read_tools import _run_index_chunks

    with tempfile.TemporaryDirectory(prefix='tofu-content-measure-', dir=source_parent) as source, \
            tempfile.TemporaryDirectory(prefix='tofu-content-cache-', dir=cache_parent) as cache:
        root, home = Path(source), Path(cache)
        paths, total = [], 0
        for index in range(files):
            path = root / f'file-{index:05}.txt'
            content = (f'miss {index}\n' * 32768 + f'needle {index}\n').encode()
            with path.open('wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            total += len(content)
            paths.append(str(path))
        # Leave the timestamp-resolution window of the synthetic writes. This
        # setup wait is not part of either query or migration measurement.
        time.sleep(2.1)
        previous_override = os.environ.get('TOFU_CONTENT_SHADOW_DIR')
        os.environ['TOFU_CONTENT_SHADOW_DIR'] = cache
        try:
            started = time.perf_counter()
            deadline = time.monotonic() + 60
            pending = paths
            while True:
                # Production supplies only cache misses, never an already-hot
                # prefix that could consume the entire optional fill interval.
                shadow._sync_worker(source, pending, home)
                entries = shadow._manifest(shadow._root_dir(source, home)).get('entries', {})
                if len(entries) == len(paths):
                    break
                pending = [path for path in paths if os.path.basename(path) not in entries]
                if time.monotonic() >= deadline:
                    raise RuntimeError('fixture cache did not converge')
            copy_seconds = time.perf_counter() - started
            expected = sorted((os.fsencode(path) + b'\0' + f'32769:needle {i}'.encode())
                              for i, path in enumerate(paths))
            samples = {'source': [], 'cache': []}
            hit_counts, live_retries = [], 0
            for pair in range(pairs + 1):
                for mode in (('source', 'cache') if pair % 2 == 0 else ('cache', 'source')):
                    started = time.perf_counter()
                    deadline = time.monotonic() + 60
                    if mode == 'source':
                        result = _run_index_chunks(source, 'needle', 0, files + 1, False,
                                                   paths, deadline, total)
                    else:
                        with shadow.candidate_view(source, paths) as view:
                            raw, timed_out, unavailable, withheld = _run_index_chunks(
                                source, 'needle', 0, files + 1, False, view.paths, deadline, total)
                            if not view.current():
                                result = _run_index_chunks(source, 'needle', 0, files + 1, False,
                                                           paths, deadline, total)
                                live_retries += 1
                            else:
                                result = (view.rewrite(raw), timed_out, unavailable,
                                          view.rewrite_withheld(withheld))
                            hit_counts.append(len(view.replacements))
                    elapsed = time.perf_counter() - started
                    equivalent(result, expected)
                    if pair:
                        samples[mode].append(round(elapsed * 1000, 3))
            medians = {mode: statistics.median(values) for mode, values in samples.items()}
            return {'files': files, 'sourceBytes': total, 'copySeconds': round(copy_seconds, 3),
                    'samplesMs': samples, 'medianMs': medians,
                    'speedup': round(medians['source'] / medians['cache'], 3),
                    'completeMatches': len(expected), 'cacheHits': hit_counts, 'liveRetries': live_retries}
        finally:
            if previous_override is None:
                os.environ.pop('TOFU_CONTENT_SHADOW_DIR', None)
            else:
                os.environ['TOFU_CONTENT_SHADOW_DIR'] = previous_override


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-parent', required=True)
    parser.add_argument('--cache-parent', default=None)
    parser.add_argument('--files', type=int, default=800)
    parser.add_argument('--pairs', type=int, default=9)
    args = parser.parse_args()
    if args.files < 1 or args.pairs < 1:
        parser.error('files and pairs must be positive')
    print(json.dumps(measure(args.source_parent, args.cache_parent,
                             files=args.files, pairs=args.pairs), indent=2))


if __name__ == '__main__':
    main()
