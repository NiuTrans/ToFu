"""Opt-in Linux diagnostic: observe selected file reads during an explicit command.

run_observed_process reuses run_command_process for trusted, caller-admitted
argv/environment, bounded output, Stop and timeout. It preserves command_result
separately from read facts. This is an internal adapter, not a model-facing tool
or authorization boundary; callers own project/write/network admission. It records selected read and
readable-map syscalls, not semantic data use, complete dependencies or tamper
resistance against a same-UID target. Dependencies: managed command process port,
project artifact file guard and standard-library trace transport.
"""
from __future__ import annotations
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import shlex
import signal
import tempfile
import threading
import time

PINNED_INPUT_ARG_PREFIX = '@tofu-input:'
FILE_READ_OBSERVATION_KEY = 'file_read_observations'

READ = re.compile(r'\b(read|pread64)\(\d+<(.*)>,.*\)\s+=\s+(\d+)')
MAP = re.compile(r'\bmmap\(.*PROT_READ.*\d+<(.*)>,.*\)\s+=\s+(0x[0-9a-f]+)')
ESCAPE = re.compile(rb'\\([0-7]{3}|[abfnrtv\\"])')
ESCAPES = {ord('a'):7, ord('b'):8, ord('f'):12, ord('n'):10, ord('r'):13,
           ord('t'):9, ord('v'):11, ord('\\'):92, ord('"'):34}


def decoded_path(value):
    def decode(match):
        token = match.group(1)
        return bytes([int(token, 8) if len(token) == 3 else ESCAPES[token[0]]])
    return os.fsdecode(ESCAPE.sub(decode, os.fsencode(value)))


def command_evidence_text(command_result, observation):
    """Route read facts to stderr, preserving stdout and producer exit status.

    The marker routes archival, never authenticity or scientific authority.
    """
    footer = command_result.rfind('\n[exit code: ')
    if footer < 0:
        raise ValueError('Read observation requires a completed producer command envelope')
    trailer = re.search(r'\n\[Command (?:timed out|aborted by user|interrupted by [^\n]+)\]$', command_result[:footer])
    insertion = trailer.start() if trailer else footer
    body = {key:value for key,value in observation.items()
            if key not in ('command_result','evidence_result')}
    addition = '\n[stderr]\n' + json.dumps({FILE_READ_OBSERVATION_KEY:body},
        ensure_ascii=False, sort_keys=True, separators=(',',':'))
    return command_result[:insertion] + addition + command_result[insertion:]


def stop_trace(process):
    # TracerPid keeps detached tracees attributable to this live tracer, even
    # after setsid/reparenting. Pause the tracer before enumerating its targets.
    if process.poll() is not None: return
    try: os.kill(process.pid, signal.SIGSTOP)
    except ProcessLookupError: return
    try:
        groups = set()
        for entry in os.scandir('/proc'):
            if not entry.name.isdigit(): continue
            try:
                for thread in os.scandir(entry.path + '/task'):
                    try:
                        fields = dict(line.split(':',1) for line in Path(thread.path,'status').read_text().splitlines() if ':' in line)
                        if int(fields.get('TracerPid','0')) == process.pid:
                            groups.add(int(fields['Tgid']))
                    except (OSError,ValueError,KeyError): continue
            except OSError: continue
        for pid in groups:
            try: os.kill(pid,signal.SIGKILL)
            except ProcessLookupError: pass
    finally:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=3)


