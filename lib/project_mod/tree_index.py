"""Persistent per-project file index ("locate DB") for grep_search / find_files.

Why this exists
───────────────
On FUSE/network filesystems (DolphinFS, cross-DC mounts) the dominant cost of
``grep_search`` / ``find_files`` is NOT reading file contents — it is the
directory walk itself: every query re-issues tens of thousands of
readdir/stat RPCs before ripgrep/fd can even start.  On large or cold trees
that walk alone regularly exceeds the 60s tool timeout.

This module builds the walk ONCE, in the background, and keeps the result
both in memory (LRU) and on disk (server-local sessions dir, NOT on the FUSE
mount).  Query-time cost then becomes:

* find_files  → pure in-memory glob over the cached relpath list (µs–ms).
* grep_search → the index yields the exact candidate file list (ignore rules
  and include-globs applied in memory); ripgrep is then invoked on EXPLICIT
  file paths, so it performs zero directory traversal and immediately
  parallelizes content reads.

Freshness model
───────────────
* Writes made through the project's own write tools update the index
  synchronously via :func:`note_write` (hooked from the write-freshness
  choke point), so agent-authored changes are visible immediately.
* Anything else (git checkout, external editors) is picked up by a
  stale-while-revalidate refresh: entries older than ``STALE_AFTER_S`` are
  served instantly while a background rebuild is kicked; entries older than
  ``MAX_AGE_S`` are no longer trusted and the caller falls back to the live
  subprocess walk (rg/fd), exactly as before this module existed.
* A ``.gitignore`` write invalidates the index outright (ignore rules are
  baked into the candidate list).

Ignore-rule parity with a raw rg/fd walk:
* root ``.gitignore`` AND ``.git/info/exclude`` at the root scope, nested
  ``.gitignore`` files scoped to their subtree, contents-level rules
  (``/scripts/*``) that must not prune their own directory, and negation
  whitelists — including whitelisted hidden entries (``!.env.example``),
  which rg resurrects past its hidden-filter exactly as we do.
* One DELIBERATE superset: inside a git repo, behavior is identical; in a
  NON-git tree, rg≥13 ignores every .gitignore it finds (``--require-git``
  default) while this index still honours them — the user wrote those rules,
  and honouring them only ever REMOVES noise (generated/bulk files), never
  adds it.  Divergence is exclusion-only.
* Approximation (documented, bounded): negation is "any whitelist match
  un-ignores" rather than git's strict last-match-wins ordering across
  overlapping patterns/scopes.  The live subprocess fallback keeps full
  semantics for everything the index declines to serve.
* Hidden entries, symlinks, and IGNORE_DIRS are skipped (rg/fd defaults).

Everything here is best-effort: no public function raises, and every failure
mode degrades to the pre-index code path (live subprocess walk).
"""

from __future__ import annotations

import array
import bisect
import fnmatch
import hashlib
import os
import re
import struct
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from lib.log import get_logger
from lib.project_mod.config import IGNORE_DIRS, SESSIONS_DIR
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════
#  Tunables (env-overridable)
# ═══════════════════════════════════════════════════════

def _env_float(name, default, minimum=0.0, maximum=86_400.0):
    try:
        v = float(os.environ.get(name, '') or default)
        return max(minimum, min(maximum, v))
    except (TypeError, ValueError):
        return default


def _disabled():
    return os.environ.get('TOFU_TREE_INDEX_DISABLE', '').strip() == '1'


# Entries older than this are served while a background refresh is kicked.
def _stale_after_s():
    return _env_float('TOFU_TREE_INDEX_STALE_S', 45.0)


# Entries older than this are NOT served; caller falls back to the live walk.
def _max_age_s():
    return _env_float('TOFU_TREE_INDEX_MAX_AGE_S', 900.0)


def _max_entries():
    return resolve_resource_budget(
        'TOFU_TREE_INDEX_MAX_ENTRIES', minimum=10_000, maximum=600_000)


def _walk_jobs():
    return resolve_resource_budget(
        'TOFU_TREE_INDEX_WALK_JOBS', minimum=2, maximum=16)


def _build_budget_s():
    return _env_float('TOFU_TREE_INDEX_BUILD_BUDGET_S', 600.0)


