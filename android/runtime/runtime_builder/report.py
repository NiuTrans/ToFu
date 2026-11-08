"""Build reports: ELF machine and LOAD alignment, sizes per component.

Entry points: ``elf_report(entries, machine)`` (every ELF with its machine,
type and PT_LOAD alignments; flags alignments below 16 KB and foreign
machines), ``payload_component``, ``size_report`` (uncompressed bytes and
gzip/xz/zstd sizes of each component's tar stream). xz and zstd figures are
data only: the archives ship as gzip. Dependencies: stdlib, ``gzip``, ``xz``,
``zstd``.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import shutil
import subprocess
import tempfile
from pathlib import Path

from .elf import PAGE_16K, inspect_elf, is_elf
from .tree import Entry, write_tar_stream

# Compressor command lines measured for the size report. gzip -n -9 is what
# ships; the others show what switching formats would buy.
COMPRESSORS = {
    'gzip': ['gzip', '-n', '-9', '-c'],
    'xz': ['xz', '-9', '-T0', '-c'],
    'zstd': ['zstd', '-19', '-T0', '-q', '-c'],
}


def elf_report(entries: list[Entry], machine: str) -> dict:
    rows = []
    for entry in entries:
        if entry.type != 'file' or entry.size < 52:
            continue
        with open(entry.source, 'rb') as handle:
            if not is_elf(handle.read(4)):
                continue
        info = inspect_elf(entry.source)
        if info is None:
            continue
        rows.append({'path': entry.path, **info})
    foreign = [row['path'] for row in rows if row['machine'] != machine]
    below = [{'path': row['path'], 'min_load_align': hex(row['min_load_align'])}
             for row in rows if row['min_load_align'] is not None
             and row['min_load_align'] < PAGE_16K]
    histogram: dict[str, int] = {}
    for row in rows:
        key = hex(row['min_load_align']) if row['min_load_align'] is not None else 'no-load'
        histogram[key] = histogram.get(key, 0) + 1
    return {
        'machine': machine,
        'elf_files': len(rows),
        'foreign_machine': foreign,
        'min_load_align_histogram': dict(sorted(histogram.items())),
        'below_16k': below,
        'files': rows,
    }


def payload_component(path: str) -> str:
    if path.startswith('python/lib/python3.12/site-packages/'):
        component = 'site-packages'
    elif path.startswith('python/'):
        component = 'python'
    elif path.startswith('tofu/static/vite/'):
        component = 'frontend'
    elif path.startswith('tofu/'):
        component = 'tofu-source'
    elif path in ('bin/uv', 'bin/uvx'):
        component = 'uv'
    elif path in ('bin/tofu-db', 'bin/libtofu_db.so'):
        component = 'tofudb'
    else:
        component = 'launcher'
    return component


def _measure(stream_path: Path, command: list[str]) -> int:
    executable = shutil.which(command[0])
    if executable is None:
        return -1
    with open(stream_path, 'rb') as source:
        process = subprocess.Popen([executable, *command[1:]], stdin=source,
                                   stdout=subprocess.PIPE)
        total = 0
        for chunk in iter(lambda: process.stdout.read(1024 * 1024), b''):
            total += len(chunk)
        process.wait()
    return total if process.returncode == 0 else -1


def size_report(components: dict[str, list[Entry]], scratch: Path) -> dict:
    """Per component: entry count, content bytes, tar bytes and compressed sizes.

    Each component's tar stream is written once; every compressor then runs
    on every stream concurrently.
    """
    report = {}
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        streams = {}
        for index, (name, entries) in enumerate(sorted(components.items())):
            streams[name] = Path(directory) / f'{index}.tar'
            with open(streams[name], 'wb') as stream:
                tar_bytes = write_tar_stream(stream, entries, None)
            report[name] = {
                'entries': len(entries),
                'content_bytes': sum(entry.size for entry in entries if entry.type == 'file'),
                'tar_bytes': tar_bytes,
            }
        jobs = [(name, compressor) for name in streams for compressor in COMPRESSORS]
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            sizes = pool.map(lambda job: _measure(streams[job[0]], COMPRESSORS[job[1]]), jobs)
            for (name, compressor), size in zip(jobs, sizes):
                report[name][f'{compressor}_bytes'] = size
    return report
