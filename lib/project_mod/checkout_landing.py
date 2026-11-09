"""Publish a commit on a shared checkout's branch, carrying uncommitted edits forward.

Entry points: ``plan`` compares every path a commit changes with the checkout's
current bytes and chooses each path's action; ``publish`` rechecks that plan
under Git's own ``index.lock``, replaces files atomically, moves only those
paths' index entries and then the branch by compare-and-swap (index before ref,
as Git does: an interruption leaves the commit staged, never a HEAD whose index
reverts it); ``revalidate`` is the same recheck without writing.

An unmodified file takes the committed content. A file with uncommitted
concurrent edits gets a ``git merge-file`` three-way merge, so those edits stay
in place, still uncommitted, on top of the new tip. Known generated outputs are
rebuilt separately for the committed and working source views through
``lib.project_mod.generated_integration``. Overlapping lines, a staged change,
an untracked file in the way, a file-type clash or an operation in progress
raise ``Refused`` before anything is written; a branch or planned path that
moved raises ``Replan``; an existing ``index.lock`` raises ``IndexBusy``.

Owners: ``scripts/git_land.py`` lands worktree branches (notices and cleanup
stay there); ``task_delta_delivery`` lands a reviewed project
delivery as its own commit. Contract: docs/modules/project_integration.md,
"Landing worktree branches" and "Reviewed project deliveries become commits".
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

_MODES = frozenset({'100644', '100755', '120000'})
_IN_PROGRESS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge',
                'rebase-apply', 'BISECT_LOG')
_OVERLAP_ADVICE = (
    'Uncommitted edits in the checkout belong to another writer. Rerun after that '
    'work is committed, or bring the checkout\'s current version of each file into '
    'the worktree branch, resolve, commit there, and rerun.')


class Refused(Exception):
    """Landing would overwrite or lose work; nothing was changed."""

    def __init__(self, reason: str, paths=(), advice: str = '') -> None:
        super().__init__(reason)
        self.reason = reason
        self.paths = list(paths)
        self.advice = advice


class LandingError(RuntimeError):
    """Git or the filesystem failed unexpectedly."""


class Replan(Exception):
    """The branch or a planned path moved before publication."""


class IndexBusy(Exception):
    """Another Git process holds the checkout's ``index.lock``; nothing was changed."""

    def __init__(self, lock_path: Path) -> None:
        super().__init__(f'{lock_path} exists')
        self.lock_path = lock_path


@dataclasses.dataclass(frozen=True)
class _Entry:
    mode: str
    oid: str


@dataclasses.dataclass
class _Current:
    kind: str  # missing | file | link | dir | other | blocked
    fingerprint: tuple
    data: bytes = b''
    executable: bool = False
    blocker: str = ''


@dataclasses.dataclass
class _Step:
    path: str
    old: _Entry | None
    new: _Entry | None
    current: _Current
    action: str = 'keep'  # keep | write | delete
    data: bytes = b''
    executable: bool = False
    carried: bool = False


class _Plan(list):
    """Publication steps plus the complete generated-source read set."""

    def __init__(self, tree):
        super().__init__()
        self.tree = tree
        self.observed = {}
        self.roots = []
        self.untracked = set()


def _untracked(top, roots):
    return set(filter(None, git(top, 'ls-files', '--others', '--exclude-standard',
                                '-z', '--', *roots).stdout.split('\0')))


def _tree_files(top, revision, roots):
    """Read the declared generator closure in one blob batch, never the entire repo."""
    entries = {}
    for record in git(top, 'ls-tree', '-r', '-z', revision, '--', *roots).stdout.split('\0'):
        if not record:
            continue
        meta, path = record.split('\t', 1)
        mode, kind, oid = meta.split()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise Refused('generated inputs must be regular files', [path])
        entries[path] = _Entry(mode, oid)
    result = git(top, 'cat-file', '--batch', binary=True,
                  stdin=''.join(entry.oid + '\n' for entry in entries.values()).encode()).stdout
    files, offset = {}, 0
    for path in entries:
        end = result.index(b'\n', offset)
        size = int(result[offset:end].split()[2])
        files[path] = result[end + 1:end + 1 + size]
        offset = end + size + 2
    return entries, files