def _mem_roots():
    return resolve_resource_budget(
        'TOFU_TREE_INDEX_MEM_ROOTS', minimum=1, maximum=8)


_DEPTH_CAP = 30          # parity with read_tools._TOOL_MAX_DEPTH
_DISK_MAGIC = b'TFIX001\n'


# ═══════════════════════════════════════════════════════
#  .gitignore → compiled regexes (root + .git/info/exclude + nested files)
# ═══════════════════════════════════════════════════════
#
# Ignore semantics replicated for rg/fd parity:
#   * root .gitignore AND .git/info/exclude (same syntax) at the root scope;
#   * nested .gitignore files apply to their own subtree (detected for free
#     from the parent's scandir listing during the walk);
#   * a pattern containing '/' is anchored at its scope's root, otherwise it
#     matches a basename at any depth below the scope;
#   * contents-level rules like ``/scripts/*`` must NOT prune ``scripts/``
#     itself — negations such as ``!/scripts/keep.py`` re-include files
#     underneath, which git can only reach by descending;
#   * dir-only rules (``logs/``) apply to directories; a whitelisted hidden
#     entry (``!.env.example``) is INCLUDED, exactly like rg's walk.
#
# Approximations (documented, bounded): negation is "any whitelist match
# un-ignores" rather than git's strict last-match-wins ordering, and deeper
# scopes do not re-order against shallower ones.

def _translate_gitignore_glob(pat):
    """Translate ONE gitignore glob to a regex source string (sans anchors).

    Returns ``(regex_src, dir_only)``.  Anchoring follows gitignore rules:
    a pattern containing '/' is anchored at the scope root; otherwise it
    matches a basename at any depth.
    """
    dir_only = pat.endswith('/')
    if dir_only:
        pat = pat.rstrip('/')
    anchored = pat.startswith('/') or ('/' in pat)
    pat = pat.lstrip('/')
    if not pat:
        return '', False
    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == '*':
            if pat[i:i + 2] == '**':
                # '**' crosses directory boundaries.
                out.append('.*')
                i += 2
                continue
            out.append('[^/]*')
        elif c == '?':
            out.append('[^/]')
        elif c == '[':
            j = pat.find(']', i + 1)
            if j < 0:
                out.append('\\[')
            else:
                out.append(pat[i:j + 1])
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    body = ''.join(out)
    if anchored:
        return f'^{body}(?:/.*)?$', dir_only
    # Unanchored: match any single path component (basename semantics), and
    # for dir patterns everything beneath such a component.
    return f'(?:^|/){body}(?:$|/)', dir_only


def _compile_gi_lines(lines):
    """Compile gitignore pattern lines into ``(ig_any, ig_dir, un_any, un_dir)``.

    ``*_any`` patterns match files and dirs; ``*_dir`` patterns (trailing
    slash) match dirs only.  Any element may be ``None``.
    """
    ig_any, ig_dir, un_any, un_dir = [], [], [], []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        negated = line.startswith('!')
        if negated:
            line = line[1:]
        src, dir_only = _translate_gitignore_glob(line)
        if not src:
            continue
        bucket = (un_dir if dir_only else un_any) if negated else (ig_dir if dir_only else ig_any)
        bucket.append(src)

    def _c(srcs):
        if not srcs:
            return None
        try:
            return re.compile('|'.join(f'(?:{s})' for s in srcs))
        except re.error as e:
            logger.debug('[TreeIndex] gitignore regex compile failed: %s', e)
            return None

    return _c(ig_any), _c(ig_dir), _c(un_any), _c(un_dir)


def _read_gi_lines(path):
    try:
        if not os.path.isfile(path):
            return []
        with open(path, errors='replace') as f:
            return f.read().splitlines()
    except OSError as e:
        logger.debug('[TreeIndex] gitignore read failed for %s: %s', path, e)
        return []


def _compile_gi_file(path):
    """Compile one gitignore-format file into a 4-regex tuple."""
    return _compile_gi_lines(_read_gi_lines(path))


