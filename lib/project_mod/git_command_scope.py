"""Resolve Git effects once for approval and checkout writer admission.

Entry point: git_command_scopes(command, base, environment). Classification
comes from command_analysis; checkout identity comes from workspace_lease.
This module only parses literal shell/Git arguments and reads path metadata.
It never executes Git, authorizes writes, owns locks, or caches task identity.
"""
from dataclasses import dataclass
import os
import re
import shlex

from .command_analysis import (
    _ENV_ASSIGNMENT_GUARD_RE, _GIT_GUARD_GLOBAL_BARE_FLAGS,
    _GIT_GUARD_GLOBAL_VALUE_OPTS, _classify_git_invocation,
    _git_worktree_add_destination, _mask_quoted_literals,
    _split_pipeline_spans, _unwrap_command_parts,
)
from .workspace_lease import _checkout_identity


@dataclass(frozen=True)
class GitCommandScope:
    start: int
    end: int
    classification: str | None
    paths: tuple[str, ...]
    future_paths: tuple[str, ...] = ()
    creates_checkout: bool = False
    execution_cwds: tuple[str, ...] = ()
    subcommand: str = ""

    def presence_roots(self):
        roots = set()
        for path in self.paths:
            identity = None if path in self.future_paths else _checkout_identity(path)
            roots.add(identity[0] if identity else path)
        return tuple(sorted(roots))

    def writer_paths(self):
        # A proven successful creation cannot affect an existing parent
        # checkout. Git refuses collisions and locks its new refs itself.
        return tuple(path for path in self.paths if path not in self.future_paths)


def _literal_path(base, path):
    if re.search(r'[$`]', path) or path.startswith('~'):
        raise ValueError('Git target requires unresolved shell expansion')
    return os.path.realpath(os.path.join(base, path))


def _static_segment(source):
    return not (re.search(r'[$`]', source)
                or re.search(r'[\r\n;&|<>(){}*?\[\]#]', _mask_quoted_literals(source)))


def _git_targets(words, cwd, environment):
    """Resolve global options only; messages/pathspecs never select a cwd."""
    index = 1
    work_tree = environment.get('GIT_WORK_TREE')
    metadata = {key: value for key, value in environment.items()
                if key in {'GIT_DIR', 'GIT_INDEX_FILE'} and value}
    ambiguous = False
    while index < len(words):
        option = words[index]
        if option in _GIT_GUARD_GLOBAL_VALUE_OPTS:
            if index + 1 == len(words):
                raise ValueError('Incomplete Git global option')
            value = words[index + 1]
            if option == '-C':
                cwd = _literal_path(cwd, value)
            elif option == '--work-tree':
                work_tree = value
            elif option == '--git-dir':
                metadata['GIT_DIR'] = value
            elif option in {'--exec-path', '--namespace'}:
                ambiguous = True
            index += 2
        elif option.startswith('-C') and len(option) > 2:
            cwd = _literal_path(cwd, option[2:])
            index += 1
        elif option.startswith('-c') and len(option) > 2:
            index += 1
        elif option in _GIT_GUARD_GLOBAL_BARE_FLAGS:
            index += 1
        elif option.startswith('--work-tree='):
            work_tree = option.split('=', 1)[1]
            index += 1
        elif option.startswith('--git-dir='):
            metadata['GIT_DIR'] = option.split('=', 1)[1]
            index += 1
        elif option.startswith('--') and '=' in option:
            ambiguous = True
            index += 1
        else:
            break
    paths = {_literal_path(cwd, work_tree or '.')}
    paths.update(_literal_path(cwd, value) for value in metadata.values())
    destination = (_git_worktree_add_destination(words[index + 1:])
                   if words[index:index + 1] == ['worktree'] else None)
    if work_tree or metadata or ambiguous:
        destination = None
    return (paths, (_literal_path(cwd, destination) if destination else None),
            ambiguous, cwd, words[index] if index < len(words) else '')