def _generated_tree(top, tree, outputs, old_outputs):
    """Only reproducible outputs enter this private commit index, never WIP sources."""
    with tempfile.TemporaryDirectory(prefix='git-land-index-') as directory:
        env = {'GIT_INDEX_FILE': str(Path(directory) / 'index')}
        git(top, 'read-tree', tree, env=env)
        records = []
        for path in sorted(set(old_outputs) | outputs.keys()):
            if path in outputs:
                oid = git(top, 'hash-object', '-w', '--stdin', binary=True,
                           stdin=outputs[path]).stdout.decode().strip()
                mode = old_outputs[path].mode if path in old_outputs else '100644'
                records.append(f'{mode} {oid}\t{path}\0')
            else:
                records.append(f'0 {"0" * len(tree)}\t{path}\0')
        if records:
            git(top, 'update-index', '-z', '--index-info', stdin=''.join(records), env=env)
        return git(top, 'write-tree', env=env).stdout.strip()


def _prepare_generated(top, head, tree, changes, plan):
    from lib.project_mod.generated_integration import (
        affected_families, build_outputs, family_roots, output_family)

    families = affected_families(path for path, _, _ in changes)
    if not families:
        return {}, {}, set()
    plan.roots = family_roots(families)
    before_entries, before = _tree_files(top, head, plan.roots)
    merged_entries, merged = _tree_files(top, tree, plan.roots)
    plan.untracked = _untracked(top, plan.roots)
    for path in before.keys() | merged.keys() | plan.untracked:
        current = _current(top, path)
        if current.kind not in ('file', 'missing'):
            raise Refused('generated inputs and outputs must be regular files', [path])
        plan.observed[path] = current
    clean_outputs = build_outputs(top, families, merged, committed=True)
    output_paths = {path for path in plan.observed if output_family(path) in families} | clean_outputs.keys()
    for path in output_paths - plan.observed.keys():
        plan.observed[path] = _current(top, path)
    plan.tree = _generated_tree(top, tree, clean_outputs,
                                {path: entry for path, entry in merged_entries.items()
                                 if output_family(path) in families})
    return before_entries, before, families


def _finish_generated(top, plan, before_entries, before, families):
    from lib.project_mod.generated_integration import build_outputs, output_family

    if not families:
        return
    current = {path: state.data for path, state in plan.observed.items() if state.kind == 'file'}
    combined = dict(current)
    for step in plan:
        if output_family(step.path) in families:
            continue
        if step.action == 'write' and step.path in plan.observed:
            combined[step.path] = step.data
        elif step.action == 'delete':
            combined.pop(step.path, None)
    # A generated edit may be replaced only after proving its source exists.
    dirty_outputs = {path for path in plan.observed
                     if output_family(path) in families
                     and current.get(path) != before.get(path)}
    if dirty_outputs:
        reproduced = build_outputs(top, families, current, committed=False)
        unexplained = sorted(path for path in dirty_outputs if current.get(path) != reproduced.get(path))
        if unexplained:
            raise Refused('uncommitted generated edits are not reproducible from their sources',
                          unexplained, 'Preserve those edits in their authored sources, regenerate, and rerun.')
    outputs = build_outputs(top, families, combined, committed=False)
    committed_entries, committed = _tree_files(top, plan.tree, plan.roots)
    steps = {step.path: step for step in plan}
    output_paths = {path for path in plan.observed if output_family(path) in families} | outputs.keys()
    for path in sorted(output_paths):
        if path not in plan.observed:
            plan.observed[path] = _current(top, path)
            if plan.observed[path].kind != 'missing':
                raise Replan()
        state = plan.observed[path]
        if state.kind not in ('file', 'missing'):
            raise Refused('generated output has an unsafe file type', [path])
        if path not in steps:
            if outputs.get(path) == current.get(path) and committed.get(path) == before.get(path):
                continue
            step = _Step(path, before_entries.get(path), committed_entries.get(path), state)
            plan.append(step)
        else:
            step = steps[path]
        data = outputs.get(path)
        base_executable = step.old is not None and step.old.mode == '100755'
        new_executable = step.new is not None and step.new.mode == '100755'
        step.executable = (state.executable if state.kind == 'file'
                           and state.executable != base_executable else new_executable)
        step.carried = data != committed.get(path) or step.executable != new_executable
        step.data = data or b''
        step.action = ('keep' if data == current.get(path) and step.executable == state.executable else
                       'delete' if data is None else 'write')