def _compile_gitignore(base):
    """Root-scope ignore rules: ``<base>/.gitignore`` + ``.git/info/exclude``.

    Returns ``(ig_any, ig_dir, un_any, un_dir)``.  ``.git/info/exclude`` uses
    gitignore syntax and is honoured by rg/fd exactly like .gitignore — this
    is where local-only excludes (e.g. scratch entry points) live.
    """
    lines = _read_gi_lines(os.path.join(base, '.gitignore'))
    lines += _read_gi_lines(os.path.join(base, '.git', 'info', 'exclude'))
    return _compile_gi_lines(lines)


def _gi_hit(rel, is_dir, rules):
    """Test one 4-regex rule set against *rel* (bare, '/'-joined)."""
    ig_any, ig_dir, un_any, un_dir = rules
    ignored = bool(ig_any and ig_any.search(rel))
    if is_dir and not ignored:
        ignored = bool(ig_dir and ig_dir.search(rel))
    if not ignored:
        return False
    if un_any and un_any.search(rel):
        return False
    if is_dir and un_dir and un_dir.search(rel):
        return False
    return True


def _ignored_by_gi(rel, is_dir, contexts):
    """Approximate gitignore test across scoped rule contexts.

    ``contexts`` is a tuple of ``(scope, rules)`` where *scope* is '' for the
    root or a dir relpath for a nested .gitignore.  Rules apply only to paths
    at or below their scope (tested against the scope-stripped relpath).
    """
    for scope, rules in contexts:
        if not rules or not any(rules):
            continue
        if scope:
            if rel == scope:
                sub = None  # the scoping dir itself is judged by its PARENT
            elif rel.startswith(scope + '/'):
                sub = rel[len(scope) + 1:]
            else:
                continue
            if sub is None:
                continue
        else:
            sub = rel
        if _gi_hit(sub, is_dir, rules):
            return True
    return False


def _whitelisted(rel, contexts):
    """True when a negation in any scope explicitly whitelists *rel*.

    Used to resurrect hidden entries (``.env.example``) the way rg does.
    """
    for scope, rules in contexts:
        if not rules or not any(rules):
            continue
        _ig_any, _ig_dir, un_any, un_dir = rules
        if un_any is None and un_dir is None:
            continue
        if scope:
            if not rel.startswith(scope + '/'):
                continue
            sub = rel[len(scope) + 1:]
        else:
            sub = rel
        if (un_any and un_any.search(sub)) or (un_dir and un_dir.search(sub)):
            return True
    return False


# ═══════════════════════════════════════════════════════
#  In-memory index entry
# ═══════════════════════════════════════════════════════

class TreeIndex:
    """Immutable snapshot of one project's file list.

    ``paths`` is a SORTED list of project-relative '/'-joined file paths;
    ``sizes`` / ``mtimes`` are parallel ``array('q')`` columns.  Kept as
    columnar arrays so a 500k-file index loads from disk in ~0.1s and stays
    compact in RAM.
    """

    __slots__ = ('root', 'paths', 'sizes', 'mtimes', 'built_at', 'complete',
                 'root_rules')

    def __init__(self, root, paths, sizes, mtimes, built_at, complete,
                 root_rules=None):
        self.root = root
        self.paths = paths
        self.sizes = sizes
        self.mtimes = mtimes
        self.built_at = built_at
        self.complete = complete
        # Root-scope ignore rules (4-regex tuple) — retained so the write
        # hook can refuse to admit files that the index policy excludes.
        self.root_rules = root_rules or (None, None, None, None)

    def age(self):
        return time.time() - self.built_at

    def __len__(self):
        return len(self.paths)


# root → TreeIndex (LRU); root → build-in-progress flag.
_lock = threading.RLock()
_mem = {}            # OrderedDict semantics via move_to_end on hit
_building = set()

# Small global pools so concurrent warm() calls (multi-root workspaces) cannot
# stampede the filesystem. All builds share one scan-worker budget rather than
# multiplying it once per root.
_builder = None
_scanner = None


def _new_builder():
    return ThreadPoolExecutor(max_workers=2, thread_name_prefix='tree-index')


def _new_scanner():
    return ThreadPoolExecutor(
        max_workers=_walk_jobs(), thread_name_prefix='tree-index-scan')


