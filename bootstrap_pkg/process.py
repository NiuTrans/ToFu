"""Bounded pre-boot subprocesses; shared by bootstrap repairs and install.sh.

No application imports. Stream redacted output, retain only a diagnostic tail,
and enforce a wall-clock deadline while reading (not after EOF). The CLI runs
one command, with at most one retry for recognized transient network failures.
"""
from __future__ import annotations

import argparse
from collections import deque
import os
import re
import signal
import subprocess
import threading
import time
from typing import Callable

OUTPUT_LIMIT = 64 * 1024
STREAM_LIMIT = 2 * 1024 * 1024
_URL_SECRET = re.compile(r'(https?://)[^/\s@]+@', re.I)
_QUERY_SECRET = re.compile(r'([?&](?:token|key|api_key|signature|access_token)=)[^&\s]+', re.I)
_TRANSIENT = re.compile(
    r'connection (?:reset|aborted)|temporary failure|temporarily unavailable|'
    r'timed? out|timeout|HTTP[^\n]*(?:429|502|503|504)|'
    r'(?:429|502|503|504) (?:Too Many|Bad Gateway|Service|Gateway)', re.I)
_PERMANENT = re.compile(
    r'certificate|CERTIFICATE_VERIFY_FAILED|no space left|permission denied|'
    r'no matching distribution|unsatisfiable|resolution impossible', re.I)


def redact(text: str) -> str:
    """Never echo URL credentials or explicitly configured provider secrets."""
    text = _URL_SECRET.sub(r'\1[redacted]@', text)
    text = _QUERY_SECRET.sub(r'\1[redacted]', text)
    for name in ('LLM_API_KEY', 'LLM_API_KEYS', 'PIP_PASSWORD', 'UV_INDEX_PASSWORD'):
        for value in os.environ.get(name, '').split(','):
            if value:
                text = text.replace(value, '[redacted]')
    return text


def run_command(
    command: list[str], *, cwd: str | None = None, timeout: float = 300,
    emit: Callable[[str], None] | None = None, env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Return (exit code, <=64 KiB tail); timeout=124; reap our process tree.

    A reader uses bounded chunks, including for output with no newlines. The
    watchdog is independent of that pipe, so silent/stuck children expire too.
    No stdin is inherited: repair/package-manager prompts must never hang.
    """
    output: deque[str] = deque()
    size = 0
    streamed = 0
    discarding_line = False
    expired = threading.Event()
    child_env = dict(os.environ if env is None else env)
    child_env.update(PIP_NO_INPUT='1', GIT_TERMINAL_PROMPT='0',
                     UV_NO_PROGRESS='1', PYTHONUNBUFFERED='1')
    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=(os.name != 'nt'),
        )
    except OSError as exc:
        message = redact(f'Could not start installer command: {exc}')
        if emit:
            emit(message)
        return 127, message

    def kill_tree(sig: int) -> None:
        try:
            if os.name != 'nt':
                os.killpg(process.pid, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass

    def terminate() -> None:
        expired.set()
        kill_tree(signal.SIGTERM)
        # Kill descendants even when the direct child exited but a descendant
        # kept the output descriptor open. The session belongs only to us.
        time.sleep(0.2)
        kill_tree(signal.SIGKILL if os.name != 'nt' else signal.SIGTERM)

    watchdog = threading.Timer(timeout, terminate)
    watchdog.daemon = True
    watchdog.start()
    try:
        assert process.stdout is not None
        while chunk := process.stdout.readline(4096):
            # Never expose half of a credential split at our chunk boundary.
            # Overlong third-party lines are omitted as a unit, not piecemeal.
            if discarding_line:
                discarding_line = not chunk.endswith(b'\n')
                continue
            if len(chunk) == 4096 and not chunk.endswith(b'\n'):
                discarding_line = True
                text = '[installer output line omitted: exceeds 4096 bytes]'
            else:
                text = redact(chunk.decode('utf-8', errors='replace').rstrip('\r\n'))
            output.append(text + '\n')
            size += len((text + '\n').encode('utf-8'))
            while size > OUTPUT_LIMIT and output:
                size -= len(output.popleft().encode('utf-8'))
            if emit and streamed < STREAM_LIMIT:
                emit(text)
                streamed += len(text.encode('utf-8'))
                if streamed >= STREAM_LIMIT:
                    emit('[installer output capped at 2 MiB; diagnostic tail retained]')
        process.wait()
    except BaseException:
        kill_tree(signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The direct child can exit on TERM while descendants ignore it.
            # Cancellation still owns their session, even after wait() returns.
            kill_tree(signal.SIGKILL if os.name != 'nt' else signal.SIGTERM)
            process.wait()
        raise
    finally:
        watchdog.cancel()
        watchdog.join(timeout=1)
        if process.stdout:
            process.stdout.close()
    tail = ''.join(output)
    if expired.is_set():
        message = f'Installer command exceeded its {timeout:g}s deadline (exit 124).'
        if emit:
            emit(message)
        return 124, (tail + message).encode('utf-8')[-OUTPUT_LIMIT:].decode('utf-8', errors='ignore')
    return process.returncode, tail


def main() -> int:
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--retry-network', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 0 < args.timeout <= 3600:
        parser.error('--timeout must be between 0 and 3600 seconds')
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a command is required')
    for attempt in range(2 if args.retry_network else 1):
        rc, tail = run_command(command, timeout=args.timeout,
                               emit=lambda line: print(line, flush=True))
        if rc == 0 or rc == 124 or _PERMANENT.search(tail) or not _TRANSIENT.search(tail):
            return rc if rc >= 0 else 128 - rc
        if attempt == 0 and args.retry_network:
            print('Transient network failure; retrying once with cached downloads.', flush=True)
            time.sleep(1)
    return rc if rc >= 0 else 128 - rc


if __name__ == '__main__':
    raise SystemExit(main())