def git(cwd, *args, stdin=None, env=None, check=True, binary=False):
    # Routing/config injection never redirects a landing; identity stays the caller's.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith('GIT_')
                   or key.upper().startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_'))}
    environment.update(LC_ALL='C', GIT_OPTIONAL_LOCKS='0', GIT_LITERAL_PATHSPECS='1')
    environment.update(env or {})
    argv = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'gc.auto=0', '-c', 'maintenance.auto=false', *args]
    text = {} if binary else {'encoding': 'utf-8', 'errors': 'surrogateescape'}
    process = subprocess.run(argv, cwd=str(cwd), input=stdin, env=environment,
                             capture_output=True, **text)
    if check and process.returncode:
        detail = process.stderr if not binary else process.stderr.decode('utf-8', 'replace')
        raise LandingError(f'git {args[0]} failed: {detail.strip()[:800]}')
    return process


def rev(top: Path, revision: str) -> str:
    return git(top, 'rev-parse', '--verify', '--quiet', f'{revision}^{{commit}}',
                check=False).stdout.strip()


def git_paths(top: Path, *names: str) -> list[Path]:
    arguments = [part for name in names for part in ('--git-path', name)]
    return [top / line for line in git(top, 'rev-parse', *arguments).stdout.splitlines()]


def operations_in_progress(top: Path) -> list[str]:
    return [name for name, path in zip(_IN_PROGRESS, git_paths(top, *_IN_PROGRESS))
            if path.exists()]


def _changes(top: Path, head: str, tree: str) -> list[tuple[str, _Entry | None, _Entry | None]]:
    tokens = git(top, 'diff', '--raw', '-z', '--no-renames', '--no-abbrev',
                  '--no-ext-diff', head, tree).stdout.split('\0')
    changes = []
    for meta, path in zip(tokens[0::2], tokens[1::2]):
        old_mode, new_mode, old_oid, new_oid, _status = meta.lstrip(':').split(' ')
        changes.append((path,
                        None if old_mode == '000000' else _Entry(old_mode, old_oid),
                        None if new_mode == '000000' else _Entry(new_mode, new_oid)))
    return changes


def _stat_key(info: os.stat_result) -> tuple:
    return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_ino, info.st_dev)


def _current(top: Path, path: str, *, read: bool = True) -> _Current:
    parts = path.split('/')
    for depth in range(1, len(parts)):
        prefix = '/'.join(parts[:depth])
        try:
            info = os.lstat(top / prefix)
        except FileNotFoundError:
            return _Current('missing', ('missing', prefix))
        if not stat.S_ISDIR(info.st_mode):
            return _Current('blocked', ('blocked', prefix, _stat_key(info)), blocker=prefix)
    full = top / path
    try:
        info = os.lstat(full)
    except FileNotFoundError:
        return _Current('missing', ('missing', path))
    key = _stat_key(info)
    if stat.S_ISREG(info.st_mode):
        return _Current('file', key, full.read_bytes() if read else b'',
                        bool(info.st_mode & stat.S_IXUSR))
    if stat.S_ISLNK(info.st_mode):
        return _Current('link', key, os.fsencode(os.readlink(full)) if read else b'')
    return _Current('dir' if stat.S_ISDIR(info.st_mode) else 'other', key)


def _smudged(top: Path, path: str, entry: _Entry) -> bytes:
    # Batch --filters reports the unconverted size (Git 2.34), so read one by one.
    if entry.mode == '120000':
        return git(top, 'cat-file', 'blob', entry.oid, binary=True).stdout
    return git(top, 'cat-file', '--filters', f'--path={path}', entry.oid,
                binary=True).stdout


def _merge_file(top: Path, step: _Step) -> tuple[bytes | None, str]:
    inputs = (step.current.data, _smudged(top, step.path, step.old),
              _smudged(top, step.path, step.new))
    with tempfile.TemporaryDirectory(prefix='git-land-file-') as directory:
        names = []
        for name, data in zip(('checkout', 'base', 'landed'), inputs):
            (Path(directory) / name).write_bytes(data)
            names.append(str(Path(directory) / name))
        merged = git(top, 'merge-file', '-p', '-L', 'checkout', '-L', 'base',
                      '-L', 'landed', *names, check=False, binary=True)
    if merged.returncode == 0:
        return merged.stdout, ''
    if 0 < merged.returncode < 128:
        return None, 'the branch and uncommitted edits in the checkout change the same lines'
    return None, 'uncommitted edits in the checkout cannot be merged (binary content?)'


def plan(top: Path, head: str, tree: str, file_mode: bool) -> _Plan:
    from lib.project_mod.generated_integration import GeneratedOutputError

    try:
        return _plan_files(top, head, tree, file_mode)
    except GeneratedOutputError as error:
        raise Refused('generated outputs could not be rebuilt', advice=str(error)) from error