def background_builder_snapshot():
    """Return the bounded builder's current resource state."""
    with _lock:
        builder = _builder
        scanner = _scanner
        builder_threads = tuple(getattr(builder, '_threads', ()))
        scanner_threads = tuple(getattr(scanner, '_threads', ()))
        return {
            'activeBuilds': len(_building),
            'executorActive': builder is not None,
            'residentThreads': sum(
                thread.is_alive() for thread in builder_threads),
            'scanExecutorActive': scanner is not None,
            'scanResidentThreads': sum(
                thread.is_alive() for thread in scanner_threads),
            'scanWorkerCapacity': int(
                getattr(scanner, '_max_workers', _walk_jobs())),
            'retainedRoots': len(_mem),
            'retainedEntries': sum(len(entry.paths) for entry in _mem.values()),
            'rootCapacity': _mem_roots(),
            'entryCapacity': _max_entries(),
        }


# ═══════════════════════════════════════════════════════
#  Background tree walk
# ═══════════════════════════════════════════════════════

def _scan_one_dir(abs_dir, rel_dir, depth, out, contexts):
    """Scandir ONE directory; append admissible files to *out*; RETURN child dirs.

    Runs inside the walk pool.  ``out`` appends ``(rel, size, mtime)`` tuples;
    DirEntry caches the stat from readdir where the FS supports readdirplus,
    so this stays at ~1-2 RPCs per directory on FUSE.  Returns a list of
    ``(abs_dir, rel_dir, depth, child_contexts)`` tuples for the BFS.

    A nested ``.gitignore`` in this directory extends ``contexts`` for its
    own subtree — detected from the scandir listing itself, so it costs no
    extra filesystem RPC.
    """
    subdirs = []
    has_nested_gi = False
    rows = []
    try:
        with os.scandir(abs_dir) as it:
            for entry in it:
                name = entry.name
                if name == '.gitignore':
                    has_nested_gi = True  # hidden itself — never indexed
                    continue
                rows.append(entry)
    except OSError as e:
        logger.debug('[TreeIndex] scandir failed for %s: %s', abs_dir, e)
        return subdirs

    if has_nested_gi and rel_dir:  # root scope is compiled separately already
        nested = _compile_gi_file(os.path.join(abs_dir, '.gitignore'))
        if any(nested):
            contexts = contexts + ((rel_dir, nested),)

    for entry in rows:
        name = entry.name
        rel_e = (rel_dir + '/' + name) if rel_dir else name
        if name.startswith('.'):
            # rg parity: hidden entries are skipped UNLESS a gitignore
            # negation explicitly whitelists them (e.g. ``!.env.example``).
            if not _whitelisted(rel_e, contexts):
                continue
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if name in IGNORE_DIRS:
                    continue
                if _ignored_by_gi(rel_e, True, contexts):
                    continue
                subdirs.append((os.path.join(abs_dir, name), rel_e, depth + 1, contexts))
            elif entry.is_file(follow_symlinks=False):
                if _ignored_by_gi(rel_e, False, contexts):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    out.append((rel_e, int(st.st_size), int(st.st_mtime)))
                except OSError:
                    out.append((rel_e, 0, 0))
        except OSError:
            continue
    return subdirs


def _walk_tree(root, deadline, *, scan_executor=None):
    """Parallel BFS walk of *root*.  Returns ``(rows, complete)``.

    rows: list of ``(rel, size, mtime)``.  ``complete`` is False when the walk
    was cut short by the entry cap or the time budget (such an index is NOT
    used for queries — a partial candidate list would silently drop grep
    hits, which is worse than falling back to the live walk).
    """
    root_rules = _compile_gitignore(root)
    contexts = (('', root_rules),) if any(root_rules) else ()
    out = []
    max_entries = _max_entries()
    truncated = False

    owns_executor = scan_executor is None
    pool = scan_executor or _new_scanner()
    pending = set()
    try:
        pending = {pool.submit(_scan_one_dir, root, '', 0, out, contexts)}
        while pending and not truncated:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    subdirs = fut.result() or []
                except Exception as e:
                    logger.debug('[TreeIndex] walk task failed: %s', e)
                    continue
                for abs_d, rel_d, depth, ctx in subdirs:
                    if depth > _DEPTH_CAP:
                        continue
                    pending.add(pool.submit(_scan_one_dir, abs_d, rel_d, depth, out, ctx))
            if len(out) >= max_entries or time.time() > deadline:
                truncated = True
                for f in pending:
                    f.cancel()
    finally:
        # Shared scan workers can outlive this build. Drain only this walk's
        # accepted futures before returning so none can mutate ``out`` after
        # rows are sorted or discarded.
        for future in pending:
            future.cancel()
        if pending:
            wait(pending)
        if owns_executor:
            pool.shutdown(wait=True, cancel_futures=True)
    return out, not truncated


