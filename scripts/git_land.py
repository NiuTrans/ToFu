#!/usr/bin/env python3
"""Land a finished worktree branch on the shared checkout's current branch.

``git merge`` refuses as soon as a branch touches any file that another writer
is editing, uncommitted, in the shared checkout, so one conversation's
in-flight edit locked every branch that shared a file with it. This command
records the same ``--no-ff`` merge commit (first parent: the pre-integration
tip) without that lock:

1. Git's own merge machinery computes the merge in a private scratch worktree;
   a real branch conflict refuses. Known generated outputs are rebuilt there.
2. Every path the merge changes is compared with the shared checkout's
   current bytes. An unmodified file takes the merged content. A file with
   uncommitted concurrent edits gets a three-way source merge, so those edits
   stay in place, still uncommitted, on top of the new tip. Known generated
   outputs are rebuilt separately for the committed and working source views.
3. Holding Git's own index.lock, the plan is revalidated, files are replaced
   atomically, the index entries of exactly those paths move to the merge,
   and the branch moves by compare-and-swap.

Overlapping lines, a staged change, an untracked file in the way, a file-type
clash or an operation in progress refuses before anything is written. Then the
branch's worktree is removed (``git worktree remove``, never forced) and the
branch deleted (``git branch -d``) unless --keep is given.

Usage:  python3 scripts/git_land.py BRANCH [--into CHECKOUT] [--keep] [--check]
Exit:   0 landed or already merged; 1 refused, nothing changed; 2 Git or
        filesystem error (the message states what was written).
Contract: docs/modules/project_integration.md, "Landing worktree branches".
Generation uses the shared lib.project_mod.generated_integration owner.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

# Direct CLI execution must find the same shared generator owner as imports do.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


class _Replan(Exception):
    """The branch or a planned path moved before publication."""


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
    return set(filter(None, _git(top, 'ls-files', '--others', '--exclude-standard',
                                '-z', '--', *roots).stdout.split('\0')))


def _tree_files(top, revision, roots):
    """Read the declared generator closure in one blob batch, never the entire repo."""
    entries = {}
    for record in _git(top, 'ls-tree', '-r', '-z', revision, '--', *roots).stdout.split('\0'):
        if not record:
            continue
        meta, path = record.split('\t', 1)
        mode, kind, oid = meta.split()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise Refused('generated inputs must be regular files', [path])
        entries[path] = _Entry(mode, oid)
    result = _git(top, 'cat-file', '--batch', binary=True,
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
        _git(top, 'read-tree', tree, env=env)
        records = []
        for path in sorted(set(old_outputs) | outputs.keys()):
            if path in outputs:
                oid = _git(top, 'hash-object', '-w', '--stdin', binary=True,
                           stdin=outputs[path]).stdout.decode().strip()
                mode = old_outputs[path].mode if path in old_outputs else '100644'
                records.append(f'{mode} {oid}\t{path}\0')
            else:
                records.append(f'0 {"0" * len(tree)}\t{path}\0')
        if records:
            _git(top, 'update-index', '-z', '--index-info', stdin=''.join(records), env=env)
        return _git(top, 'write-tree', env=env).stdout.strip()


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
                raise _Replan()
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


def _git(cwd, *args, stdin=None, env=None, check=True, binary=False):
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


def _rev(top: Path, revision: str) -> str:
    return _git(top, 'rev-parse', '--verify', '--quiet', f'{revision}^{{commit}}',
                check=False).stdout.strip()


def _is_ancestor(top: Path, ancestor: str, descendant: str) -> bool:
    result = _git(top, 'merge-base', '--is-ancestor', ancestor, descendant, check=False)
    if result.returncode not in (0, 1):
        raise LandingError(f'git merge-base failed: {result.stderr.strip()[:800]}')
    return result.returncode == 0


def _refuse_task_input_snapshots(top: Path, branch_ref: str, source: str) -> None:
    """A platform input is borrowed context, never a worker's merge parent."""
    inputs = _git(top, 'for-each-ref', '--format=%(objectname)',
                  'refs/tofu/inputs/').stdout.split()
    if not inputs:
        return
    snapshots = set(inputs).intersection(
        _git(top, 'rev-list', f'{branch_ref}..{source}').stdout.split())
    if not snapshots:
        return
    paths = []
    for snapshot in sorted(snapshots):
        carried = _git(top, 'diff-tree', '--root', '--no-commit-id', '--name-only',
                       '-r', '-z', snapshot).stdout.split('\0')
        paths.extend(f'{snapshot}: {path}' for path in carried if path)
    branch = branch_ref.removeprefix('refs/heads/')
    raise Refused(
        'the landing range contains a task-input snapshot commit: '
        + ', '.join(sorted(snapshots)), paths,
        f'Create a fresh worktree from the shared checkout {top} at its current branch '
        f'{branch} ({branch_ref}), then cherry-pick only your own commits onto a new '
        'branch and land that branch. Do not start from the task workspace\'s HEAD.')


