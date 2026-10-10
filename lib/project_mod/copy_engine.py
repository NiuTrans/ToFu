"""Parallel ``cp`` engine for run_command: one copier, two transparent entries.

Motivation (measured 2026-09 on this repo's DolphinFS FUSE mount, 808-file /
68 MiB tree): ``cp -a`` 7.2s, tar-pipe 6.4s, rsync 9.9s, 16-way parallel
copy 2.5s. Tree copies are per-file metadata-latency bound, not throughput
bound; only parallelism attacks that. Single big files show no gap, so the
engine parallelizes across directory entries, never inside one file.

Two entries share one parser and one executor:

  * in-process (:func:`run_copy_plan`) — a lone ``cp`` command is executed
    inside run_command with the caller's deadline/abort/streaming seams,
    zero subprocess spawn;
  * helper splice (:func:`helper_main`) — inside compound commands the
    argv span is replaced by ``python copy_engine.py -- ...``; startup is
    amortized by the copy itself. An argv the planner would not have
    accepted re-execs the real ``cp`` (belt-and-suspenders transparency).

Accepted surface (anything else → native cp, see :func:`parse_cp_argv`):
``-r -R -a -p --preserve[=…] -f -v -d -P --no-dereference -u -n -x
--one-file-system --strip-trailing-slashes``, multiple sources, and the
``src/``-on-symlink dereference rule. Preservation covers mode, timestamps,
symlinks, hardlinks (with -a/-p/-d), sparse holes, xattrs (best effort),
ownership (euid 0 only). ``-i -l -s -b -S -t -T -H -L --parents --reflink
--sparse --backup --context --attributes-only --copy-contents`` stay native.

Standard-library only; the script-mode entry must boot without application
imports. Locale-matched GNU quoting (``‘x’`` under UTF-8, ``'x'`` otherwise)
keeps ``-v``/diagnostic text byte-identical to coreutils 8.22 output.
"""

from __future__ import annotations

import errno
import os
import shlex
import stat
import sys
import threading
from concurrent.futures import as_completed
from dataclasses import dataclass, field

_SCRIPT_MODE = __package__ in (None, '')
if _SCRIPT_MODE:  # pragma: no cover - exercised via helper subprocess
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))


# ── GNU-compatible presentation ────────────────────────────────────────

def _utf8_locale():
    probe = ' '.join(os.environ.get(k, '')
                     for k in ('LC_ALL', 'LC_CTYPE', 'LANG'))
    return 'utf-8' in probe.lower() or 'utf8' in probe.lower()


_LQ, _RQ = ('‘', '’') if _utf8_locale() else ("'", "'")


def _q(path):
    return f'{_LQ}{path}{_RQ}'


# ── Plan ───────────────────────────────────────────────────────────────

_FLAG_SHORTS_NOARG = frozenset('rRapfvdPunx')


@dataclass
class CopySource:
    display: str            # as typed (for diagnostics / -v)
    follow_root: bool       # trailing slash on a symlink operand


@dataclass
class CopyPlan:
    sources: list
    dest: str
    recursive: bool = False
    preserve: bool = False          # mode+timestamps (+links when -a/-p/-d)
    preserve_links: bool = False
    no_dereference: bool = False
    force: bool = False
    verbose: bool = False
    update: bool = False
    no_clobber: bool = False
    one_file_system: bool = False