def _build_sync(root, *, scan_executor=None):
    """Build (or rebuild) the index for *root*; swap into memory; persist.

    Runs on the builder pool.  Never raises.
    """
    deadline = time.time() + _build_budget_s()
    t0 = time.perf_counter()
    try:
        result = _walk_tree(root, deadline, scan_executor=scan_executor)
        if not result:
            return
        rows, complete = result
        if not complete:
            logger.warning('[TreeIndex] walk of %s hit caps (entries=%d, budget=%ds) '
                           '— index NOT installed; queries fall back to live walk',
                           root, len(rows), int(_build_budget_s()))
            return
        rows.sort(key=lambda r: r[0])
        paths = [r[0] for r in rows]
        sizes = array.array('q', (r[1] for r in rows))
        mtimes = array.array('q', (r[2] for r in rows))
        entry = TreeIndex(root, paths, sizes, mtimes, time.time(), True,
                          _compile_gitignore(root))
        with _lock:
            _mem[root] = entry
            _mem_move_to_end(root)
            _evict_mem_over_cap()
        _persist(entry)
        elapsed = time.perf_counter() - t0
        logger.info('[TreeIndex] built %s: %d files in %.1fs', root, len(paths), elapsed)
    except Exception as e:
        logger.warning('[TreeIndex] build failed for %s: %s', root, e, exc_info=True)
    finally:
        with _lock:
            _building.discard(root)


def _finish_builder_job(root, builder, scanner):
    """Release one root lease and retire its exact idle pool generation."""
    global _builder, _scanner
    builder_to_shutdown = None
    scanner_to_shutdown = None
    with _lock:
        _building.discard(root)
        if not _building and builder is not None and _builder is builder:
            _builder = None
            builder_to_shutdown = builder
        if not _building and scanner is not None and _scanner is scanner:
            _scanner = None
            scanner_to_shutdown = scanner
    if scanner_to_shutdown is not None:
        scanner_to_shutdown.shutdown(wait=False, cancel_futures=False)
    if builder_to_shutdown is not None:
        # This normally runs on the final builder worker. Waiting here would
        # attempt to join the current thread; non-waiting shutdown lets it
        # return naturally after the accepted batch has completed.
        builder_to_shutdown.shutdown(wait=False, cancel_futures=False)


def _run_builder_job(root, builder, scanner):
    try:
        _build_sync(root, scan_executor=scanner)
    finally:
        # _build_sync also clears the root lease. Keep this idempotent wrapper
        # as the lifecycle authority so injected/test builders cannot strand
        # the executor generation when their build function returns early.
        _finish_builder_job(root, builder, scanner)


def _mem_move_to_end(root):
    # Plain dicts preserve insertion order; emulate LRU touch.
    entry = _mem.pop(root, None)
    if entry is not None:
        _mem[root] = entry


def _evict_mem_over_cap():
    root_capacity = _mem_roots()
    entry_capacity = _max_entries()
    retained_entries = sum(len(entry.paths) for entry in _mem.values())
    while (_mem and (
            len(_mem) > root_capacity or retained_entries > entry_capacity)):
        victim = next(iter(_mem))
        entry = _mem.pop(victim, None)
        retained_entries -= len(entry.paths) if entry is not None else 0
        logger.debug(
            '[TreeIndex] LRU-evicted in-memory index for %s '
            '(roots=%d/%d entries=%d/%d)',
            victim, len(_mem), root_capacity,
            retained_entries, entry_capacity,
        )