def _plan_files(top: Path, head: str, tree: str, file_mode: bool) -> _Plan:
    from lib.project_mod.generated_integration import output_family

    refusals: list[str] = []
    steps = _Plan(tree)
    changes = _changes(top, head, tree)
    before_entries, before, families = _prepare_generated(top, head, tree, changes, steps)
    for path, old, new in _changes(top, head, steps.tree):
        if '\n' in path or any(entry and entry.mode not in _MODES for entry in (old, new)):
            refusals.append(f'{path}: submodules and newline paths are not supported')
            continue
        steps.append(_Step(path, old, new, steps.observed.get(path) or _current(top, path)))
    files = [step for step in steps if step.current.kind == 'file']
    oids = git(top, 'hash-object', '--stdin-paths',
                stdin=''.join(step.path + '\n' for step in files)).stdout.split() if files else []
    if len(oids) != len(files):
        raise LandingError('git hash-object returned an incomplete answer')
    digest = hashlib.sha1 if len(head) == 40 else hashlib.sha256

    def entry_of(step: _Step, oid: str = '') -> _Entry | None:
        current = step.current
        if current.kind == 'link':
            return _Entry('120000', digest(b'blob %d\0' % len(current.data) + current.data).hexdigest())
        if current.kind != 'file':
            return None
        if file_mode:
            mode = '100755' if current.executable else '100644'
        else:
            mode = next((entry.mode for entry in (step.old, step.new)
                         if entry and entry.mode != '120000'), '100644')
        return _Entry(mode, oid)

    hashed = dict(zip((step.path for step in files), oids))
    deleted: set[str] = set()
    blocked: list[_Step] = []
    for step in steps:
        kind = step.current.kind
        if kind in ('dir', 'other'):
            refusals.append(f'{step.path}: a directory or special file is in the way')
            continue
        if kind == 'blocked':
            blocked.append(step)
            continue
        if output_family(step.path) in families:
            continue  # Sources determine these bytes in _finish_generated.
        refusal = _classify(top, step, entry_of(step, hashed.get(step.path, '')), file_mode)
        if refusal:
            refusals.append(f'{step.path}: {refusal}')
        elif step.action == 'delete':
            deleted.add(step.path)
    for step in blocked:
        # A non-directory parent is fine only if this landing deletes it first.
        if step.current.blocker not in deleted:
            refusals.append(f'{step.path}: {step.current.blocker} is in the way')
            continue
        refusal = _classify(top, step, None, file_mode)
        if refusal:
            refusals.append(f'{step.path}: {refusal}')
    if refusals:
        raise Refused('the checkout has uncommitted work this landing would overwrite',
                      refusals, _OVERLAP_ADVICE)
    _finish_generated(top, steps, before_entries, before, families)
    return steps


def _classify(top: Path, step: _Step, current: _Entry | None, file_mode: bool) -> str:
    """Choose the step's action; return a refusal reason or ''."""
    if current == step.new:
        return ''
    if current == step.old:
        if step.new is None:
            step.action = 'delete'
        else:
            step.action, step.executable = 'write', step.new.mode == '100755'
            step.data = _smudged(top, step.path, step.new)
        return ''
    regular = ('100644', '100755')
    if (step.old and step.new and current and step.old.mode in regular
            and step.new.mode in regular and current.mode in regular):
        merged, reason = _merge_file(top, step)
        if merged is None:
            return reason
        ours = current.mode == '100755'
        base, theirs = step.old.mode == '100755', step.new.mode == '100755'
        step.action, step.data, step.carried = 'write', merged, True
        step.executable = theirs if ours == base else ours
        return ''
    if step.old is None:
        return 'an untracked or uncommitted file already exists here'
    if step.new is None:
        return 'the branch deletes this file, which has uncommitted edits'
    if current is None:
        return 'the branch changes this file, which is deleted (uncommitted) in the checkout'
    return 'the file type clashes with uncommitted changes in the checkout'