def parse_cp_argv(argv):
    """Parse cp operands (program word excluded). Returns CopyPlan or None.

    None means "outside the proven surface — keep native cp". The parser
    never guesses: unknown flags, argument-taking flags, and interactive or
    link-producing modes all return None.
    """
    recursive = preserve = preserve_links = False
    no_deref = force = verbose = update = False
    no_clobber = one_file_system = strip_slashes = False
    operands = []
    i = 0
    n = len(argv)
    options_done = False
    while i < n:
        tok = argv[i]
        if not options_done and tok == '--':
            options_done = True
            i += 1
            continue
        if not options_done and tok.startswith('--'):
            name, sep, value = tok.partition('=')
            if name in ('--recursive',):
                recursive = True
            elif name == '--archive':
                recursive = preserve = preserve_links = no_deref = True
            elif name == '--preserve':
                preserve = True
                if sep:
                    accepted = {'mode', 'ownership', 'timestamps', 'links',
                                'context', 'xattr', 'all'}
                    values = value.split(',')
                    if any(v not in accepted for v in values):
                        return None
                    preserve_links = preserve_links or any(
                        v in ('links', 'all') for v in values)
            elif name == '--no-preserve':
                if sep and value in ('links',):
                    preserve_links = False
                elif sep and value not in ('links',):
                    # --no-preserve=mode etc. would strip -a semantics we
                    # already committed to; keep native.
                    return None
            elif name == '--force':
                force = True
            elif name == '--verbose':
                verbose = True
            elif name in ('--no-dereference',):
                no_deref = True
            elif name == '--update':
                update = True
            elif name == '--no-clobber':
                no_clobber = True
            elif name == '--one-file-system':
                one_file_system = True
            elif name == '--strip-trailing-slashes':
                strip_slashes = True
            else:
                return None        # --parents/--reflink/-t/-T/-b/…
            i += 1
            continue
        if not options_done and tok.startswith('-') and tok != '-':
            cluster = tok[1:]
            if not cluster:
                operands.append(tok)
                i += 1
                continue
            for ch in cluster:
                if ch not in _FLAG_SHORTS_NOARG:
                    return None    # -i -l -s -H -L and arg-taking flags
            for ch in cluster:
                if ch in 'rR':
                    recursive = True
                elif ch == 'a':
                    recursive = preserve = preserve_links = no_deref = True
                elif ch == 'p':
                    preserve = True
                elif ch == 'f':
                    force = True
                elif ch == 'v':
                    verbose = True
                elif ch in 'dP':
                    no_deref = True
                    if ch == 'd':
                        preserve_links = True
                elif ch == 'u':
                    update = True
                elif ch == 'n':
                    no_clobber = True
                elif ch == 'x':
                    one_file_system = True
            i += 1
            continue
        operands.append(tok)
        i += 1
    if len(operands) < 2:
        return None
    sources = []
    for operand in operands[:-1]:
        stripped = operand.rstrip('/')
        if not stripped:
            stripped = '/'          # root operand keeps one slash
        had_slash = (len(operand) > 1 and operand.endswith('/')
                     and not strip_slashes)
        sources.append(CopySource(display=operand if not strip_slashes
                                  else stripped,
                                  follow_root=had_slash))
    dest = operands[-1]
    return CopyPlan(
        sources=sources, dest=dest, recursive=recursive,
        preserve=preserve, preserve_links=preserve_links,
        no_dereference=no_deref, force=force, verbose=verbose,
        update=update, no_clobber=no_clobber,
        one_file_system=one_file_system)


# ── Executor ───────────────────────────────────────────────────────────

@dataclass
class CopyResult:
    exit_code: int = 0
    errors: list = field(default_factory=list)
    verbose_lines: list = field(default_factory=list)
    entries_copied: int = 0
    bytes_copied: int = 0
    timed_out: bool = False
    stopped: bool = False


_COPY_BUFFER = 1024 * 1024


def _copy_file_range(src_fd, dst_fd, size):
    """Kernel-side copy via sendfile(2) when available (file-to-file, Linux).

    ``os.copy_file_range`` is absent on this host's Python (removed upstream
    after kernel cross-fs issues); plain streaming is the measured-equal
    fallback (~200 MB/s sequential on the FUSE mount).
    """
    sendfile = getattr(os, 'sendfile', None)
    if sendfile is None:
        return False
    offset = 0
    remaining = size
    try:
        while remaining > 0:
            sent = sendfile(dst_fd, src_fd, offset, min(remaining, 1 << 30))
            if sent == 0:
                return True
            offset += sent
            remaining -= sent
    except OSError:
        return False
    return True


def _copy_stream(src_fd, dst_fd, size):
    remaining = size
    while remaining > 0:
        chunk = os.read(src_fd, min(remaining, _COPY_BUFFER))
        if not chunk:
            break
        view = memoryview(chunk)
        while view:
            written = os.write(dst_fd, view)
            view = view[written:]
        remaining -= len(chunk)