def warm(root):
    """Kick a background build/refresh for *root* when stale.  Non-blocking.

    Called from project registration (set_project / ensure_project_state) and
    lazily from the read tools, so the index usually exists by the time a
    grep/find query needs it.  The common fresh-hit path is a pure dict
    lookup — no filesystem RPC at all.
    """
    global _builder, _scanner
    if _disabled() or not root:
        return
    builder = None
    scanner = None
    try:
        root = os.path.abspath(root)
        with _lock:
            if root in _building:
                return
            entry = _mem.get(root)
            if entry is not None and entry.complete and entry.age() <= _stale_after_s():
                return  # fresh enough — nothing to do
            _building.add(root)
        try:
            if os.path.isdir(root):
                with _lock:
                    builder = _builder
                    scanner = _scanner
                    if builder is None:
                        builder = _new_builder()
                        _builder = builder
                    if scanner is None:
                        scanner = _new_scanner()
                        _scanner = scanner
                    builder.submit(
                        _run_builder_job, root, builder, scanner)
            else:
                with _lock:
                    _building.discard(root)
        except Exception:
            _finish_builder_job(root, builder, scanner)
            raise
    except Exception as e:
        logger.debug('[TreeIndex] warm(%s) skipped: %s', root, e)


def invalidate(root):
    """Drop memory + disk state for *root* (e.g. .gitignore changed)."""
    if not root:
        return
    root = os.path.abspath(root)
    with _lock:
        _mem.pop(root, None)
    _drop_persisted_snapshot(root)


def _drop_persisted_snapshot(root):
    """Remove one reconstructible disk snapshot without touching live memory."""
    try:
        os.unlink(_disk_path(root))
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug('[TreeIndex] disk invalidate failed for %s: %s', root, e)


# ═══════════════════════════════════════════════════════
#  Disk persistence (server-local sessions dir — never on the FUSE mount)
# ═══════════════════════════════════════════════════════

def _index_dir():
    path = os.path.join(SESSIONS_DIR, 'project_indexes')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.debug('[TreeIndex] cannot create index dir %s: %s', path, e)
    return path


def _disk_path(root):
    digest = hashlib.sha1(root.encode('utf-8', 'surrogateescape')).hexdigest()[:20]
    return os.path.join(_index_dir(), f'{digest}.tfix')


def _persist(entry):
    """Write the index atomically.  Little-endian columnar blob:

    magic(8) built_at(d) count(I) root_len(H) root sizes(count×8) mtimes(count×8) paths
    """
    try:
        paths_blob = '\n'.join(entry.paths).encode('utf-8', 'surrogateescape')
        root_b = entry.root.encode('utf-8', 'surrogateescape')
        header = struct.pack('<8sdIH', _DISK_MAGIC, entry.built_at, len(entry.paths), len(root_b))
        blob = (header + root_b + entry.sizes.tobytes() + entry.mtimes.tobytes()
                + struct.pack('<I', len(paths_blob)) + paths_blob)
        final = _disk_path(entry.root)
        tmp = final + f'.tmp.{os.getpid()}'
        with open(tmp, 'wb') as f:
            f.write(blob)
        os.replace(tmp, final)
    except Exception as e:
        logger.debug('[TreeIndex] persist failed for %s: %s', entry.root, e)


def _load_disk(root):
    """Load a persisted index for *root* (root-string validated)."""
    try:
        path = _disk_path(root)
        with open(path, 'rb') as f:
            header_size = struct.calcsize('<8sdIH')
            header = f.read(header_size)
            magic, built_at, count, root_len = struct.unpack(
                '<8sdIH', header)
            if magic != _DISK_MAGIC:
                return None
            entry_capacity = _max_entries()
            if count > entry_capacity:
                logger.debug(
                    '[TreeIndex] persisted index exceeds current entry budget '
                    'for %s (%d/%d)', root, count, entry_capacity)
                return None
            blob = header + f.read()
        off = struct.calcsize('<8sdIH')
        disk_root = blob[off:off + root_len].decode('utf-8', 'surrogateescape')
        if disk_root != root:
            return None
        off += root_len
        nbytes = count * 8
        sizes = array.array('q')
        sizes.frombytes(blob[off:off + nbytes])
        off += nbytes
        mtimes = array.array('q')
        mtimes.frombytes(blob[off:off + nbytes])
        off += nbytes
        (paths_len,) = struct.unpack_from('<I', blob, off)
        off += 4
        paths = blob[off:off + paths_len].decode('utf-8', 'surrogateescape').split('\n')
        if count and (not paths or paths[0] == '' and count > 1):
            return None
        return TreeIndex(root, paths, sizes, mtimes, built_at, True,
                         _compile_gitignore(root))
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug('[TreeIndex] disk load failed for %s: %s', root, e)
        return None


