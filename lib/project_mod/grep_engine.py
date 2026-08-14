"""Shared, streaming search execution for ``grep_search`` and run-command grep.

The public project search tool supplies an rg and a GNU-grep argv and lets this
module pick the fastest available backend.  The run-command redirect supplies
the user's GNU argv verbatim and deliberately selects GNU grep: arbitrary GNU
options must keep their native stdout, stderr, and exit-code behaviour.

The runner relays complete lines while the backend is alive.  It can enforce a
global match limit (unlike ``rg -m``, which is per file), and on timeout it
kills the backend tree without discarding already-forwarded complete lines.
"""

from __future__ import annotations

# This module is also executed by absolute path from a rewritten shell command.
# Make the repository importable before importing ``lib`` in that mode.
_SCRIPT_MODE = __package__ in (None, '')
if _SCRIPT_MODE:  # pragma: no cover - exercised via helper subprocess
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import argparse
import getopt
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

if _SCRIPT_MODE:  # keep helper startup independent from the application boot
    class _QuietLogger:
        def debug(self, *_args, **_kwargs):
            pass

    logger = _QuietLogger()
    IGNORE_DIRS = frozenset()
else:
    from lib.log import get_logger
    from lib.project_mod.config import IGNORE_DIRS
    logger = get_logger(__name__)


@dataclass(frozen=True)
class SearchRequest:
    """Structured request understood by the common search process runner.

    ``rg_argv`` and ``gnu_argv`` are complete argv values (binary included).
    The remaining fields describe the search contract and are intentionally
    backend-neutral; callers and tests can inspect them without reverse-parsing
    an argv.  Raw run-command grep uses ``gnu_argv`` plus ``raw_gnu=True``.
    """

    cwd: str
    patterns: tuple[str, ...] = ()
    pattern_mode: str = 'bre'       # bre | ere | fixed | pcre
    file_operands: tuple[str, ...] = ()
    recursive: bool = False
    includes: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    exclude_dirs: tuple[str, ...] = ()
    before_context: int = 0
    after_context: int = 0
    count_only: bool = False
    list_mode: str = ''             # '' | with | without
    quiet: bool = False
    binary_mode: str = 'binary'
    max_results: Optional[int] = None
    timeout: Optional[float] = None
    rg_argv: tuple[str, ...] = ()
    gnu_argv: tuple[str, ...] = ()
    preferred_backend: str = 'auto'  # auto | rg | gnu
    raw_gnu: bool = False
    match_line: Optional[Callable[[bytes], bool]] = field(
        default=None, compare=False, repr=False)


@dataclass
class SearchResult:
    stdout: bytes = b''
    stderr: bytes = b''
    returncode: int = 2
    backend: str = ''
    timed_out: bool = False
    limit_reached: bool = False
    consumer_closed: bool = False
    unavailable: bool = False


def _which_argv(argv: Sequence[str]) -> Optional[list[str]]:
    if not argv:
        return None
    command = list(argv)
    override = os.environ.get('TOFU_GREP_EXECUTABLE', '').strip()
    if override and os.path.basename(command[0]) in ('grep', 'egrep', 'fgrep'):
        command[0] = override
    binary = command[0]
    if os.path.sep not in binary:
        resolved = shutil.which(binary)
        if not resolved:
            return None
        # Keep argv[0] exactly as invoked. GNU grep includes it in diagnostic
        # prefixes (``grep:`` versus ``/usr/bin/grep:``), so replacing it with
        # the resolved PATH would be a visible compatibility regression.
    elif not os.path.isfile(binary):
        return None
    return command


def select_backend(request: SearchRequest) -> tuple[str, Optional[list[str]]]:
    """Return ``(backend, argv)`` using rg only when the caller supplied it.

    A raw GNU invocation never gets heuristically translated: falling back to
    native grep is what provides complete GNU option compatibility.
    """
    preference = request.preferred_backend
    if not request.raw_gnu and preference in ('auto', 'rg'):
        rg = _which_argv(request.rg_argv)
        if rg is not None:
            return 'rg', rg
        if preference == 'rg' and not request.gnu_argv:
            return 'rg', None
    if preference in ('auto', 'gnu') or request.raw_gnu:
        grep = _which_argv(request.gnu_argv)
        if grep is not None:
            return 'gnu', grep
    return '', None