def _copy_sparse(src_fd, dst_fd, size):
    """Copy data extents only; holes stay holes (SEEK_HOLE/SEEK_DATA)."""
    pos = 0
    while pos < size:
        try:
            data = os.lseek(src_fd, pos, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                return True            # no more data extents
            return False
        if data >= size:
            return True
        try:
            hole = os.lseek(src_fd, data, os.SEEK_HOLE)
        except OSError:
            return False
        hole = min(hole, size)
        os.lseek(src_fd, data, os.SEEK_SET)
        os.lseek(dst_fd, data, os.SEEK_SET)
        _copy_stream(src_fd, dst_fd, hole - data)
        pos = hole
    return True


def _copy_contents(src_path, dst_fd, src_stat):
    src_fd = os.open(src_path, os.O_RDONLY)
    try:
        size = src_stat.st_size
        sparse = (hasattr(os, 'SEEK_HOLE')
                  and src_stat.st_blocks * 512 < size)
        done = False
        if sparse:
            done = _copy_sparse(src_fd, dst_fd, size)
        if not done:
            os.lseek(src_fd, 0, os.SEEK_SET)
            os.lseek(dst_fd, 0, os.SEEK_SET)
            done = _copy_file_range(src_fd, dst_fd, size)
        if not done:
            os.lseek(src_fd, 0, os.SEEK_SET)
            os.lseek(dst_fd, 0, os.SEEK_SET)
            _copy_stream(src_fd, dst_fd, size)
    finally:
        os.close(src_fd)


def _copy_xattrs(src_path, dst_path, *, follow=True):
    try:
        names = os.listxattr(src_path, follow_symlinks=follow)
    except OSError:
        return
    for name in names:
        try:
            value = os.getxattr(src_path, name, follow_symlinks=follow)
            os.setxattr(dst_path, name, value, follow_symlinks=follow)
        except OSError:
            continue                     # best effort, mirrors cp's leniency


def _apply_metadata(src_path, dst_path, src_stat, plan, *, follow=True):
    mode = stat.S_IMODE(src_stat.st_mode)
    try:
        if follow:
            os.chmod(dst_path, mode)
        else:
            os.lchmod(dst_path, mode)
    except (OSError, AttributeError):
        pass
    try:
        os.utime(dst_path, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns),
                 follow_symlinks=follow)
    except OSError:
        pass
    if os.geteuid() == 0:
        try:
            os.chown(dst_path, src_stat.st_uid, src_stat.st_gid,
                     follow_symlinks=follow)
        except OSError:
            pass
    _copy_xattrs(src_path, dst_path, follow=follow)


