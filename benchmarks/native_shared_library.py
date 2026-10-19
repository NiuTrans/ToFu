"""Fresh-process combined-library admission through the actual NativeHost.

Compare old independent acquisition with shared member acquisition, using the
same installed source and archive. Both execute real Rust token and Flow work.
This component observation does not qualify application or system defaults.
Entry: collect --python WRAPPER --module-root INSTALLED --archive PINNED_ARCHIVE
--sha256 PIN --version VERSION --output NEW_DIRECTORY [--pairs 100].
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time


def worker(args):
    sys.path.insert(0, str(args.module_root))
    # Start before imports, admission or native calls. This is the operation's
    # cold clock, while the collector also retains launch-through-exit clocks.
    before_fds = set(os.listdir('/proc/self/fd'))
    started, cpu = time.perf_counter_ns(), time.process_time_ns()
    import asyncio
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_process import Budget
    from lib.agent_core.native_token_count import NativeTokenCounter
    from lib.identity import PrincipalContext
    from lib.orchestration.native_flow import NativeFlowLibrary
    from lib.tasks_pkg.native_host import NativeHost

    # The baseline factory preserves the previous host admission policy. All
    # other host ownership, calls, work and shutdown are common to both arms.
    if args.arm == 'independent':
        NativeTokenCounter.from_shared_archive = classmethod(lambda cls, *, image: cls(
            archive=image.archive, sha256=image.sha256,
            application_version=image.application_version, binary_bytes=image.budget.binary_bytes))
        NativeFlowLibrary.from_shared_archive = classmethod(lambda cls, *, image: cls.from_archive(
            archive=image.archive, sha256=image.sha256,
            application_version=image.application_version, maximum_bytes=image.budget.binary_bytes))
    image = NativeArchiveImage(archive=args.archive, sha256=args.sha256,
        application_version=args.version, budget=Budget(1, 64 << 20, 65536, 65536, 5))
    host = NativeHost(image, token_count=True)
    graph = None
    try:
        if args.first == 'token':
            counter, flow = host.token_counter(), host.flow_scheduler()
        else:
            flow, counter = host.flow_scheduler(), host.token_counter()
        tokens = {encoding: counter.count_batch(['hello world', '', '世界 café'], encoding=encoding)
                  for encoding in ('cl100k_base', 'o200k_base')}
        graph = flow.open_graph(principal=PrincipalContext.user(subject_id='library-bench', owner_user_id=7),
            task_id='library-bench', nodes={'start': {'id': 'start', 'type': 'role'}}, edges=[])
        work = dict(tokens=tokens, start=graph.find_start())
        member_identities = [os.fstat(fd).st_ino for fd in (counter._image.fileno(), flow._fd)]
        member_bytes = [os.fstat(fd).st_size for fd in (counter._image.fileno(), flow._fd)]
        handles_shared = counter._library._handle == flow._library._handle
        graph.close()
        graph = None
    finally:
        if graph is not None:
            graph.close()
        asyncio.run(host.close())
    elapsed, consumed = time.perf_counter_ns() - started, time.process_time_ns() - cpu
    joined = (host._closed and not image._libraries and counter._image.closed and flow._fd is None
              and set(os.listdir('/proc/self/fd')) == before_fds)
    sources = ('lib/agent_core/native_image.py', 'lib/agent_core/native_artifact.py',
               'lib/agent_core/native_token_count.py', 'lib/orchestration/native_flow.py',
               'lib/tasks_pkg/native_host.py')
    for name in sources:
        module = sys.modules[name[:-3].replace('/', '.')]
        if Path(module.__file__).resolve() != (args.module_root / name).resolve():
            raise ValueError('measurement imported a different source owner: ' + name)
    return dict(arm=args.arm, first=args.first, work=work, joined_cleanup=joined,
        member_identities=member_identities, member_bytes=member_bytes,
        sealed_backing_bytes=sum(dict(zip(member_identities, member_bytes)).values()), handles_shared=handles_shared,
        wall_ns=elapsed, cpu_ns=consumed, maxrss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        identity=[os.getpid(), Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]],
        archive_sha256=args.sha256, application_version=args.version,
        source_sha256={name: hashlib.sha256((args.module_root / name).read_bytes()).hexdigest() for name in sources},
        scope='combined native library cold component', system_admission=False)


def validate_pair(before, after):
    if (before.get('arm') != 'independent' or after.get('arm') != 'shared'
            or not before.get('identity') or before.get('identity') == after.get('identity')
            or not after.get('identity')):
        raise ValueError('distinct fresh-process arm identities required')
    for key in ('first', 'work', 'archive_sha256', 'application_version', 'source_sha256'):
        if not before.get(key) or before[key] != after.get(key):
            raise ValueError('unequal library work or source: ' + key)
    for row in (before, after):
        if row.get('joined_cleanup') is not True or row.get('system_admission') is not False:
            raise ValueError('incomplete joined cleanup or inappropriate system admission')
        if row.get('scope') != 'combined native library cold component':
            raise ValueError('unexpected measurement scope')
        if any(type(row.get(key)) is not int or row[key] <= 0
               for key in ('wall_ns', 'cpu_ns', 'maxrss_kib')):
            raise ValueError('missing positive clocks or memory observation')
        identities = row.get('member_identities', [])
        expected = row['arm'] == 'shared'
        if (len(identities) != 2 or (identities[0] == identities[1]) != expected
                or row.get('handles_shared') is not expected):
            raise ValueError('missing physical shared/independent library witness')


def collect(args):
    import statistics
    args.output.mkdir(parents=True, exist_ok=False)
    if hashlib.sha256(args.archive.read_bytes()).hexdigest() != args.sha256:
        raise ValueError('archive differs from explicit pin')
    rows = []
    for first in ('token', 'flow'):
        for pair in range(args.pairs):
            current = {}
            order = ('independent', 'shared') if pair % 2 == 0 else ('shared', 'independent')
            for arm in order:
                command = [args.python, str(Path(__file__).resolve()), 'worker',
                    '--module-root', str(args.module_root), '--archive', str(args.archive),
                    '--sha256', args.sha256, '--version', args.version, '--arm', arm, '--first', first]
                cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
                start = time.perf_counter_ns()
                completed = subprocess.run(command, cwd=args.output, capture_output=True, timeout=60)
                elapsed = time.perf_counter_ns() - start
                cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
                stem = f'{first}-{pair:03d}-{arm}'
                (args.output / (stem + '.stderr')).write_bytes(completed.stderr)
                (args.output / (stem + '.stdout')).write_bytes(completed.stdout)
                completed.check_returncode()
                row = json.loads(completed.stdout)
                row.update(launch_exit_wall_ns=elapsed, waited_cpu_ns=round(1e9 * (
                    cpu_after.ru_utime + cpu_after.ru_stime - cpu_before.ru_utime - cpu_before.ru_stime)))
                current[arm] = row
                (args.output / (stem + '.json')).write_text(json.dumps(row, sort_keys=True) + '\n')
            validate_pair(current['independent'], current['shared'])
            rows.extend(current.values())
        print(json.dumps(dict(completed_case=first, pairs=args.pairs)), flush=True)
    if len({tuple(row['identity']) for row in rows}) != len(rows):
        raise ValueError('worker identity reused')
    if any(row['source_sha256'] != rows[0]['source_sha256'] for row in rows):
        raise ValueError('source changed during collection')
    summaries = {}
    for first in ('token', 'flow'):
        def average(arm, key):
            return statistics.mean(row[key] for row in rows if row['arm'] == arm and row['first'] == first)
        keys = ('wall_ns', 'cpu_ns', 'maxrss_kib', 'launch_exit_wall_ns', 'waited_cpu_ns')
        summaries[first] = {key: dict(independent=average('independent', key), shared=average('shared', key),
                                      ratio=average('shared', key) / average('independent', key)) for key in keys}
    artifacts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(args.output.iterdir())}
    summary = dict(scope='combined native library cold component', system_admission=False,
        pairs_per_order=args.pairs, fresh_workers=len(rows), summaries=summaries, raw_sha256=artifacts,
        benchmark_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        archive_sha256=args.sha256, source_sha256=rows[0]['source_sha256'], joined_cleanup=True,
        memory_scope='fresh parent ru_maxrss high water; no whole-system RSS/PSS qualification')
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('worker', 'collect'))
    for name in ('module-root', 'archive'):
        parser.add_argument('--' + name, required=True, type=lambda value: Path(value).resolve())
    for name in ('sha256', 'version'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--arm', choices=('independent', 'shared'))
    parser.add_argument('--first', choices=('token', 'flow'))
    parser.add_argument('--python')
    parser.add_argument('--output', type=lambda value: Path(value).resolve())
    parser.add_argument('--pairs', type=int, default=100)
    args = parser.parse_args()
    if args.mode == 'worker':
        if args.arm is None or args.first is None:
            parser.error('worker requires arm and first')
        print(json.dumps(worker(args), sort_keys=True))
    else:
        if not args.python or args.output is None or args.pairs <= 0:
            parser.error('collect requires python, new output and positive pairs')
        collect(args)


if __name__ == '__main__':
    main()