def _git_paths(top: Path, *names: str) -> list[Path]:
    arguments = [part for name in names for part in ('--git-path', name)]
    return [top / line for line in _git(top, 'rev-parse', *arguments).stdout.splitlines()]


def _operations_in_progress(top: Path) -> list[str]:
    return [name for name, path in zip(_IN_PROGRESS, _git_paths(top, *_IN_PROGRESS))
            if path.exists()]


def _main_checkout(start: Path) -> Path:
    lines = _git(start, 'worktree', 'list', '--porcelain').stdout.splitlines()
    if not lines or not lines[0].startswith('worktree ') or 'bare' in lines[1:2]:
        raise Refused('the repository has no main checkout to land into',
                      advice='Pass --into CHECKOUT.')
    return Path(lines[0][len('worktree '):])


def _worktree_of(top: Path, ref: str) -> Path | None:
    path = None
    for line in _git(top, 'worktree', 'list', '--porcelain').stdout.splitlines():
        if line.startswith('worktree '):
            path = Path(line[len('worktree '):])
        elif line == f'branch {ref}':
            return path
    return None


def _all_generated(top: Path, paths: list[str]) -> bool:
    """Every path is declared ``linguist-generated`` by the current .gitattributes."""
    fields = _git(top, 'check-attr', '-z', '--cached', 'linguist-generated', '--',
                  *paths).stdout.split('\0')
    values = fields[2::3]
    return len(values) == len(paths) and all(value in ('true', 'set') for value in values)


def _merge_tree(top: Path, head: str, source: str) -> str:
    """Merge and regenerate in a private checkout."""
    if _is_ancestor(top, head, source):
        return _git(top, 'rev-parse', f'{source}^{{tree}}').stdout.strip()
    bases = _git(top, 'merge-base', '--all', head, source, check=False)
    if bases.returncode == 1 and not bases.stdout.strip():
        raise Refused('the branch shares no history with the current branch')
    if bases.returncode:
        raise LandingError(f'git merge-base failed: {bases.stderr.strip()[:800]}')
    with tempfile.TemporaryDirectory(prefix='git-land-') as parent:
        scratch = Path(parent) / 'git-land-merge'
        # Merge and regeneration read a private working tree; never the
        # canonical checkout's source or index. All Git state stays scratch-local.
        _git(top, 'worktree', 'add', '--detach', str(scratch), head)
        try:
            _git(scratch, 'read-tree', head)
            _git(scratch, 'reset', '--hard', head)
            merged = _git(scratch, 'merge-recursive', *bases.stdout.split(), '--',
                          head, source, check=False)
            if merged.returncode == 1:
                unmerged = _git(scratch, 'ls-files', '-u', '-z').stdout
                paths = sorted({entry.split('\t', 1)[1]
                                for entry in unmerged.split('\0') if '\t' in entry})
                if paths:
                    from lib.project_mod.generated_integration import (
                        GeneratedOutputError, resolve_generated_conflicts)
                    def scratch_git(_root, args, env=None, **kwargs):
                        environment = {'GIT_INDEX_FILE': str(_git_paths(scratch, 'index')[0])}
                        environment.update(env or {})
                        return _git(scratch, *args, env=environment, **kwargs)
                    try:
                        if resolve_generated_conflicts(
                                scratch, paths, source, scratch_git, source_root=top) is not None:
                            tree = _git(scratch, 'write-tree').stdout.strip()
                            for state in ('AUTO_MERGE', 'MERGE_HEAD', 'MERGE_MODE', 'MERGE_MSG'):
                                _git_paths(scratch, state)[0].unlink(missing_ok=True)
                            return tree
                    except GeneratedOutputError as error:
                        raise Refused('generated outputs could not be rebuilt', paths, str(error)) from error
                advice = ('Merge the current branch into the worktree branch, resolve and '
                          'commit there, then rerun.')
                if paths and _all_generated(top, paths):
                    advice = ('Only generated files conflict. Merge the current branch into the '
                              'worktree branch, regenerate these files from the merged sources '
                              'with their generators (never hand-merge them), run their checks, '
                              'commit there, then rerun.')
                raise Refused(
                    'the branch conflicts with the current branch',
                    paths or [line for line in merged.stdout.splitlines()
                              if line.startswith('CONFLICT')],
                    advice)
            if merged.returncode:
                raise LandingError('git merge-recursive failed: '
                                   + (merged.stderr or merged.stdout).strip()[:800])
            return _git(scratch, 'write-tree').stdout.strip()
        finally:
            _git(top, 'worktree', 'remove', '--force', str(scratch), check=False)