class _Engine:
    def __init__(self, plan, cwd, *, workers, on_verbose=None, on_error=None,
                 stop_check=None, deadline=None):
        self.plan = plan
        self.cwd = cwd
        self.workers = max(1, workers)
        self.on_verbose = on_verbose
        self.on_error = on_error
        self.stop_check = stop_check
        self.deadline = deadline
        self.result = CopyResult()
        self._lock = threading.Lock()
        self._hardlinks = {}             # (dev, ino) -> first dst path

    # ── plumbing ──
    def _resolve(self, path):
        return path if os.path.isabs(path) else os.path.join(self.cwd, path)

    def _interrupted(self):
        import time as _time
        if self.stop_check is not None and self.stop_check():
            self.result.stopped = True
            return True
        if self.deadline is not None and _time.monotonic() >= self.deadline:
            self.result.timed_out = True
            return True
        return False

    def _verbose(self, src_display, dst_display):
        line = f'{_q(src_display)} -> {_q(dst_display)}'
        if self.on_verbose is not None:
            self.on_verbose(line)
        with self._lock:
            self.result.verbose_lines.append(line)

    def _error(self, message):
        if self.on_error is not None:
            self.on_error(message)
        with self._lock:
            self.result.errors.append(message)
            self.result.exit_code = 1

    def _pool(self):
        if _SCRIPT_MODE:
            from tofu_workers import DemandExecutor
            return DemandExecutor(max_workers=self.workers, queue_capacity=0,
                max_abandoned_workers=self.workers, thread_name_prefix='tofu-cp',
                daemon_workers=False)
        from lib.observability import InstrumentedThreadPoolExecutor
        return InstrumentedThreadPoolExecutor(max_workers=self.workers,
            metric_pool='project-copy', thread_name_prefix='tofu-cp')

    def _run_level(self, tasks, fn):
        """Run tasks for one dependency level; stop scheduling on interrupt."""
        futures = []
        with self._pool() as pool:
            for task in tasks:
                if self._interrupted():
                    break
                try:
                    futures.append(pool.submit(fn, task))
                except Exception as error:
                    # Failed publication is unstarted. Preserve previously
                    # published copies and continue independent siblings.
                    self._error(f'cp: cannot start copy task: {error}')
            for future in as_completed(futures):
                try:
                    future.result()  # ordinary per-file errors settle inside
                except Exception as error:
                    self._error(f'cp: copy worker failed: {error}')

    # ── walk ──
    def _walk_tree(self, src_abs, src_display, dst_abs, dst_display,
                   src_stat, root_dev, dirs, files, links):
        """Collect a directory tree into phase buckets (GNU pre-order)."""
        dirs.append((0, src_abs, src_display, dst_abs, dst_display, src_stat))
        stack = [(src_abs, src_display, dst_abs, dst_display, 0)]
        while stack:
            if self._interrupted():
                return
            cur_abs, cur_disp, cur_dst_abs, cur_dst_disp, depth = stack.pop()
            try:
                with os.scandir(cur_abs) as it:
                    entries = list(it)
            except OSError as exc:
                self._error(f'cp: cannot access {_q(cur_disp)}: '
                            f'{exc.strerror}')
                continue
            for entry in entries:
                e_disp = f'{cur_disp.rstrip("/")}/{entry.name}'
                e_dst_abs = os.path.join(cur_dst_abs, entry.name)
                e_dst_disp = f'{cur_dst_disp.rstrip("/")}/{entry.name}'
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    self._error(f'cp: cannot stat {_q(e_disp)}: {exc.strerror}')
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if self.plan.one_file_system and st.st_dev != root_dev:
                        continue
                    rel_depth = depth + 1
                    dirs.append((rel_depth, entry.path, e_disp,
                                 e_dst_abs, e_dst_disp, st))
                    stack.append((entry.path, e_disp, e_dst_abs,
                                  e_dst_disp, rel_depth))
                elif stat.S_ISLNK(st.st_mode):
                    links.append((entry.path, e_disp, e_dst_abs,
                                  e_dst_disp, st))
                elif stat.S_ISREG(st.st_mode):
                    files.append((entry.path, e_disp, e_dst_abs,
                                  e_dst_disp, st))
                else:
                    # fifos/sockets/devices: GNU -a recreates them via mknod;
                    # without CAP_MKNOD this errors per entry. Report like cp.
                    self._error(
                        f'cp: cannot copy special file {_q(e_disp)}')

    # ── phases ──
    def _make_dir(self, task):
        if self._interrupted():
            return
        _depth, _src_abs, src_disp, dst_abs, dst_disp, st = task
        try:
            os.makedirs(dst_abs, exist_ok=True)
        except OSError as exc:
            self._error(f'cp: cannot create directory {_q(dst_disp)}: '
                        f'{exc.strerror}')
            return
        if self.plan.verbose:
            self._verbose(src_disp, dst_disp)
        with self._lock:
            self.result.entries_copied += 1

    def _copy_one_file(self, task):
        if self._interrupted():
            return
        src_abs, src_disp, dst_abs, dst_disp, st = task
        plan = self.plan
        if plan.no_clobber and os.path.lexists(dst_abs):
            return
        if plan.update:
            try:
                dst_st = os.stat(dst_abs)
                if dst_st.st_mtime_ns >= st.st_mtime_ns:
                    return
            except OSError:
                pass
        try:
            dst_st_l = os.lstat(dst_abs) if os.path.lexists(dst_abs) else None
        except OSError:
            dst_st_l = None
        if dst_st_l is not None and stat.S_ISDIR(dst_st_l.st_mode):
            self._error(f'cp: cannot overwrite directory {_q(dst_disp)} '
                        f'with non-directory {_q(src_disp)}')
            return
        try:
            dst_fd = os.open(dst_abs, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             stat.S_IMODE(st.st_mode))
        except OSError as exc:
            if plan.force and dst_st_l is not None:
                try:
                    os.remove(dst_abs)
                    dst_fd = os.open(
                        dst_abs, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                        stat.S_IMODE(st.st_mode))
                except OSError as exc2:
                    self._error(f'cp: cannot create regular file '
                                f'{_q(dst_disp)}: {exc2.strerror}')
                    return
            else:
                self._error(f'cp: cannot create regular file {_q(dst_disp)}: '
                            f'{exc.strerror}')
                return
        try:
            _copy_contents(src_abs, dst_fd, st)
        except OSError as exc:
            os.close(dst_fd)
            self._error(f'cp: error reading {_q(src_disp)}: {exc.strerror}')
            return
        try:
            os.close(dst_fd)
        except OSError as exc:
            self._error(f'cp: error writing {_q(dst_disp)}: {exc.strerror}')
            return
        if plan.preserve:
            _apply_metadata(src_abs, dst_abs, st, plan)
        if plan.preserve_links and st.st_nlink > 1:
            with self._lock:
                self._hardlinks.setdefault(
                    (st.st_dev, st.st_ino), (dst_abs, dst_disp))
        if plan.verbose:
            self._verbose(src_disp, dst_disp)
        with self._lock:
            self.result.entries_copied += 1
            self.result.bytes_copied += st.st_size

    def _copy_one_symlink(self, task):
        if self._interrupted():
            return
        src_abs, src_disp, dst_abs, dst_disp, st = task
        plan = self.plan
        if plan.no_clobber and os.path.lexists(dst_abs):
            return
        if plan.update:
            try:
                if os.lstat(dst_abs).st_mtime_ns >= st.st_mtime_ns:
                    return
            except OSError:
                pass
        try:
            target = os.readlink(src_abs)
        except OSError as exc:
            self._error(f'cp: cannot read symbolic link {_q(src_disp)}: '
                        f'{exc.strerror}')
            return
        try:
            if os.path.lexists(dst_abs):
                if stat.S_ISDIR(os.lstat(dst_abs).st_mode):
                    self._error(f'cp: cannot overwrite directory '
                                f'{_q(dst_disp)} with non-directory '
                                f'{_q(src_disp)}')
                    return
                os.remove(dst_abs)
            os.symlink(target, dst_abs)
        except OSError as exc:
            self._error(f'cp: cannot create symbolic link {_q(dst_disp)}: '
                        f'{exc.strerror}')
            return
        if plan.preserve:
            _apply_metadata(src_abs, dst_abs, st, plan, follow=False)
        if plan.verbose:
            self._verbose(src_disp, dst_disp)
        with self._lock:
            self.result.entries_copied += 1

    def _make_link(self, task):
        if self._interrupted():
            return
        inode_key, dst_abs, dst_disp = task
        with self._lock:
            canonical = self._hardlinks.get(inode_key)
        if canonical is None or canonical[0] == dst_abs:
            return
        try:
            if os.path.lexists(dst_abs):
                os.remove(dst_abs)
            os.link(canonical[0], dst_abs)
        except OSError as exc:
            self._error(f'cp: cannot create link {_q(dst_disp)}: '
                        f'{exc.strerror}')

    def _apply_dir_metadata(self, task):
        if self._interrupted():
            return
        _depth, src_abs, _src_disp, dst_abs, _dst_disp, st = task
        _apply_metadata(src_abs, dst_abs, st, self.plan)

    # ── top level ──
    def run(self):
        plan = self.plan
        dest_display = plan.dest
        dest_abs = self._resolve(plan.dest)
        dest_kind = None
        try:
            dest_st = os.stat(dest_abs)
            dest_kind = 'dir' if stat.S_ISDIR(dest_st.st_mode) else 'other'
        except OSError:
            pass
        if len(plan.sources) > 1 and dest_kind != 'dir':
            self._error(f'cp: target {_q(dest_display)} is not a directory')
            return self.result

        dirs, files, symlinks = [], [], []
        for source in plan.sources:
            if self._interrupted():
                break
            src_abs = self._resolve(source.display)
            try:
                st = (os.stat(src_abs) if source.follow_root
                      else os.lstat(src_abs))
            except OSError as exc:
                self._error(f'cp: cannot stat {_q(source.display)}: '
                            f'{exc.strerror}')
                continue
            if source.follow_root and not stat.S_ISDIR(st.st_mode):
                # ``cp operand/ dst`` on a non-directory operand is ENOTDIR.
                self._error(f'cp: cannot stat {_q(source.display)}: '
                            'Not a directory')
                continue
            is_symlink = stat.S_ISLNK(st.st_mode) and not source.follow_root
            base_name = source.display.rstrip('/').rsplit('/', 1)[-1] \
                or source.display.rstrip('/')
            if dest_kind == 'dir':
                dst_abs = os.path.join(dest_abs, base_name)
                dst_disp = f'{dest_display.rstrip("/")}/{base_name}'
            else:
                dst_abs, dst_disp = dest_abs, dest_display
            if stat.S_ISDIR(st.st_mode):
                if not plan.recursive:
                    self._error(f'cp: omitting directory {_q(source.display)}')
                    continue
                src_real = os.path.realpath(src_abs)
                dst_real = os.path.realpath(dst_abs)
                if dst_real == src_real or dst_real.startswith(
                        src_real + os.sep):
                    self._error(
                        f'cp: cannot copy a directory, {_q(source.display)}, '
                        f'into itself, {_q(dst_disp)}')
                    continue
                if os.path.lexists(dst_abs) and not os.path.isdir(dst_abs):
                    self._error(f'cp: cannot overwrite non-directory '
                                f'{_q(dst_disp)} with directory '
                                f'{_q(source.display)}')
                    continue
                self._walk_tree(src_abs, source.display, dst_abs, dst_disp,
                                st, st.st_dev, dirs, files, symlinks)
            elif is_symlink:
                # GNU cp copies a root symlink operand as a symlink (no -H/-L
                # ever reaches the engine; trailing slash was handled above).
                symlinks.append((src_abs, source.display, dst_abs,
                                 dst_disp, os.lstat(src_abs)))
            elif stat.S_ISREG(st.st_mode):
                files.append((src_abs, source.display, dst_abs, dst_disp, st))
            else:
                self._error(f'cp: cannot copy special file '
                            f'{_q(source.display)}')

        if self._interrupted():
            return self.result

        # Phase A: directories, parents before children (level-parallel).
        by_level = {}
        for task in dirs:
            by_level.setdefault(task[0], []).append(task)
        for level in sorted(by_level):
            if self._interrupted():
                break
            self._run_level(by_level[level], self._make_dir)

        # Phase B: regular files (parallel).
        if files and not self._interrupted():
            self._run_level(files, self._copy_one_file)

        # Phase C: hardlink second-occurrences + symlinks (targets exist now).
        if not self._interrupted():
            link_tasks = []
            for (src_abs, src_disp, dst_abs, dst_disp, st) in files:
                if st.st_nlink > 1 and plan.preserve_links:
                    link_tasks.append(
                        ((st.st_dev, st.st_ino), dst_abs, dst_disp))
            if link_tasks:
                self._run_level(link_tasks, self._make_link)
            if symlinks:
                self._run_level(symlinks, self._copy_one_symlink)

        # Phase D: directory metadata, deepest first (children settled).
        if plan.preserve and not self._interrupted():
            for level in sorted(by_level, reverse=True):
                if self._interrupted():
                    break
                self._run_level(by_level[level], self._apply_dir_metadata)

        return self.result


