#!/usr/bin/env python3
"""Compare storage media for native-authority placement with scratch-only I/O.

Measures, inside a private ``<scratch-root>/storage-medium-probe-<pid>``
directory (mode 0700, removed on exit), the operations the native engine pays
for: durable WAL-style appends (write+fdatasync), the write() alone (the
Normal-durability in-lock cost), loose-block publication (temporary file,
fsync, rename, directory fsync), metadata (create, stat hit and miss,
open+read+close warm and after a page-cache drop, rename, readdir, unlink),
sequential throughput, cold random 4 KiB reads (buffered after a cache drop
and O_DIRECT where supported), random durable writes, and 8-thread durable
appends and publications. The shared placement policy classifies the volume.

It never opens a path outside its scratch directory, so it is safe beside a
live authority; it measures the medium, not the engine. Compare with the
engine's own relocation probe (``tofu-db serve --fastpath-admission
required``) and live ``system.metrics`` before deciding placement
(docs/STORAGE_OPERATIONS.md, "Native authority placement").

Run: python3 benchmarks/storage_medium_probe.py SCRATCH_ROOT [--seq-mib N] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import mmap
import os
from pathlib import Path
import random
import shutil
import statistics
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BLOCK = 4096


def summary(samples):
    ordered = sorted(samples)
    count = len(ordered)
    if not count:
        return {'n': 0}

    def quantile(fraction):
        return round(ordered[min(count - 1, int(round(fraction * (count - 1))))] * 1e6, 1)

    return {'n': count, 'p50_us': quantile(0.50), 'p90_us': quantile(0.90),
            'p99_us': quantile(0.99), 'max_us': round(ordered[-1] * 1e6, 1),
            'mean_us': round(statistics.fmean(ordered) * 1e6, 1)}


def timed(operation, items):
    samples = []
    for item in items:
        started = time.perf_counter()
        operation(item)
        samples.append(time.perf_counter() - started)
    return samples


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _drop_cache(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)  # dirty pages survive DONTNEED
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def appends(path, size, count, *, sync=True, preallocate=False):
    buffer = os.urandom(size)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if preallocate:
            for index in range(count):
                os.pwrite(fd, buffer, index * size)
            os.fsync(fd)
        samples = []
        for index in range(count):
            started = time.perf_counter()
            os.pwrite(fd, buffer, index * size)
            if sync:
                os.fdatasync(fd)
            samples.append(time.perf_counter() - started)
        os.fsync(fd)
        return samples
    finally:
        os.close(fd)


def publications(directory, count):
    buffer = os.urandom(BLOCK)
    samples = []
    for index in range(count):
        temporary = os.path.join(directory, f'.publish-{index}.tmp')
        started = time.perf_counter()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, buffer)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(temporary, os.path.join(directory, f'publish-{index}.blk'))
        _fsync_directory(directory)
        samples.append(time.perf_counter() - started)
    return samples


def metadata(root, count):
    directory = os.path.join(root, 'metadata')
    os.mkdir(directory, 0o700)
    buffer = os.urandom(BLOCK)
    names = [os.path.join(directory, f'f{index:06d}.blk') for index in range(count)]

    def create(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, buffer)
        os.close(fd)

    def stat_missing(path):
        try:
            os.stat(path)
        except FileNotFoundError:
            pass

    def open_read_close(path):
        fd = os.open(path, os.O_RDONLY)
        os.read(fd, BLOCK)
        os.close(fd)

    result = {'create_4k': summary(timed(create, names)),
              'stat_hit_first': summary(timed(os.stat, names)),
              'stat_hit_repeat': summary(timed(os.stat, names))}
    missing = [os.path.join(directory, f'.missing-{os.urandom(8).hex()}') for _ in range(count)]
    result['stat_miss'] = summary(timed(stat_missing, missing))
    result['open_read_close_warm'] = summary(timed(open_read_close, names))
    for path in names:
        _drop_cache(path)
    result['open_read_close_after_drop'] = summary(timed(open_read_close, names))
    result['readdir'] = summary(timed(lambda _: os.listdir(directory), range(10)))
    renamed = [path + '.renamed' for path in names]
    result['rename'] = summary(timed(lambda pair: os.rename(*pair), zip(names, renamed)))
    result['unlink'] = summary(timed(os.unlink, renamed))
    os.rmdir(directory)
    return result


def data_path(root, seq_mib, random_count):
    path = os.path.join(root, 'sequential.bin')
    chunk = 1 << 20
    buffer = os.urandom(chunk)
    size = seq_mib * chunk
    result = {}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    started = time.perf_counter()
    for index in range(seq_mib):
        os.pwrite(fd, buffer, index * chunk)
    os.fsync(fd)
    result['seq_write_fsync_mb_s'] = round(size / (time.perf_counter() - started) / 1e6, 1)
    os.close(fd)
    _drop_cache(path)
    fd = os.open(path, os.O_RDONLY)
    started, offset = time.perf_counter(), 0
    while data := os.pread(fd, chunk, offset):
        offset += len(data)
    result['seq_read_after_drop_mb_s'] = round(offset / (time.perf_counter() - started) / 1e6, 1)
    os.close(fd)
    offsets = [random.randrange(size // BLOCK) * BLOCK for _ in range(random_count)]
    _drop_cache(path)
    fd = os.open(path, os.O_RDONLY)
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_RANDOM)
    result['random_read_4k_after_drop'] = summary(timed(lambda at: os.pread(fd, BLOCK, at), offsets))
    os.close(fd)
    try:
        direct = os.open(path, os.O_RDONLY | os.O_DIRECT)
    except OSError as exc:
        result['o_direct'] = f'unsupported: {exc.strerror}'
    else:
        aligned = mmap.mmap(-1, BLOCK)
        view = memoryview(aligned)
        try:
            again = [random.randrange(size // BLOCK) * BLOCK for _ in range(random_count)]
            result['random_read_4k_o_direct'] = summary(
                timed(lambda at: os.preadv(direct, [view], at), again))
        except OSError as exc:
            result['o_direct'] = f'read failed: {exc.strerror}'
        finally:
            view.release()
            aligned.close()
            os.close(direct)
    fd = os.open(path, os.O_WRONLY)
    block = os.urandom(BLOCK)

    def durable_write(at):
        os.pwrite(fd, block, at)
        os.fdatasync(fd)

    result['random_write_4k_fdatasync'] = summary(timed(durable_write, offsets[:max(1, random_count // 4)]))
    os.close(fd)
    os.unlink(path)
    return result


def concurrent(root, kind, threads, count):
    results, errors = [None] * threads, []

    def worker(index):
        try:
            directory = os.path.join(root, f'{kind}-{index}')
            os.mkdir(directory, 0o700)
            results[index] = (appends(os.path.join(directory, 'wal.bin'), BLOCK, count)
                              if kind == 'append' else publications(directory, count))
        except OSError as exc:
            errors.append(repr(exc))

    started = time.perf_counter()
    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    elapsed = time.perf_counter() - started
    merged = [sample for samples in results if samples for sample in samples]
    report = summary(merged)
    report['aggregate_ops_per_s'] = round(len(merged) / elapsed, 1)
    if errors:
        report['errors'] = errors
    return report


def volume_description(path):
    from serverctl_pkg.local_placement import describe_volume
    volume = describe_volume(path)
    return {'filesystem': volume.filesystem_type, 'mount_point': volume.mount_point,
            'storage_class': volume.storage_class, 'persistence': volume.persistence}


def run(scratch_root, *, seq_mib=1024, appends_count=400, publish_count=200,
        metadata_count=1000, random_count=1000, concurrent_count=100):
    base = Path(scratch_root).resolve()
    if not base.is_dir():
        raise SystemExit(f'scratch root does not exist: {base}')
    root = base / f'storage-medium-probe-{os.getpid()}'
    previous = os.umask(0o077)
    try:
        root.mkdir(mode=0o700)
    finally:
        os.umask(previous)
    report = {'scratch_root': str(base), 'started': time.strftime('%Y-%m-%d %H:%M:%S'),
              'volume': volume_description(base),
              'loadavg_before': os.getloadavg()}
    started = time.perf_counter()
    try:
        report['wal_append_4k_fdatasync'] = summary(appends(root / 'a4k', BLOCK, appends_count))
        report['wal_overwrite_4k_fdatasync'] = summary(
            appends(root / 'o4k', BLOCK, appends_count, preallocate=True))
        report['wal_append_64k_fdatasync'] = summary(appends(root / 'a64k', 65536, max(1, appends_count // 2)))
        report['wal_append_4k_write_only'] = summary(
            appends(root / 'w4k', BLOCK, appends_count * 5, sync=False))
        (root / 'publish').mkdir(mode=0o700)
        report['publish_4k_block'] = summary(publications(root / 'publish', publish_count))
        report['metadata'] = metadata(root, metadata_count)
        report['data'] = data_path(root, seq_mib, random_count)
        report['concurrent8_append_4k_fdatasync'] = concurrent(root, 'append', 8, concurrent_count)
        report['concurrent8_publish_4k_block'] = concurrent(root, 'publish', 8, max(1, concurrent_count // 2))
    finally:
        report['elapsed_s'] = round(time.perf_counter() - started, 1)
        report['loadavg_after'] = os.getloadavg()
        shutil.rmtree(root, ignore_errors=True)
        report['scratch_removed'] = not root.exists()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('scratch_root', help='existing directory on the volume under study')
    parser.add_argument('--seq-mib', type=int, default=1024)
    parser.add_argument('--out', help='also write the JSON report here')
    args = parser.parse_args(argv)
    report = run(args.scratch_root, seq_mib=args.seq_mib)
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + '\n')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
