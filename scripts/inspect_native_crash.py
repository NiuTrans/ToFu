#!/usr/bin/env python3
"""Inspect an explicitly supplied ELF core without attaching to a live service.

Entry point: CLI ``--core PATH --executable PATH [--output NEW_JSON]``.
Dependencies: standard library; optional GDB. Reports core completeness before
debugging, preserves native signal/PC evidence, records executable identity,
and bounds debugger time/output. Does not change core policy, launch Tofu,
read application storage, or print frame arguments/locals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time

MAX_PROGRAM_HEADERS = 16384
DEBUGGER_TIMEOUT_SECONDS = 30.0
DEBUGGER_OUTPUT_BYTES = 1024 * 1024


def inspect_elf_core(path: Path) -> dict:
    """Read only bounded ELF headers; a truncated dump is never called complete."""
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('core must be a regular file, not a device or pipe')
    with path.open('rb') as source:
        header = source.read(64)
        if len(header) != 64 or header[:4] != b'\x7fELF':
            raise ValueError('core is not a complete ELF header')
        if header[4] != 2 or header[5] not in (1, 2):
            raise ValueError('only ELF64 cores with a known byte order are supported')
        endian = '<' if header[5] == 1 else '>'
        fields = struct.unpack(endian + 'HHIQQQIHHHHHH', header[16:])
        file_type, machine = fields[:2]
        phoff, shoff = fields[4:6]
        phsize, phcount, shsize = fields[8:11]
        if file_type != 4 or phsize != 56:
            raise ValueError('expected an ELF64 core and standard program headers')
        if phcount == 0xffff:
            if shsize != 64:
                raise ValueError('invalid extended program-header count')
            source.seek(shoff)
            section = source.read(64)
            if len(section) != 64:
                raise ValueError('missing extended program-header count')
            phcount = struct.unpack(endian + 'IIQQQQIIQQ', section)[7]
        if not 0 < phcount <= MAX_PROGRAM_HEADERS:
            raise ValueError('program-header count is outside the diagnostic budget')
        size = os.fstat(source.fileno()).st_size
        headers_end = phoff + phsize * phcount
        if headers_end > size:
            raise ValueError('core is truncated within its program headers')
        source.seek(phoff)
        required_size = headers_end
        missing = 0
        for _ in range(phcount):
            program = struct.unpack(endian + 'IIQQQQQQ', source.read(phsize))
            end = program[2] + program[5]
            if program[5]:
                required_size = max(required_size, end)
                missing += end > size
        return {'format': 'ELF64', 'machine': machine, 'sizeBytes': size,
                'expectedSizeBytes': required_size, 'complete': size >= required_size,
                'truncatedSegments': missing, 'programHeaders': phcount}


def capture_debugger(command: list[str], *, timeout: float = DEBUGGER_TIMEOUT_SECONDS,
                     max_bytes: int = DEBUGGER_OUTPUT_BYTES) -> dict:
    """Bound a private debugger process and its pipe, including failure cleanup."""
    environment = dict(os.environ)
    # The debugger must not inherit an injected filesystem interceptor or
    # incompatible conda libraries; the original mappings remain in the core.
    environment.pop('LD_PRELOAD', None)
    environment.pop('LD_LIBRARY_PATH', None)
    parts: list[bytes] = []
    captured = 0
    limit_reason = ''
    # Linux pipe collectors ignore RLIMIT_CORE=0. The kernel's one-byte
    # recursion guard suppresses both piped and regular cores (fs/coredump.c).
    # Use an isolated launcher instead of preexec_fn in a threaded caller;
    # lower only the owned child's limit and pass debugger argv verbatim.
    launcher = ('import os,resource,sys; '
                'limit=1 if sys.platform.startswith("linux") else 0; '
                'resource.setrlimit(resource.RLIMIT_CORE,(limit,limit)); '
                'os.execvpe(sys.argv[1],sys.argv[1:],os.environ)')
    command = [sys.executable, '-I', '-S', '-c', launcher, *command]
    child = subprocess.Popen(command, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=environment,
                             start_new_session=True)
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    limit_reason = 'timeout'
                    break
                events = selector.select(min(0.2, remaining))
                for key, _ in events:
                    chunk = key.fileobj.read1(64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    room = max(0, max_bytes - captured)
                    parts.append(chunk[:room])
                    captured += min(room, len(chunk))
                    if len(chunk) > room:
                        limit_reason = 'output_budget'
                        break
                if limit_reason:
                    break
            if not limit_reason:
                try:
                    child.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    limit_reason = 'timeout'
    finally:
        if limit_reason or child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=2)
        if child.stdout is not None:
            child.stdout.close()
    return {'returnCode': child.returncode, 'limitReason': limit_reason,
            'output': b''.join(parts).decode('utf-8', errors='replace')}


def inspect_native_crash(core: Path, executable: Path, *, gdb: str | None = None) -> dict:
    core = core.resolve(strict=True)
    executable = executable.resolve(strict=True)
    digest = hashlib.sha256()
    with executable.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    report = {'corePath': str(core), 'core': inspect_elf_core(core),
              'executablePath': str(executable), 'executableSha256': digest.hexdigest(),
              'executableMatch': 'not_proven', 'warnings': []}
    if not report['core']['complete']:
        report['warnings'].append('Core is truncated; missing stacks/mappings cannot be recovered.')
    debugger = gdb or shutil.which('gdb')
    if not debugger:
        report['warnings'].append('GDB unavailable; native signal and stack remain unresolved.')
        return report
    commands = [debugger, '-q', '-nx', '-nh', '-batch',
                '-iex', 'set auto-load off', '-iex', 'set pagination off',
                '-iex', 'set print thread-events off',
                '-iex', 'set print frame-arguments none',
                '--se', str(executable), '--core', str(core)]
    for command in ('printf "NATIVE_SIGNAL=%d\\n", $_siginfo.si_signo',
                    'printf "NATIVE_SIGNAL_CODE=%d\\n", $_siginfo.si_code',
                    'printf "NATIVE_FAULT_ADDRESS=%p\\n", $_siginfo._sifields._sigfault.si_addr',
                    'bt 24', 'info registers', 'x/12i $pc-24', 'info proc mappings'):
        commands.extend(['-ex', command])
    result = capture_debugger(commands)
    report['debugger'] = result
    output = result['output']
    for field, marker in (('signal', 'NATIVE_SIGNAL'), ('signalCode', 'NATIVE_SIGNAL_CODE')):
        found = re.search(rf'^{marker}=(-?\d+)$', output, re.M)
        if found:
            report[field] = int(found.group(1))
    found = re.search(r'^NATIVE_FAULT_ADDRESS=(\S+)$', output, re.M)
    # si_addr shares a union with sender metadata. It is not a fault address
    # for SI_USER/SI_TKILL or for other signal classes.
    if (found and report.get('signal') in (signal.SIGSEGV, signal.SIGBUS,
                                          signal.SIGILL, signal.SIGFPE)
            and report.get('signalCode', 0) > 0):
        report['faultAddress'] = found.group(1)
    report['nativeMappedFiles'] = sorted(set(re.findall(
        r'^\s*0x[0-9a-f]+\s+0x[0-9a-f]+\s+0x[0-9a-f]+\s+0x[0-9a-f]+\s+(.+)$', output, re.M)))
    if result['limitReason'] or result['returnCode']:
        report['warnings'].append('Debugger did not complete within its diagnostic boundary.')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core', required=True, type=Path)
    parser.add_argument('--executable', required=True, type=Path)
    parser.add_argument('--gdb')
    parser.add_argument('--output', type=Path, help='Create a new private JSON report; never overwrite')
    args = parser.parse_args()
    try:
        report = inspect_native_crash(args.core, args.executable, gdb=args.gdb)
        wire = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as sink:
                sink.write(wire)
            print(f'Native crash report: {args.output}')
        else:
            print(wire, end='')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(2, f'Native crash inspection failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