def run_observed_process(project, paths, command, *, timeout_seconds, child_env, task=None,
                         tracer=None, on_chunk=None, on_spawn=None, runtime_context=None, deadline_epoch=None, pin_inputs=False, expected_input_sha256=None, admission_check=None, working_dir=None, display_command=None):
    """Execute one explicitly supplied probe, with bounded memory and cleanup."""
    if (timeout_seconds is not None and (type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds) or timeout_seconds < 0)) or not command or not paths:
        raise ValueError('Nonnegative timeout, selected paths and command are required')
    if not isinstance(command, (tuple,list)) or any(not isinstance(arg,str) or '\0' in arg for arg in command):
        raise ValueError('Command must be structured string arguments')
    if admission_check is not None and not callable(admission_check):
        raise ValueError('Input admission check must be callable')
    if type(pin_inputs) is not bool:
        raise ValueError('pin_inputs must be boolean')
    if not isinstance(paths, (tuple,list)) or not paths or any(not isinstance(path,str) for path in paths):
        raise ValueError('Selected paths must be a nonempty structured string list')
    # One caller budget covers input preparation and the managed process.
    # None remains unbounded; wall-clock deadlines remain independently enforced.
    budget_deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    selected_paths = tuple(dict.fromkeys(paths))
    expected = None
    if expected_input_sha256 is not None:
        if not isinstance(expected_input_sha256, dict):
            raise ValueError('Expected input SHA-256 must map all selected paths to digests')
        expected = dict(expected_input_sha256)
        if (set(expected) != set(selected_paths) or any(not isinstance(value,str)
                or re.fullmatch('[0-9a-f]{64}',value) is None for value in expected.values())):
            raise ValueError('Expected input SHA-256 must exactly cover selected paths with canonical digests')
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Linux process tracing is unavailable; command was not executed')
    tracer = tracer or shutil.which('strace')
    if not tracer or not Path(tracer).is_file():
        raise RuntimeError('strace is unavailable; command was not executed')
    from lib.evolution.artifact_evidence import _open_project_file, _stable_file
    def check_preflight_stop():
        event = (task or {}).get('abort_event')
        if ((task or {}).get('aborted') or (event is not None and event.is_set())
                or (runtime_context is not None and runtime_context.cancellation_requested)):
            raise RuntimeError('Read observation preflight cancelled; command was not allocated')
        if budget_deadline is not None and time.monotonic() >= budget_deadline:
            raise RuntimeError('Read observation timeout exhausted during preparation; command was not allocated')
        if deadline_epoch is not None and time.time() >= deadline_epoch:
            raise RuntimeError('Read observation deadline exhausted; command was not allocated')
    check_preflight_stop()
    project = str(Path(project).resolve(strict=True))
    with ExitStack() as resources:
        selected = {}
        for path in selected_paths:
            handle, before = resources.enter_context(_open_project_file(project, path))
            digest = hashlib.sha256();size = 0
            while True:
                check_preflight_stop()
                block = os.read(handle, 1024*1024)
                if not block: break
                digest.update(block);size += len(block)
            if not _stable_file(before, os.fstat(handle), size):
                raise RuntimeError('Selected input changed during initial hash')
            if expected is not None and digest.hexdigest() != expected[path]:
                raise RuntimeError(f'Frozen input digest mismatch: {path}; command was not allocated')
            selected[str(Path(project)/path)] = {'path':path,'initial_sha256':digest.hexdigest(),
                'bytes':size,'read_calls':0,'read_bytes':0,'readable_maps':0,
                'handle':handle,'before':before,
                **({'expected_sha256':expected[path],'matches_expected':True} if expected is not None else {})}
        effective_command = list(command)
        supplied = set()
        if pin_inputs:
            tokens = {PINNED_INPUT_ARG_PREFIX + row['path']: row for row in selected.values()}
            for position, argument in enumerate(effective_command):
                if argument in tokens:
                    row = tokens[argument]
                    os.lseek(row['handle'], 0, os.SEEK_SET)
                    effective_command[position] = '/proc/self/fd/' + str(row['handle'])
                    supplied.add(row['handle'])
                elif PINNED_INPUT_ARG_PREFIX in argument:
                    raise ValueError('Input tokens must name selected paths and occupy whole argv positions')
            if not supplied:
                raise ValueError('pin_inputs requires at least one selected input token')
        for row in selected.values(): row['descriptor_supplied'] = row['handle'] in supplied
        with tempfile.TemporaryDirectory(prefix='tofu-experiment-read-trace-') as directory:
            fifo = Path(directory)/'events';os.mkfifo(fifo, 0o600)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            try: writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except BaseException:
                os.close(reader);raise
            stopped = threading.Event();errors = [];counts = {'oversized_lines':0,'unparsed_events':0}
            def consume():
                pending = b'';discarding = False
                try:
                    while not stopped.is_set():
                        try: block = os.read(reader, 65536)
                        except BlockingIOError:
                            stopped.wait(.01);continue
                        if not block: break
                        for piece in block.splitlines(keepends=True):
                            if len(pending) + len(piece) > 65536:
                                pending = b'';discarding = True
                            if not discarding: pending += piece
                            if piece.endswith(b'\n'):
                                if discarding: counts['oversized_lines'] += 1
                                else:
                                    line = os.fsdecode(pending)
                                    if '<unfinished ...>' in line or 'resumed>' in line: counts['unparsed_events'] += 1
                                    read = READ.search(line);mapping = MAP.search(line)
                                    match = read or mapping
                                    if match:
                                        target = decoded_path(match.group(2) if read else match.group(1))
                                        row = selected.get(target)
                                        if row is not None:
                                            if read and int(match.group(3)) > 0:
                                                row['read_calls'] += 1;row['read_bytes'] += int(match.group(3))
                                            elif mapping: row['readable_maps'] += 1
                                pending = b'';discarding = False
                    if pending or discarding: counts['oversized_lines'] += 1
                except Exception as exc: errors.append(type(exc).__name__)
            worker = threading.Thread(target=consume, name='experiment-read-trace')
            processes = [];started = time.monotonic()
            result_text = ''
            def bind_process(process):
                process._file_read_cleanup = stop_trace
                processes.append(process)
            try:
                worker.start()
                args = [tracer,'-f','-q','-yy','-s','0','-e','trace=read,pread64,mmap','-o',str(fifo)]
                for target in selected: args += ['-P',target]
                check_preflight_stop()
                if admission_check is not None:
                    if admission_check() is not None:
                        raise ValueError('Input admission check must synchronously return None or refuse')
                check_preflight_stop()
                for row in selected.values():
                    if not _stable_file(row['before'], os.fstat(row['handle']), row['bytes']):
                        raise RuntimeError('Selected input changed before launch; command was not allocated')
                check_preflight_stop()
                remaining_timeout = None if budget_deadline is None else max(0.0, budget_deadline - time.monotonic())
                from .run_command import run_command_process
                result_text = run_command_process(args + ['--'] + effective_command,
                    command=display_command if display_command is not None else shlex.join(command), timeout=remaining_timeout, base=working_dir if working_dir is not None else project,
                    task=task, on_chunk=on_chunk, on_spawn=on_spawn,
                    runtime_context=runtime_context, child_env=child_env,
                    process_guard=bind_process, deadline_epoch=deadline_epoch, extra_pass_fds=tuple(sorted(supplied)))
            finally:
                try:
                    for process in processes:
                        if process.poll() is None: stop_trace(process)
                finally:
                    if writer is not None: os.close(writer)
                    if worker.ident is not None:
                        worker.join(timeout=2)
                        if worker.is_alive():
                            errors.append('trace_drain_timeout');stopped.set()
                            # Nonblocking reads and bounded parsing cooperate with Stop.
                            # Await quiescence before closing FDs or publishing mutable rows.
                            worker.join()
                    os.close(reader)
            from lib.background_command_projection import command_result_status
            status = command_result_status(result_text) or {}
            returncode = status.get('exitCode')
            timed_out = bool(status.get('timedOut'))
            reports = []
            for row in selected.values():
                descriptor_stable = _stable_file(row['before'], os.fstat(row['handle']), row['bytes'])
                path_matches = False
                try:
                    with _open_project_file(project, row['path']) as (current, _):
                        path_matches = os.fstat(current).st_ino == row['before'].st_ino and os.fstat(current).st_dev == row['before'].st_dev
                except (OSError, ValueError, RuntimeError): path_matches = False
                report = {key:value for key,value in row.items() if key not in ('handle','before')}
                report['descriptor_metadata_stable'] = bool(descriptor_stable)
                report['final_path_matches_initial_descriptor'] = bool(path_matches)
                report['stable_through_probe'] = bool(descriptor_stable and path_matches)
                reports.append(report)
            result = {'format':'tofu.experiment-read-probe/v1',
                'command_sha256':hashlib.sha256(json.dumps(list(command),ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),
                'project_root_sha256':hashlib.sha256(project.encode()).hexdigest(),
                'input_binding':'borrowed_readonly_descriptors' if pin_inputs else 'visible_paths',
                'effective_command_sha256':hashlib.sha256(json.dumps(effective_command,ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),
                'returncode':returncode, 'command_result':result_text,
                'timed_out':timed_out,'duration_seconds':time.monotonic()-started,
                'trace_transport_complete':returncode == 0 and not timed_out and not errors and not any(counts.values()),
                'unparsed_events':counts['unparsed_events'],
                'inputs':reports,'coverage':'read,pread64,readable mmap of visible paths only; initial hashes and file metadata do not attest exact loaded bytes, namespace identity, semantic use or complete dependencies',
                'instrumentation':'strace alters execution timing; observed duration is not uninstrumented performance',
                'binding_scope':'Descriptors bind supplied argv at launch, not immutable contents or proof of consumption; in-place edits remain possible and descendant forwarding is target-owned',
                'trust':'internal observation adapter; no same-UID tamper resistance or scientific acceptance authority'}
            result['evidence_result'] = command_evidence_text(result_text,result)
            return result