def default_workers():
    # The same dependency-free launch probe serves helpers and the app. This
    # value sizes idle reuse; accepted files do not wait behind its count.
    from runtime_guards import resolve_resource_budget
    return resolve_resource_budget('TOFU_AGENT_WORKERS', maximum=sys.maxsize)


def configured_workers():
    raw = os.environ.get('TOFU_CP_WORKERS', '').strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return default_workers()


def run_copy_plan(plan, cwd, *, workers=None, on_verbose=None, on_error=None,
                  stop_check=None, deadline=None):
    """Execute a parsed plan. Returns :class:`CopyResult`."""
    engine = _Engine(plan, cwd, workers=workers or configured_workers(),
                     on_verbose=on_verbose, on_error=on_error,
                     stop_check=stop_check, deadline=deadline)
    return engine.run()


# ── Shell-segment splice (compound commands) ───────────────────────────

def _argv_end_words(words, start, segment):
    from lib.project_mod.command_analysis import (
        _REDIR_BARE_RE, _REDIR_FUSED_RE)
    for idx in range(start, len(words)):
        word = words[idx]
        raw = segment[word.start:word.end]
        if not word.squote and not word.dquote and (
                _REDIR_FUSED_RE.match(raw) or _REDIR_BARE_RE.match(raw)):
            return idx
    return len(words)