def _descendant_pids(root_pid: int) -> list[int]:
    """Best-effort process-tree discovery without a dependency on psutil."""
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir('/proc')
    except OSError as exc:
        logger.debug('cannot enumerate /proc for grep descendants: %s', exc)
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            # /proc/PID/stat: pid (comm) state ppid ...; split after the final
            # ')' because command names may themselves contain spaces.
            raw = open(f'/proc/{entry}/stat', encoding='utf-8').read()
            tail = raw[raw.rfind(')') + 2:].split()
            ppid = int(tail[1])
            children.setdefault(ppid, []).append(int(entry))
        except (OSError, ValueError, IndexError) as exc:
            logger.debug('cannot inspect grep descendant pid %s: %s', entry, exc)
            continue
    found = []
    stack = list(children.get(root_pid, ()))
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children.get(pid, ()))
    return found


def _terminate_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    descendants = _descendant_pids(proc.pid)
    for pid in reversed(descendants):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            logger.debug('grep descendant SIGTERM failed pid=%s: %s', pid, exc)
            pass
    try:
        proc.terminate()
    except OSError as exc:
        logger.debug('grep process terminate failed pid=%s: %s', proc.pid, exc)
        pass
    try:
        proc.wait(timeout=0.35)
        return
    except subprocess.TimeoutExpired:
        pass
    for pid in reversed(descendants):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as exc:
            logger.debug('grep descendant SIGKILL failed pid=%s: %s', pid, exc)
            pass
    try:
        proc.kill()
    except OSError as exc:
        logger.debug('grep process kill failed pid=%s: %s', proc.pid, exc)
        pass


def _write_all(sink: Callable[[bytes], object], data: bytes) -> bool:
    if not data:
        return True
    try:
        result = sink(data)
        return result is not False
    except (BrokenPipeError, ConnectionResetError) as exc:
        logger.debug('grep output consumer closed: %s', exc)
        return False