def _changes(top: Path, head: str, tree: str) -> list[tuple[str, _Entry | None, _Entry | None]]:
    tokens = _git(top, 'diff', '--raw', '-z', '--no-renames', '--no-abbrev',
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
        return _git(top, 'cat-file', 'blob', entry.oid, binary=True).stdout
    return _git(top, 'cat-file', '--filters', f'--path={path}', entry.oid,
                binary=True).stdout


def _merge_file(top: Path, step: _Step) -> tuple[bytes | None, str]:
    inputs = (step.current.data, _smudged(top, step.path, step.old),
              _smudged(top, step.path, step.new))
    with tempfile.TemporaryDirectory(prefix='git-land-file-') as directory:
        names = []
        for name, data in zip(('checkout', 'base', 'landed'), inputs):
            (Path(directory) / name).write_bytes(data)
            names.append(str(Path(directory) / name))
        merged = _git(top, 'merge-file', '-p', '-L', 'checkout', '-L', 'base',
                      '-L', 'landed', *names, check=False, binary=True)
    if merged.returncode == 0:
        return merged.stdout, ''
    if 0 < merged.returncode < 128:
        return None, 'the branch and uncommitted edits in the checkout change the same lines'
    return None, 'uncommitted edits in the checkout cannot be merged (binary content?)'


def _plan(top: Path, head: str, tree: str, file_mode: bool) -> _Plan:
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
    oids = _git(top, 'hash-object', '--stdin-paths',
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


def _revalidate(top: Path, branch_ref: str, head: str, steps: list[_Step]) -> None:
    """Under index.lock: nothing the plan read may have moved."""
    if _git(top, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip() != branch_ref:
        raise Refused('the checkout switched branches during landing')
    if _rev(top, branch_ref) != head:
        raise _Replan()
    busy = _operations_in_progress(top)
    if busy:
        raise Refused(f'an operation is in progress ({", ".join(busy)})',
                      advice='Rerun after it finishes.')
    if _git(top, 'ls-files', '-u', '-z').stdout:
        raise Refused('the checkout has unresolved merge conflicts')
    if steps:
        listed = _git(top, 'ls-files', '-s', '-v', '-z', '--',
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
            raise _Replan()
    for path, observed in steps.observed.items():
        now = _current(top, path, read=False)
        if (now.kind, now.fingerprint) != (observed.kind, observed.fingerprint):
            raise _Replan()
    if steps.roots and _untracked(top, steps.roots) != steps.untracked:
        raise _Replan()


def _prepared_index(index_path: Path, top: Path, steps: list[_Step], oid_length: int) -> Path:
    if not index_path.is_file():
        # A fresh index would hold only the landed paths and untrack the rest.
        raise Refused(f'{index_path} is missing; the checkout has no index to update')
    descriptor, name = tempfile.mkstemp(prefix='index.git-land-', dir=str(index_path.parent))
    os.close(descriptor)
    prepared = Path(name)
    shutil.copyfile(index_path, prepared)
    zero = '0' * oid_length
    records = ''.join(f'{step.new.mode} {step.new.oid}\t{step.path}\0' if step.new
                      else f'0 {zero}\t{step.path}\0' for step in steps)
    _git(top, 'update-index', '-z', '--index-info', stdin=records,
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


def _open_holders(path: Path) -> list[str] | None:
    """PIDs (visible to this user) holding ``path`` open; None without /proc."""
    proc = Path('/proc')
    if not proc.is_dir():
        return None
    target = os.path.realpath(path)
    holders = []
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if any(os.readlink(fd) == target for fd in (process / 'fd').iterdir()):
                holders.append(process.name)
        except OSError:
            continue  # exited, or not ours to inspect
    return holders


def _busy_lock(lock_path: Path) -> Refused:
    """Refusal for an existing index.lock, with the evidence to tell busy from stale."""
    try:
        age = time.time() - lock_path.stat().st_mtime
    except FileNotFoundError:
        return Refused(f'{lock_path} was briefly held by another Git process',
                       advice='Rerun now.')
    holders = _open_holders(lock_path)
    if holders is None or holders:
        owner = f'process {", ".join(holders)}' if holders else 'another Git process'
        return Refused(f'{lock_path} is held by {owner}: Git is busy in this checkout',
                       advice='Rerun when it finishes.')
    return Refused(
        f'{lock_path} exists ({age:.0f} s old) but no process of this user holds it open; '
        'a Git process probably exited while holding it',
        advice='Confirm no Git command is running in this checkout, remove that file '
               '(Git requires the same manual step), and rerun.')


def _publish(top: Path, branch_ref: str, head: str, commit: str,
             steps: list[_Step], reflog: str) -> None:
    index_path = _git_paths(top, 'index')[0]
    lock_path = index_path.with_name(index_path.name + '.lock')
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        raise _busy_lock(lock_path) from None
    installed = False
    prepared = None
    try:
        with os.fdopen(descriptor, 'wb') as lock:
            _revalidate(top, branch_ref, head, steps)
            prepared = _prepared_index(index_path, top, steps, len(head))
            try:
                _apply(top, steps)
            except OSError as error:
                raise LandingError(
                    f'writing the merged files failed ({error}); files already written '
                    'carry the merge as uncommitted changes, the index and branch are '
                    'unchanged') from error
            lock.write(prepared.read_bytes())
        # Git's own order: index first, then the ref. An interrupted landing
        # leaves the merge staged, never a HEAD whose index reverts it.
        os.replace(lock_path, index_path)
        installed = True
    finally:
        if not installed:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(lock_path)
        if prepared is not None:
            with contextlib.suppress(FileNotFoundError):
                prepared.unlink()
    moved = _git(top, 'update-ref', '-m', reflog, branch_ref, commit, head, check=False)
    if moved.returncode:
        raise LandingError(
            f'the files and index carry merge {commit}, but {branch_ref} did not move from '
            f'{head}: {moved.stderr.strip()[:400]}')


def _cleanup(top: Path, source_ref: str) -> list[str]:
    notes = []
    worktree = _worktree_of(top, source_ref)
    if worktree is not None:
        removed = _git(top, 'worktree', 'remove', str(worktree), check=False)
        if removed.returncode:
            return [f'kept worktree {worktree}: {removed.stderr.strip()}',
                    f'kept branch {source_ref.removeprefix("refs/heads/")}: its worktree remains']
        notes.append(f'removed worktree {worktree}')
    name = source_ref.removeprefix('refs/heads/')
    deleted = _git(top, 'branch', '-d', name, check=False)
    notes.append(f'deleted branch {name}' if deleted.returncode == 0
                 else f'kept branch {name}: {deleted.stderr.strip()}')
    return notes


def land(branch: str, *, into: str | None = None, keep: bool = False,
         check: bool = False, cwd: str | os.PathLike = '.') -> dict:
    """Land BRANCH; raises Refused (nothing changed) or LandingError."""
    top = (Path(_git(into, 'rev-parse', '--show-toplevel').stdout.strip()) if into
           else _main_checkout(Path(cwd)))
    branch_ref = _git(top, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip()
    if not branch_ref.startswith('refs/heads/'):
        raise Refused(f'{top} has no checked-out branch to land on')
    source = _rev(top, branch)
    if not source:
        raise Refused(f'{branch!r} does not name a commit')
    source_ref = _git(top, 'rev-parse', '--symbolic-full-name', branch, check=False).stdout.strip()
    if source_ref == branch_ref:
        raise Refused(f'{branch} is the branch checked out in {top}')
    busy = _operations_in_progress(top)
    if busy:
        raise Refused(f'{top} has an operation in progress ({", ".join(busy)})',
                      advice='Rerun after it finishes.')
    file_mode = _git(top, 'config', '--bool', 'core.fileMode', check=False).stdout.strip() != 'false'
    is_branch = source_ref.startswith('refs/heads/')
    label = source_ref.removeprefix('refs/heads/') if is_branch else source[:12]
    target = branch_ref.removeprefix('refs/heads/')
    message = (f"Merge branch '{label}' into {target}" if is_branch
               else f"Merge commit '{label}' into {target}")
    result: dict = {'branch': label, 'into': target, 'checkout': str(top), 'source': source}
    merged_for = tree = None
    while True:
        head = _rev(top, branch_ref)
        result['preIntegration'] = head
        if _is_ancestor(top, source, head):
            result['status'] = 'already-merged'
            break
        _refuse_task_input_snapshots(top, branch_ref, source)
        if merged_for != head:
            tree = _merge_tree(top, head, source)
        merged_for = head
        try:
            steps = _plan(top, head, tree, file_mode)
            result.update(paths=len(steps), carried=[step.path for step in steps if step.carried])
            if check:
                _revalidate(top, branch_ref, head, steps)
                result['status'] = 'landable'
                return result
            commit = _git(top, 'commit-tree', steps.tree, '-p', head, '-p', source, '-m', message).stdout.strip()
            _publish(top, branch_ref, head, commit, steps,
                     f'merge {label}: landed by scripts/git_land.py')
        except _Replan:
            continue
        result.update(status='landed', commit=commit)
        break
    if check:
        return result
    if not keep and is_branch:
        result['cleanup'] = _cleanup(top, source_ref)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Land a finished worktree branch on the shared checkout without '
                    'locking on other writers\' uncommitted edits.')
    parser.add_argument('branch', help='branch (or commit) to land')
    parser.add_argument('--into', help='checkout whose current branch receives the merge '
                                       '(default: the repository\'s main checkout)')
    parser.add_argument('--keep', action='store_true',
                        help='keep the worktree and branch after landing')
    parser.add_argument('--check', action='store_true',
                        help='report what landing would do; change nothing')
    args = parser.parse_args(argv)
    try:
        result = land(args.branch, into=args.into, keep=args.keep, check=args.check)
    except Refused as refusal:
        print(f'Refused: {refusal.reason}. Nothing was changed.')
        for path in refusal.paths:
            print(f'  {path}')
        if refusal.advice:
            print(refusal.advice)
        return 1
    except LandingError as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2
    status = result['status']
    if status == 'already-merged':
        print(f"{result['branch']} is already merged into {result['into']} "
              f"({result['preIntegration']}).")
    elif status == 'landable':
        print(f"{result['branch']} can land on {result['into']}: {result['paths']} paths change.")
    else:
        print(f"Landed {result['branch']} on {result['into']}: merge {result['commit']}; "
              f"pre-integration tip {result['preIntegration']}; {result['paths']} paths changed.")
    if result.get('carried'):
        print('Carried forward uncommitted edits in: ' + ', '.join(result['carried']))
    for note in result.get('cleanup', []):
        print(note)
    return 0


if __name__ == '__main__':
    sys.exit(main())