_CP_BINARIES = frozenset({'cp', '/bin/cp', '/usr/bin/cp'})


def plan_cp_splice(command):
    """Rewrite standalone cp segments to the helper. Returns command or None.

    Only single-statement cp segments are eligible (no redirection inside
    the argv — a redirection truncating our argv view could smuggle
    operands past the planner). Pipelines/control flow around the segment
    stay byte-for-byte intact.
    """
    if os.name != 'posix' or not command or not command.strip():
        return None
    from lib.project_mod.command_analysis import (
        _shell_words, _split_pipeline_spans)
    replacements = []
    for seg_start, seg_end in _split_pipeline_spans(command):
        segment = command[seg_start:seg_end]
        words = _shell_words(segment)
        if not words or words[0].text not in _CP_BINARIES:
            continue
        argv_end = _argv_end_words(words, 0, segment)
        if argv_end != len(words):
            continue                       # redirection inside cp argv
        plan = parse_cp_argv([w.text for w in words[1:]])
        if plan is None:
            continue
        helper = os.path.abspath(__file__)
        parts = [shlex.quote(os.path.abspath(sys.executable)),
                 shlex.quote(helper),
                 '--workers', str(configured_workers()), '--']
        parts.extend(shlex.quote(w.text) for w in words[1:])
        replacements.append((seg_start + words[0].start,
                             seg_start + words[-1].end,
                             ' '.join(parts)))
    if not replacements:
        return None
    rewritten = command
    for start, end, replacement in sorted(replacements, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return rewritten


# ── Helper entry (splice target) ───────────────────────────────────────

def helper_main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    workers = configured_workers()
    while args and args[0].startswith('--') and args[0] != '--':
        if args[0] == '--workers' and len(args) > 1:
            try:
                workers = max(1, min(64, int(args[1])))
            except ValueError:
                pass
            args = args[2:]
        else:
            args = args[1:]
    if args and args[0] == '--':
        args = args[1:]
    plan = parse_cp_argv(args)
    if plan is None:
        # The planner upstream gates every splice; reaching this means a
        # hand-crafted invocation. Re-exec the real cp for exact semantics.
        try:
            os.execvp('cp', ['cp'] + args)
        except OSError:
            return 127
    out = _fd_line_sink(sys.stdout.fileno())
    err = _fd_line_sink(sys.stderr.fileno())
    result = run_copy_plan(
        plan, os.getcwd(), workers=workers,
        on_verbose=out, on_error=err)
    return result.exit_code


def _fd_line_sink(fd):
    def write(text):
        data = (text + '\n').encode('utf-8', errors='replace')
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            except OSError:
                return
            view = view[written:]
    return write


if __name__ == '__main__':  # pragma: no cover - exercised via run_command
    raise SystemExit(helper_main())