# ═══════════════════════════════════════════════════════
#  Query entry point
# ═══════════════════════════════════════════════════════

def acquire(root):
    """Return a usable :class:`TreeIndex` for *root*, or ``None``.

    ``None`` means "no trustworthy index right now" — the caller MUST fall
    back to the live subprocess walk.  A background build/refresh is kicked
    whenever this returns None or a stale entry, so subsequent queries get
    faster without any synchronous waiting here (except a one-shot disk
    load, ~0.1s, which beats a 60s walk every time).
    """
    if _disabled() or not root:
        return None
    try:
        root = os.path.abspath(root)
        with _lock:
            entry = _mem.get(root)
            if entry is not None:
                _mem_move_to_end(root)
        if entry is None:
            entry = _load_disk(root)
            if entry is not None:
                with _lock:
                    _mem[root] = entry
                    _mem_move_to_end(root)
                    _evict_mem_over_cap()
        if entry is None or not entry.complete:
            warm(root)
            return None
        age = entry.age()
        if age > _max_age_s():
            warm(root)
            return None
        if age > _stale_after_s():
            warm(root)  # stale-while-revalidate: serve now, refresh behind us
        return entry
    except Exception as e:
        logger.debug('[TreeIndex] acquire(%s) failed: %s', root, e)
        return None


# ═══════════════════════════════════════════════════════
#  Write-tool hook
# ═══════════════════════════════════════════════════════

def _rel_admissible(entry, rel):
    """Would the index policy (hidden / IGNORE_DIRS / ignore rules) admit *rel*?

    The write hook must not insert files a rebuild would exclude (a write
    into ``node_modules/`` or ``.git/`` must stay invisible).  Nested-scope
    rules are not re-evaluated here — the next background refresh reconciles
    those; root-scope rules cover the common cases.
    """
    parts = rel.split('/')
    for i, part in enumerate(parts):
        if part in IGNORE_DIRS:
            return False
        if part.startswith('.'):
            # Hidden entries survive only via an explicit whitelist negation.
            if not _whitelisted(rel if i == len(parts) - 1 else '/'.join(parts[:i + 1]),
                                (('', entry.root_rules),)):
                return False
    rules = entry.root_rules
    if any(rules):
        if _gi_hit(rel, False, rules):
            return False
        # Ancestor dir-only rules (e.g. 'logs/') exclude everything below.
        for i in range(1, len(parts)):
            if _gi_hit('/'.join(parts[:i]), True, rules):
                return False
    return True


def note_write(abs_path):
    """Upsert *abs_path* into any in-memory index that contains its root.

    Hooked from the write-freshness choke point (every project write tool
    calls it), so agent-authored changes are reflected in the next query
    without waiting for the background refresh.  A vanished file (delete via
    run_command raced us) is dropped instead.  Ignore-rule sources
    (``.gitignore`` at any depth, ``.git/info/exclude``) drop the whole index
    — their semantics are baked into the candidate list.
    """
    if _disabled() or not abs_path:
        return
    try:
        ap = os.path.abspath(abs_path)
    except OSError:
        return
    with _lock:
        snapshot = list(_mem.values())
    for entry in snapshot:
        root = entry.root
        if ap != root and not ap.startswith(root + os.sep):
            continue
        rel = os.path.relpath(ap, root).replace(os.sep, '/')
        if rel == '.gitignore' or rel.endswith('/.gitignore') \
                or rel == '.git/info/exclude':
            logger.info('[TreeIndex] ignore rules written under %s (%s) — invalidating index',
                        root, rel)
            invalidate(root)
            return
        if not _rel_admissible(entry, rel):
            continue  # not indexable (hidden/ignored) — nothing to record
        try:
            st = os.stat(ap)
            size, mtime = int(st.st_size), int(st.st_mtime)
            remove = False
        except OSError:
            size = mtime = 0
            remove = True
        persisted_snapshot_is_stale = False
        with _lock:
            live = _mem.get(root)
            if live is not entry:
                continue  # entry was evicted/replaced meanwhile
            idx = bisect.bisect_left(entry.paths, rel)
            found = idx < len(entry.paths) and entry.paths[idx] == rel
            if remove:
                if found:
                    del entry.paths[idx]
                    del entry.sizes[idx]
                    del entry.mtimes[idx]
                    persisted_snapshot_is_stale = True
            elif found:
                entry.sizes[idx] = size
                entry.mtimes[idx] = mtime
                persisted_snapshot_is_stale = True
            else:
                entry.paths.insert(idx, rel)
                entry.sizes.insert(idx, size)
                entry.mtimes.insert(idx, mtime)
                persisted_snapshot_is_stale = True
            if persisted_snapshot_is_stale:
                # The local blob predates this mutation. Remove it while the
                # memory authority is still locked, before cap enforcement can
                # evict this root and make a stale disk load eligible again.
                _drop_persisted_snapshot(root)
                _mem_move_to_end(root)
                _evict_mem_over_cap()