def run_search(
    request: SearchRequest,
    *,
    stdout_sink: Optional[Callable[[bytes], object]] = None,
    stderr_sink: Optional[Callable[[bytes], object]] = None,
) -> SearchResult:
    """Run and stream a search request, forwarding complete lines promptly."""
    backend, command = select_backend(request)
    if command is None:
        return SearchResult(backend=backend, unavailable=True, returncode=127)

    captured_out: list[bytes] = []
    captured_err: list[bytes] = []
    out_sink = stdout_sink or captured_out.append
    err_sink = stderr_sink or captured_err.append
    try:
        proc = subprocess.Popen(
            command,
            cwd=request.cwd,
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Stay in the outer run_command process group. Whole-command Stop
            # must kill the shell, this helper, and grep together.
            start_new_session=False,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.debug('[grep_engine] backend start failed: %s', exc)
        return SearchResult(backend=backend, unavailable=True, returncode=127)

    sel = selectors.DefaultSelector()
    streams = {
        proc.stdout.fileno(): ('stdout', proc.stdout, out_sink),
        proc.stderr.fileno(): ('stderr', proc.stderr, err_sink),
    }
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    for _name, pipe, _sink in streams.values():
        os.set_blocking(pipe.fileno(), False)
        sel.register(pipe, selectors.EVENT_READ)

    started = time.monotonic()
    match_count = 0
    timed_out = False
    limit_reached = False
    consumer_closed = False

    def forward_lines(name: str, sink: Callable[[bytes], object], *, eof=False) -> bool:
        nonlocal match_count, limit_reached
        buf = buffers[name]
        while True:
            pos = buf.find(b'\n')
            if pos < 0:
                break
            line = bytes(buf[:pos + 1])
            del buf[:pos + 1]
            if name == 'stdout' and request.max_results is not None:
                is_match = request.match_line(line) if request.match_line else True
                if is_match:
                    match_count += 1
            if not _write_all(sink, line):
                return False
            if (name == 'stdout' and request.max_results is not None
                    and match_count >= request.max_results):
                limit_reached = True
                return True
        if eof and buf:
            # A naturally-completed command may legitimately omit its final
            # newline. Timeout/forced-limit callers pass eof=False and thereby
            # discard only the possibly torn trailing record.
            data = bytes(buf)
            buf.clear()
            if not _write_all(sink, data):
                return False
        return True

    while sel.get_map():
        if request.timeout is not None and not timed_out \
                and time.monotonic() - started >= request.timeout:
            timed_out = True
            _terminate_tree(proc)
        events = sel.select(0.1)
        if not events and proc.poll() is not None:
            # Pipes can become readable only to report EOF; loop once more.
            events = sel.select(0)
        for key, _mask in events:
            name, pipe, sink = streams[key.fd]
            try:
                chunk = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                buffers[name].extend(chunk)
                if not forward_lines(name, sink):
                    consumer_closed = True
                    _terminate_tree(proc)
                    break
                if limit_reached:
                    _terminate_tree(proc)
                    break
            else:
                sel.unregister(pipe)
                natural_eof = not (timed_out or limit_reached or consumer_closed)
                if not forward_lines(name, sink, eof=natural_eof):
                    consumer_closed = True
                    _terminate_tree(proc)
                    break
                pipe.close()
        if timed_out or limit_reached or consumer_closed:
            # Drain ready complete records after termination, but never wait
            # for a misbehaving descendant that retained a pipe descriptor.
            drain_until = time.monotonic() + 0.5
            while sel.get_map() and time.monotonic() < drain_until:
                ready = sel.select(0.02)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                for key, _mask in ready:
                    name, pipe, sink = streams[key.fd]
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        sel.unregister(pipe)
                        pipe.close()
                        continue
                    if not limit_reached or name == 'stderr':
                        buffers[name].extend(chunk)
                        if not forward_lines(name, sink):
                            consumer_closed = True
            break

    sel.close()
    if proc.poll() is None:
        _terminate_tree(proc)
    try:
        returncode = proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        returncode = -signal.SIGKILL
    if timed_out:
        returncode = 124
    elif limit_reached:
        returncode = 0
    elif consumer_closed:
        returncode = 128 + signal.SIGPIPE
    return SearchResult(
        stdout=b''.join(captured_out),
        stderr=b''.join(captured_err),
        returncode=returncode,
        backend=backend,
        timed_out=timed_out,
        limit_reached=limit_reached,
        consumer_closed=consumer_closed,
    )


def _fd_sink(fd: int) -> Callable[[bytes], bool]:
    def write(data: bytes) -> bool:
        view = memoryview(data)
        while view:
            try:
                n = os.write(fd, view)
            except InterruptedError as exc:
                logger.debug('grep output write interrupted, retrying: %s', exc)
                continue
            except (BrokenPipeError, OSError) as exc:
                if isinstance(exc, BrokenPipeError) or getattr(exc, 'errno', None) == 32:
                    return False
                raise
            view = view[n:]
        return True
    return write


def _inject_safe_excludes(program: str, argv: Sequence[str],
                          exclude_dirs: Sequence[str] = ()) -> list[str]:
    # These are the same descent guards used by grep_search. They are GNU
    # options, inserted before the user's argv so later user flags and ``--``
    # continue to be interpreted exactly by GNU grep.
    command = [program]
    for dirname in sorted(exclude_dirs or IGNORE_DIRS):
        command.extend(('--exclude-dir', dirname))
    command.extend(argv)
    return command


_TEXT_GLOB_RE = re.compile(
    r'^(?:.*/)?[^/]*\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|css|scss|sass|less|'
    r'html|htm|vue|svelte|java|kt|kts|c|cc|cpp|cxx|h|hh|hpp|hxx|rs|go|'
    r'rb|php|swift|scala|sh|bash|zsh|fish|sql|md|rst|txt|json|jsonl|yaml|'
    r'yml|toml|ini|cfg|conf|xml|svg)$', re.IGNORECASE)
_BRE_META = frozenset('.[^$*\\')
_ERE_META = frozenset('.[^$*\\(){}+?|')


def _raw_grep_rg_argv(program: str, argv: Sequence[str], cwd: str,
                      safe_excludes: Sequence[str]) -> tuple[str, ...]:
    """Translate a deliberately narrow, byte-compatible GNU subset to rg.

    Eligibility requires a recursive search constrained to known text-file
    include globs, existing directory operands, and a fixed/literal pattern.
    That excludes the semantic fault lines where rg visibly differs from GNU
    (zero counts, binary notices, missing operands, pattern files, PCRE, and
    device/directory policies). Everything else falls through to GNU.
    """
    if os.environ.get('TOFU_GREP_EXECUTABLE') or \
            os.environ.get('TOFU_GREP_FORCE_GNU') == '1':
        return ()
    if os.path.basename(program) not in ('grep', 'fgrep'):
        return ()
    short = 'EFGivwnxHhclLoqrsRIayUue:f:m:A:B:C:d:D:'
    long = [
        'extended-regexp', 'basic-regexp', 'fixed-strings', 'ignore-case',
        'invert-match', 'word-regexp', 'line-regexp', 'line-number',
        'with-filename', 'no-filename', 'count', 'files-with-matches',
        'files-without-match', 'only-matching', 'quiet', 'silent',
        'recursive', 'dereference-recursive', 'no-messages', 'text',
        'regexp=', 'file=', 'max-count=', 'after-context=',
        'before-context=', 'context=', 'include=', 'exclude=',
        'exclude-dir=', 'directories=', 'binary-files=', 'color=',
        'colour=', 'line-buffered', 'unix-byte-offsets',
    ]
    try:
        options, positionals = getopt.gnu_getopt(list(argv), short, long)
    except getopt.GetoptError as exc:
        logger.debug('grep argv is not eligible for rg translation: %s', exc)
        return ()

    mode = 'fixed' if os.path.basename(program) == 'fgrep' else 'bre'
    patterns = []
    includes = []
    excludes = []
    exclude_dirs = list(safe_excludes)
    recursive = False
    follow = False
    rg_flags = []
    for opt, value in options:
        if opt in ('-E', '--extended-regexp'):
            mode = 'ere'
        elif opt in ('-G', '--basic-regexp'):
            mode = 'bre'
        elif opt in ('-F', '--fixed-strings'):
            mode = 'fixed'
        elif opt in ('-e', '--regexp'):
            patterns.append(value)
        elif opt in ('-f', '--file', '-c', '--count', '-D'):
            return ()
        elif opt in ('-r', '--recursive'):
            recursive = True
        elif opt in ('-R', '--dereference-recursive'):
            recursive = True
            follow = True
        elif opt in ('-i', '-y', '--ignore-case'):
            rg_flags.append('-i')
        elif opt in ('-v', '--invert-match'):
            rg_flags.append('-v')
        elif opt in ('-w', '--word-regexp'):
            rg_flags.append('-w')
        elif opt in ('-x', '--line-regexp'):
            rg_flags.append('-x')
        elif opt in ('-n', '--line-number'):
            rg_flags.append('-n')
        elif opt in ('-H', '--with-filename'):
            rg_flags.append('--with-filename')
        elif opt in ('-h', '--no-filename'):
            rg_flags.append('--no-filename')
        elif opt in ('-l', '--files-with-matches'):
            rg_flags.append('--files-with-matches')
        elif opt in ('-L', '--files-without-match'):
            rg_flags.append('--files-without-match')
        elif opt in ('-o', '--only-matching'):
            rg_flags.append('--only-matching')
        elif opt in ('-q', '--quiet', '--silent'):
            rg_flags.append('--quiet')
        elif opt in ('-s', '--no-messages'):
            rg_flags.append('--no-messages')
        elif opt in ('-a', '--text'):
            rg_flags.append('--text')
        elif opt == '-I':
            pass  # rg's default binary policy is GNU --without-match
        elif opt in ('-U', '-u', '--unix-byte-offsets', '--line-buffered'):
            pass
        elif opt in ('-m', '--max-count'):
            rg_flags.extend(('--max-count', value))
        elif opt in ('-A', '--after-context'):
            rg_flags.extend(('--after-context', value))
        elif opt in ('-B', '--before-context'):
            rg_flags.extend(('--before-context', value))
        elif opt in ('-C', '--context'):
            rg_flags.extend(('--context', value))
        elif opt == '--include':
            includes.append(value)
        elif opt == '--exclude':
            excludes.append(value)
        elif opt == '--exclude-dir':
            exclude_dirs.append(value)
        elif opt in ('-d', '--directories'):
            if value != 'recurse':
                return ()
            recursive = True
        elif opt == '--binary-files':
            if value == 'text':
                rg_flags.append('--text')
            elif value != 'without-match':
                return ()
        elif opt in ('--color', '--colour'):
            if value not in ('auto', 'never'):
                return ()
        else:
            return ()

    if patterns:
        operands = positionals
    elif positionals:
        patterns = [positionals[0]]
        operands = positionals[1:]
    else:
        return ()
    if not recursive or not patterns or not includes:
        return ()
    if not all(_TEXT_GLOB_RE.fullmatch(glob) for glob in includes):
        return ()
    meta = _BRE_META if mode == 'bre' else _ERE_META
    if mode != 'fixed' and any(any(ch in meta for ch in pat)
                               for pat in patterns):
        return ()
    # Missing files and explicit file operands have observable diagnostic and
    # filename-prefix differences. Restrict the fast path to real directories.
    for operand in operands:
        resolved = operand if os.path.isabs(operand) else os.path.join(cwd, operand)
        if not os.path.isdir(resolved):
            return ()

    # Match grep_search's safe traversal contract: ripgrep honors .gitignore
    # and skips hidden trees by default, while explicit IGNORE_DIRS globs cover
    # non-git workspaces. This is the reason the fast path stays bounded on
    # FUSE projects where native grep may reach a useful subtree only after
    # walking caches and generated artifacts for minutes.
    command = ['rg', '--no-heading', '--color=never']
    command.extend(rg_flags)
    if follow:
        command.append('--follow')
    command.append('--fixed-strings')
    for glob in includes:
        command.extend(('--glob', glob))
    for glob in excludes:
        command.extend(('--glob', '!' + glob))
    for dirname in exclude_dirs:
        command.extend(('--glob', f'!{dirname}/'))
    for pattern in patterns:
        command.extend(('--regexp', pattern))
    command.append('--')
    command.extend(operands)
    return tuple(command)


def helper_main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--program', default='grep')
    parser.add_argument('--timeout', type=float, default=None)
    parser.add_argument('--exclude-dir', action='append', default=[])
    ns, rest = parser.parse_known_args(argv)
    if rest and rest[0] == '--':
        rest = rest[1:]
    timeout = ns.timeout
    if timeout is None:
        try:
            timeout = float(os.environ.get(
                'TOFU_GREP_REDIRECT_DEADLINE_S', '40'))
        except (TypeError, ValueError) as exc:
            logger.debug('invalid grep redirect deadline, using default: %s', exc)
            timeout = 40.0
    command = _inject_safe_excludes(ns.program, rest, ns.exclude_dir)
    rg_command = _raw_grep_rg_argv(ns.program, rest, os.getcwd(), ns.exclude_dir)
    request = SearchRequest(
        cwd=os.getcwd(),
        rg_argv=rg_command,
        gnu_argv=tuple(command),
        preferred_backend='auto',
        raw_gnu=not bool(rg_command),
        timeout=max(0.01, timeout) if timeout and timeout > 0 else None,
    )
    result = run_search(
        request,
        stdout_sink=_fd_sink(sys.stdout.fileno()),
        stderr_sink=_fd_sink(sys.stderr.fileno()),
    )
    if result.unavailable:
        os.write(sys.stderr.fileno(), (
            b'grep_search helper: GNU grep is unavailable; full GNU option '
            b'compatibility cannot be provided on this host.\n'))
        return 2
    if result.timed_out:
        seconds = f'{timeout:g}'.encode('ascii', errors='replace')
        os.write(sys.stderr.fileno(),
                 b'grep_search: timed out after ' + seconds
                 + b's; partial results above. Narrow the path or add an '
                   b'--include glob.\n')
        return 124
    return result.returncode


if __name__ == '__main__':  # pragma: no cover - covered through run_command
    raise SystemExit(helper_main())