def revalidate(top: Path, branch_ref: str, head: str, steps: list[_Step]) -> None:
    """Under index.lock: nothing the plan read may have moved."""
    if git(top, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip() != branch_ref:
        raise Refused('the checkout switched branches during landing')
    if rev(top, branch_ref) != head:
        raise Replan()
    busy = operations_in_progress(top)
    if busy:
        raise Refused(f'an operation is in progress ({", ".join(busy)})',
                      advice='Rerun after it finishes.')
    if git(top, 'ls-files', '-u', '-z').stdout:
        raise Refused('the checkout has unresolved merge conflicts')
    if steps:
        listed = git(top, 'ls-files', '-s', '-v', '-z', '--',
                      *(step.path for step in steps)).stdout
        index = {}
        for record in listed.split('\0'):
            meta, _, path = record.partition('\t')
            if path:
                index[path] = meta.split(' ')
        staged = []
        for step in steps:
            record = index.get(step.path)
            actual = None
            if record is not None:
                tag, mode, oid, stage = record
                if tag != 'H' or stage != '0':
                    staged.append(step.path)
                    continue
                actual = _Entry(mode, oid)
            if actual not in (step.old, step.new):
                staged.append(step.path)
        if staged:
            raise Refused('the checkout has staged changes on paths this landing changes',
                          staged, 'Rerun after that change is committed or unstaged by its owner.')
    for step in steps:
        now = _current(top, step.path, read=False)
        if (now.kind, now.fingerprint) != (step.current.kind, step.current.fingerprint):
            raise Replan()
    for path, observed in steps.observed.items():
        now = _current(top, path, read=False)
        if (now.kind, now.fingerprint) != (observed.kind, observed.fingerprint):
            raise Replan()
    if steps.roots and _untracked(top, steps.roots) != steps.untracked:
        raise Replan()


def _prepared_index(index_path: Path, top: Path, steps: list[_Step], oid_length: int) -> Path:
    if not index_path.is_file():
        # A fresh index would hold only the landed paths and untrack the rest.
        raise Refused(f'{index_path} is missing; the checkout has no index to update')
    descriptor, name = tempfile.mkstemp(prefix='index.git-land-', dir=str(index_path.parent))
    os.close(descriptor)
    prepared = Path(name)
    shutil.copyfile(index_path, prepared)
    shutil.copymode(index_path, prepared)  # it replaces the index itself
    zero = '0' * oid_length
    records = ''.join(f'{step.new.mode} {step.new.oid}\t{step.path}\0' if step.new
                      else f'0 {zero}\t{step.path}\0' for step in steps)
    git(top, 'update-index', '-z', '--index-info', stdin=records,
         env={'GIT_INDEX_FILE': str(prepared)})
    return prepared


def _apply(top: Path, steps: list[_Step]) -> None:
    for step in sorted((step for step in steps if step.action == 'delete'),
                       key=lambda step: step.path, reverse=True):
        path = top / step.path
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        parent = path.parent
        while parent != top:
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = parent.parent
    for step in (step for step in steps if step.action == 'write'):
        path = top / step.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f'.{path.name}.git-land-{os.getpid()}')
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        if step.new is not None and step.new.mode == '120000':
            os.symlink(os.fsdecode(step.data), temporary)
        else:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                 0o777 if step.executable else 0o666)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(step.data)
        os.replace(temporary, path)


def publish(top: Path, branch_ref: str, head: str, commit: str,
            steps: list[_Step], reflog: str) -> None:
    index_path = git_paths(top, 'index')[0]
    lock_path = index_path.with_name(index_path.name + '.lock')
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        raise IndexBusy(lock_path) from None
    prepared = None
    # index.lock stays held, and open, until the branch has moved: another
    # landing that revalidated between this index and this ref would see the
    # old tip with the new index, and one of the two compare-and-swaps would
    # then fail with its merge staged but uncommitted. An open descriptor
    # also keeps the holder visible to a landing that meets the lock.
    with os.fdopen(descriptor, 'wb'):
        try:
            revalidate(top, branch_ref, head, steps)
            prepared = _prepared_index(index_path, top, steps, len(head))
            try:
                _apply(top, steps)
            except OSError as error:
                raise LandingError(
                    f'writing the merged files failed ({error}); files already written '
                    'carry the merge as uncommitted changes, the index and branch are '
                    'unchanged') from error
            # Git's own order: index first, then the ref. An interrupted landing
            # leaves the merge staged, never a HEAD whose index reverts it.
            os.replace(prepared, index_path)
            prepared = None
            moved = git(top, 'update-ref', '-m', reflog, branch_ref, commit, head, check=False)
            if moved.returncode:
                raise LandingError(
                    f'the files and index carry merge {commit}, but {branch_ref} did not move '
                    f'from {head}: {moved.stderr.strip()[:400]}')
        finally:
            # Never renamed, so the lock is still this publication's own.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(lock_path)
            if prepared is not None:
                with contextlib.suppress(FileNotFoundError):
                    prepared.unlink()