def git_command_scopes(command, base, *, environment, strict=True):
    """Return per-Git-segment effects through literal cwd/control flow.

    A failed cd followed by ;/|| may leave either cwd; pipelines cannot move
    their peers' cwd. A future checkout survives only success-dependent &&
    control. Metadata/environment redirects participate in the same target set.
    Nonliteral/unknown targets raise ValueError for the caller's existing gate.
    """
    scopes = []
    cwds = {os.path.realpath(base or os.getcwd())}
    fallback_cwds = set()
    future_checkouts = set()
    previous_separator = ''
    bound_environment = dict(environment)
    spans = _split_pipeline_spans(command)
    for index, (start, end) in enumerate(spans):
        source = command[start:end].strip()
        next_separator = (command[end:spans[index + 1][0]].strip()
                          if index + 1 < len(spans) else '')
        if previous_separator != '&&':
            future_checkouts.clear()
        if previous_separator in {';', '||'}:
            cwds.update(fallback_cwds)
        try:
            bare_words = shlex.split(source)
        except ValueError as exc:
            if strict:
                raise ValueError('Unterminated shell argument') from exc
            bare_words = []
        words = list(bare_words)
        segment_environment = dict(bound_environment)
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            key, value = words.pop(0).split('=', 1)
            segment_environment[key] = value
        words = _unwrap_command_parts(words)
        if words[:1] == ['env'] and (len(words) == 1 or not words[1].startswith('-')):
            words = words[1:]
            while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
                key, value = words.pop(0).split('=', 1)
                segment_environment[key] = value
        if not words:
            bound_environment.update(segment_environment)
        if words[:1] == ['export'] and _static_segment(source):
            for word in words[1:]:
                if _ENV_ASSIGNMENT_GUARD_RE.match(word):
                    key, value = word.split('=', 1)
                    bound_environment[key] = value
        if bare_words[:1] == ['cd']:
            cd_args = bare_words[1:]
            if cd_args[:1] == ['--']:
                cd_args = cd_args[1:]
            if len(cd_args) != 1 or not _static_segment(source):
                if strict:
                    raise ValueError('Cwd requires unresolved shell control')
                previous_separator = next_separator
                future_checkouts.clear()
                continue
            path = cd_args[0]
            if (segment_environment.get('CDPATH') and not os.path.isabs(path)
                    and not path.startswith(('./', '../'))):
                raise ValueError('CDPATH changes command cwd identity')
            targets = {_literal_path(cwd, path) for cwd in cwds}
            fallback_cwds.update(cwds)
            if previous_separator == '|' or next_separator == '|':
                pass  # cd executes in a pipeline subshell.
            elif next_separator == '&&':
                cwds = targets
            else:
                cwds.update(targets)
        elif words and os.path.basename(words[0]) == 'git':
            paths, future_paths = set(), set()
            creates_checkout = False
            execution_cwds = set()
            for cwd in cwds:
                try:
                    resolved, destination, ambiguous, execution_cwd, subcommand = _git_targets(words, cwd, segment_environment)
                except ValueError:
                    if strict:
                        raise
                    resolved, destination, ambiguous, execution_cwd, subcommand = {cwd}, None, True, cwd, ''
                execution_cwds.add(execution_cwd)
                if ambiguous:
                    resolved.add(cwd)
                if destination and _static_segment(source) and not os.path.lexists(destination):
                    creates_checkout = True
                    if next_separator == '&&':
                        future_checkouts.add(destination)
                    continue
                paths.update(resolved)
                future_paths.update(resolved & future_checkouts)
            scopes.append(GitCommandScope(
                start, end, _classify_git_invocation(words[1:]),
                tuple(sorted(paths)), tuple(sorted(future_paths)), creates_checkout,
                tuple(sorted(execution_cwds)), subcommand))
        elif words:
            # Opaque code cannot attest that a future .git identity survived.
            future_checkouts.clear()
        previous_separator = next_separator
    return tuple(scopes)