# ═══════════════════════════════════════════════════════
#  Query helpers
# ═══════════════════════════════════════════════════════

_GLOB_CHARS = re.compile(r'[*?\[]')


def _prefix_bounds(entry, rel_prefix):
    """Return the [lo, hi) slice of the sorted path list under *rel_prefix*."""
    if not rel_prefix or rel_prefix == '.':
        return 0, len(entry.paths)
    prefix = rel_prefix.rstrip('/') + '/'
    paths = entry.paths
    lo = bisect.bisect_left(paths, prefix)
    hi = bisect.bisect_left(paths, prefix[:-1] + '0')  # '0' == '/' + 1
    return lo, hi


def find_matching(entry, rel_prefix, pattern, cap, *, case_sensitive=False):
    """In-memory find_files: ``[(rel, size), ...]`` capped at *cap*.

    Glob semantics mirror the Python fallback walker: case-insensitive by
    default, or case-sensitive for a translated GNU ``find -name``. Patterns
    match the basename unless they contain '/', in which case they match the
    project-relative path.
    """
    lo, hi = _prefix_bounds(entry, rel_prefix)
    paths, sizes = entry.paths, entry.sizes
    pat = pattern if case_sensitive else pattern.lower()
    on_relpath = '/' in pattern
    # Fast path: the overwhelmingly common '*.ext' shape.
    simple_suffix = None
    if not on_relpath and pat.startswith('*.') and not _GLOB_CHARS.search(pat[1:]):
        simple_suffix = pat[1:]
    out = []
    for i in range(lo, hi):
        p = paths[i]
        comparable = p if case_sensitive else p.lower()
        if simple_suffix is not None:
            ok = comparable.endswith(simple_suffix)
        elif on_relpath:
            ok = fnmatch.fnmatchcase(comparable, pat)
        else:
            basename = comparable.rsplit('/', 1)[-1]
            ok = fnmatch.fnmatchcase(basename, pat)
        if ok:
            out.append((p, sizes[i]))
            if len(out) >= cap:
                break
    return out


def grep_candidates(entry, rel_prefix, include, max_bytes):
    """In-memory candidate list for grep: ``(abs_paths, total_bytes)``.

    Applies the include glob (rg -g semantics: basename match unless the glob
    contains '/') and the recorded size cap (parity with rg --max-filesize).
    ``total_bytes`` is the summed candidate size from the snapshot — the
    chunk runner uses it to tell content-heavy searches (parallelize reads
    across processes) from metadata-bound ones.
    """
    lo, hi = _prefix_bounds(entry, rel_prefix)
    paths, sizes = entry.paths, entry.sizes
    root = entry.root
    inc_rel = include and '/' in include
    inc_re = None
    if include:
        inc_re = re.compile(fnmatch.translate(include), re.IGNORECASE)
    out = []
    total = 0
    for i in range(lo, hi):
        sz = sizes[i]
        if sz > max_bytes:
            continue
        p = paths[i]
        if inc_re is not None:
            target = p if inc_rel else p.rsplit('/', 1)[-1]
            if not inc_re.match(target):
                continue
        out.append(root + '/' + p)
        total += sz
    return out, total
