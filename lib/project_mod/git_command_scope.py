"""Resolve Git effects once for approval, the Git guard and read preparation.

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
    _ENV_ASSIGNMENT_GUARD_RE, _GIT_GUARD_GLOBAL_BARE_FLAGS, _HARMLESS_GIT_CONFIG,
    _GIT_GUARD_GLOBAL_VALUE_OPTS, _classify_git_invocation,
    _git_worktree_add_destination, _lease_free_git_segment, _mask_quoted_literals,
    _shell_words, git_worktree_lifecycle,
    _split_pipeline_spans, _MAX_WRAPPER_DEPTH, unwrap_process_wrappers, pipeline_span_separator,
    command_segment_words, shell_source_has_expansion,
    command_segment_discardable_redirect_split, _literal_heredoc_body_view,
    command_segment_word_shapes, git_read_expansion_inert, peeled_command,
    command_substitution_bodies,
)
from .workspace_lease import _checkout_binding, _checkout_identity


# The cwd after a cd whose directory the shell picks at run time. No checkout
# contains it, so approval falls back to its attended gate.
UNRESOLVED_CWD = '/dev/null/tofu-unresolved-cwd'


@dataclass(frozen=True)
class CwdChange:
    """A command that moves the shell's cwd; *target* None when decided at run time."""
    target: str | None
    certain: bool


_UNRESOLVED_CHANGE = CwdChange(None, False)
_CD_OPTION = re.compile(r'-[LPe@]+\Z')
_LOOP_HEADS = frozenset({'for', 'while', 'until', 'select'})
_FUNCTION_DEFINITION = re.compile(
    r'(?:^|[\s;&|(){}])(?:function\s+[^\s;&|()]+|[A-Za-z_][\w.:-]*\s*\(\s*\))')


def shell_cwd_change(segment):
    """How one simple command, reserved words already peeled, moves the shell's cwd.

    None when it leaves the cwd alone. ``CwdChange(DIR, certain)`` when it
    enters the literal directory DIR: ``cd [-L|-P] [--] DIR`` alone, after
    ``command`` or variable assignments, or with redirects that only discard
    output. ``certain`` is False when the command may fail whatever DIR is:
    POSIX sh finds no bash-only ``builtin`` or ``pushd`` and stays put.
    ``CwdChange(None, False)`` when the shell picks the directory at run time
    (an expanded, ``~`` or ``-`` target, ``cd`` alone, ``popd``, ``pushd``
    without one directory) or when another redirect may stop the cd. A wrapper
    that execs a program (``env cd``, ``timeout 5 cd``) runs an external
    ``cd`` in a child and moves nothing.
    """
    discardable = command_segment_discardable_redirect_split(segment)
    if discardable is not None:
        segment = discardable[0]
    try:
        words = [(token.text, shape.computed) for token, shape in zip(
            _shell_words(segment, strict=True, redirects=True),
            command_segment_word_shapes(segment))]
    except ValueError:
        words = [(word, True) for word in segment.split()]
    while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0][0]):
        del words[0]
    certain = True
    if words[:1] == [('command', False)]:
        del words[0]
        if words[:1] == [('-p', False)]:
            del words[0]
    elif words[:1] == [('builtin', False)]:
        del words[0]
        certain = False
    if not words or words[0][0] not in ('cd', 'pushd', 'popd'):
        return None
    program, operands = words[0][0], words[1:]
    if words[0][1] or program == 'popd' or any(
            text.startswith(('<', '>')) for text, _computed in operands):
        return _UNRESOLVED_CHANGE
    if program == 'pushd':
        certain = False
    else:
        while operands and not operands[0][1] and _CD_OPTION.match(operands[0][0]):
            # dash rejects bash's -e/-@ ("Illegal option") and stays put.
            certain = certain and set(operands[0][0][1:]) <= {'L', 'P'}
            del operands[0]
        if operands[:1] == [('--', False)]:
            del operands[0]
    if len(operands) != 1:
        return _UNRESOLVED_CHANGE
    target, computed = operands[0]
    if computed or not target or target.startswith(('-', '+', '~')):
        return _UNRESOLVED_CHANGE
    return CwdChange(target, certain)


def cwd_change_targets(cwds, change, cdpath=''):
    """The directories *change* may enter from each of *cwds*.

    A relative target under a set CDPATH may resolve through CDPATH, and one
    taken from an unresolved cwd stays unresolved.
    """
    target = change.target
    if target is None or (cdpath and not target.startswith(('/', './', '../'))):
        return {UNRESOLVED_CWD}
    return {UNRESOLVED_CWD if cwd == UNRESOLVED_CWD and not os.path.isabs(target)
            else os.path.realpath(os.path.join(cwd, target)) for cwd in cwds}


def _literal_loop_frame(segment):
    """``(variable, values)`` of a ``for NAME in <plain words>`` header, else None."""
    from .command_setup import _LOOP_HEAD, _literal_loop_values, _ordinary_loop_variable
    match = _LOOP_HEAD.fullmatch(segment.strip().lstrip('(').strip())
    if match is None or not _ordinary_loop_variable(match.group(1)):
        return None
    values = _literal_loop_values(match.group(2) or '')
    return (match.group(1), tuple(values)) if values else None


def literal_loop_body_openers(source, spans):
    """``{span index: (frame, command)}`` for each first command of a literal loop body.

    The ``do`` right after ``for NAME in <plain words>`` belongs to that loop
    whatever encloses it, so the command it introduces (on the same line or
    after a bare ``do`` line) starts every iteration with NAME bound to one
    of those values. Nothing else is tracked: a later command may follow a
    rebinding, and a ``do`` after any other header belongs to another loop.
    """
    openers, pending, after_do = {}, None, False
    for index, (start, end) in enumerate(spans):
        segment = source[start:end].strip()
        frame, opened = pending, after_do
        pending, after_do = None, False
        if frame is not None and opened:
            openers[index] = (frame, segment)
        elif frame is not None and segment == 'do':
            pending, after_do = frame, True
        elif frame is not None and segment.startswith('do') and segment[2:3].isspace():
            openers[index] = (frame, segment[2:].strip())
        header = _literal_loop_frame(segment)
        if header is not None:
            pending = header
    return openers


def loop_body_cd_targets(opener):
    """Directories a literal loop body's opening ``cd /abs/$NAME`` enters, else None.

    *opener* is a :func:`literal_loop_body_openers` value. One absolute target
    per loop value; any other expansion, a relative result (it would depend
    on the previous iteration's cwd) or another command returns None.
    """
    from .command_setup import _substitute_loop_reference
    if opener is None:
        return None
    (variable, values), command = opener
    targets = set()
    for value in values:
        resolved = _substitute_loop_reference(command, variable, value)
        change = None if resolved is None else shell_cwd_change(resolved)
        if change is None or change.target is None or not os.path.isabs(change.target):
            return None
        targets.add(os.path.realpath(change.target))
    return targets


def repeated_cwd_changes(source, spans):
    """``{span index: directories}`` that repeated bodies may enter from that span on.

    A loop body runs again from wherever its previous iteration left the
    shell, and a function body wherever and as often as it is called. So a cd
    in a loop may move every command from the loop's first segment on, and
    in a script that defines a function, every command of the script. An
    absolute target is added as itself, a literal loop body's opening
    ``cd /abs/$NAME`` as one target per value; any other target is unresolved.
    """
    region_start = 0 if _FUNCTION_DEFINITION.search(_mask_quoted_literals(source)) else None
    added, depth, loop_start = {}, 0, None
    openers = literal_loop_body_openers(source, spans)
    for index, (start, end) in enumerate(spans):
        segment = source[start:end].strip()
        head = segment.split(None, 1)[0] if segment else ''
        if head in _LOOP_HEADS:
            loop_start = index if not depth else loop_start
            depth += 1
        region = region_start if region_start is not None else (loop_start if depth else None)
        change = shell_cwd_change(peeled_command(segment)) if region is not None else None
        bound = loop_body_cd_targets(openers.get(index)) if change is not None else None
        if bound is not None:
            added.setdefault(region, set()).update(bound)
        elif change is not None:
            target = change.target
            added.setdefault(region, set()).add(
                os.path.realpath(target) if target and os.path.isabs(target) else UNRESOLVED_CWD)
        if head == 'done' and depth:
            depth -= 1
    return added


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
    # Lifecycle commands share metadata with the invoker but destroy only
    # their tree operands, unless executable helpers/redirects widen effects.
    presence_paths: tuple[str, ...] | None = None
    # Ordered literal -C operands, replayed against the shell's actual cwd
    # when a readonly query cannot select one checkout before execution.
    directory_steps: tuple[str, ...] = ()

    def presence_roots(self):
        if self.presence_paths is not None:
            return self.presence_paths
        roots = set()
        for path in self.paths:
            identity = None if path in self.future_paths else _checkout_identity(path)
            roots.add(identity[0] if identity else path)
        return tuple(sorted(roots))


def _literal_path(base, path):
    return os.path.realpath(os.path.join(base, path))


def _static_segment(source):
    return not (shell_source_has_expansion(source)
                or re.search(r'[\r\n;&|<>(){}*?\[\]#]', _mask_quoted_literals(source)))


def leading_cd_enters(command, start, path, targets):
    """Whether the literal ``cd PATH`` segment at offset *start* surely enters *targets*.

    Only the first command of a script runs before anything that could remove
    its target, and ``cd`` into an existing directory this process may search
    cannot fail, so no later segment can still run in the original cwd. The
    shell resolves ``..`` against its logical cwd while *targets* are physical
    paths, so such a path never qualifies. The answer holds when it is asked:
    a target removed before the shell runs the cd sends later segments back to
    the original cwd.
    """
    if command[:start].strip() or '..' in path.split('/'):
        return False
    return all(os.path.isdir(target) and os.access(target, os.X_OK) for target in targets)


def _expansion_may_select_target(source):
    """Whether a shell expansion can change Git's repository, verb or options.

    Pathname expansion after a literal ``--`` produces pathspecs only, and so
    does any expansion that :func:`git_read_expansion_inert` proves cannot
    become an option of a read verb. Any other parameter/command/arithmetic
    expansion, and any expansion up to that separator, may still add an
    option such as ``-C`` or ``--output``.
    """
    if not shell_source_has_expansion(source) or git_read_expansion_inert(source):
        return False
    if shell_source_has_expansion(source, pathname=False):
        return True
    try:
        tokens = _shell_words(source, strict=True, redirects=True)
    except ValueError:
        return True
    separator = next((token for token in tokens if source[token.start:token.end] == '--'), None)
    return separator is None or shell_source_has_expansion(source[:separator.end])


def _git_targets(words, cwd, environment):
    """Resolve global options only; messages/pathspecs never select a cwd."""
    index = 1
    work_tree = environment.get('GIT_WORK_TREE')
    metadata = {key: value for key, value in environment.items()
                if key in {'GIT_DIR', 'GIT_INDEX_FILE'} and value}
    ambiguous = False
    directory_steps = []
    while index < len(words):
        option = words[index]
        if option in _GIT_GUARD_GLOBAL_VALUE_OPTS:
            if index + 1 == len(words):
                raise ValueError('Incomplete Git global option')
            value = words[index + 1]
            if option == '-C':
                directory_steps.append(value)
                cwd = _literal_path(cwd, value)
            elif option == '--work-tree':
                work_tree = value
            elif option == '--git-dir':
                metadata['GIT_DIR'] = value
            elif option in {'--exec-path', '--namespace', '--config-env'}:
                # Dynamic configuration can enable callbacks, including after
                # literal disabling overrides. Both config-env forms widen.
                ambiguous = True
            index += 2
        elif option.startswith('-C') and len(option) > 2:
            directory_steps.append(option[2:])
            cwd = _literal_path(cwd, option[2:])
            index += 1
        elif option.startswith('-c') and len(option) > 2:
            index += 1
        elif option in _GIT_GUARD_GLOBAL_BARE_FLAGS:
            if option in {'--paginate', '-p'}:
                ambiguous = True  # A requested pager is an executable callback.
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

    common_dir = environment.get('GIT_COMMON_DIR')
    if common_dir:
        # Unlike a gitdir's commondir file, the environment override is
        # relative to Git's effective cwd (after every -C), not its gitdir.
        paths.add(_literal_path(cwd, common_dir))
        ambiguous = True
    destination = (_git_worktree_add_destination(words[index + 1:])
                   if words[index:index + 1] == ['worktree'] else None)
    if work_tree or metadata or ambiguous:
        destination = None
    if work_tree or metadata:
        # A redirect may name a bare/admin directory with no ancestor .git
        # marker. Protect the actual declared tree rather than a no-op root
        # or metadata lease inferred from the invocation checkout.
        ambiguous = True
    return (paths, (_literal_path(cwd, destination) if destination else None),
            ambiguous, cwd, words[index] if index < len(words) else '', index,
            tuple(directory_steps))


def _git_configs(words, position):
    """Only global -c operands (including attached forms) configure this invocation."""
    configs = set()
    index = 1
    while index < position:
        value = words[index]
        if value == '-c' and index + 1 < position:
            configs.add(words[index + 1])
            index += 2
        elif value.startswith('-c') and len(value) > 2:
            configs.add(value[2:])
            index += 1
        elif value in _GIT_GUARD_GLOBAL_VALUE_OPTS:
            index += 2
        else:
            index += 1
    return configs


def _git_inline_include(configs):
    return any(value.lower().startswith(('include.path=', 'includeif.')) for value in configs)


def _git_config_has_helpers(filename, helper_keys, helper_sections=()):
    """Read bounded config without includes/subprocesses; uncertainty widens.

    Git accepts a key immediately after a section header, and boolean keys
    without '='. Inspect keys rather than values so those forms cannot hide
    helpers or include paths. This is deliberately not a full Git parser:
    unsupported syntax/continuations cannot prove absence of callbacks.
    ``helper_sections`` names sections whose every key is a callback
    (``[pager] log = program``).
    """
    try:
        with open(filename, encoding='utf-8') as stream:
            text = stream.read(65537)
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError):
        return True
    if len(text) > 65536 or re.search(r'\\\r?\n', text):
        return True
    keys = set(helper_keys.split('|'))
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('['):
            section = re.match(r'\[(?:[^"\\\]\r\n]|"(?:[^"\\\r\n]|\\.)*")+\]\s*', line)
            if not section:
                return True
            name = re.match(r'\[\s*([A-Za-z0-9-]+)', line)
            if name and name[1].lower() in helper_sections:
                return True
            line = line[section.end():]
            if not line or line.startswith(('#', ';')):
                continue
        key = re.match(r'([A-Za-z][A-Za-z0-9-]*)\s*(?:=|[#;]|$)', line)
        if not key or key[1].lower() in keys:
            return True
    return False


def _git_config_injected(environment):
    """Whether the environment injects Git configuration admission cannot read.

    Every Tofu-run shell carries exactly the maintenance-off parameters
    (`lib.git_runtime`); they start no helper, so a server or probe launched
    from such a shell judges Git like the parent server does.
    """
    from lib.git_runtime import AUTOMATIC_GIT_MAINTENANCE_OFF
    injected = {key for key in ('GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG',
                                'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_GLOBAL') if environment.get(key)}
    return bool(injected) and not (
        injected == {'GIT_CONFIG_PARAMETERS'}
        and environment['GIT_CONFIG_PARAMETERS'].strip() == AUTOMATIC_GIT_MAINTENANCE_OFF)


def _git_executable_helpers(paths, configs, environment, subcommand, *, read=False):
    """Bounded conservative evidence for callbacks outside native Git effects.

    ``read`` adds the callbacks an unhardened inspection can reach: a pager,
    an external diff or textconv driver, and signature verification.
    """

    if _git_inline_include(configs):
        return True  # Includes may override even an earlier hooksPath=/dev/null.
    if _git_config_injected(environment):
        return True
    if read and any(value and (key.startswith('GIT_TRACE') or key in {
            'GIT_PAGER', 'PAGER', 'GIT_EXTERNAL_DIFF', 'GIT_EXEC_PATH'})
            for key, value in environment.items()):
        return True  # Pager/diff programs, or trace output written to a named file.
    if any(value.lower().startswith(('filter.', 'gpg.', 'core.fsmonitor=',
                                      'core.pager=', 'core.hookspath='))
           and value not in {'core.fsmonitor=false', 'core.hooksPath=/dev/null'}
           for value in configs):
        return True
    hooks_disabled = 'core.hooksPath=/dev/null' in configs
    hook_names = ('pre-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit',
                  'reference-transaction', 'post-checkout', 'post-merge',
                  'pre-rebase', 'applypatch-msg', 'pre-applypatch', 'post-applypatch',
                  'post-rewrite', 'post-index-change', 'pre-auto-gc', 'post-update')
    configuration = {'/etc/gitconfig'}
    home = environment.get('HOME')
    if home:
        configuration.update({os.path.join(home, '.gitconfig'), os.path.join(
            environment.get('XDG_CONFIG_HOME') or os.path.join(home, '.config'), 'git', 'config')})
    worktree_configuration = set()
    for path in paths:
        binding = _checkout_binding(path)
        if binding:
            common = os.fspath(binding[1])
            configuration.add(os.path.join(common, 'config'))
            # extensions.worktreeConfig is a configuration source, not a
            # callback. Inspect that source, including the linked gitdir.
            # Reading it even when disabled is conservative and avoids a
            # second parser for Git boolean precedence.
            worktree_configuration.add(os.path.join(binding[2], 'config.worktree'))
            if not hooks_disabled and any(os.access(os.path.join(common, 'hooks', hook), os.X_OK)
                                          for hook in hook_names):
                return True
    helper_keys = 'hookspath|fsmonitor|gpgsign|program|command|clean|smudge|process|path'
    if subcommand == 'pull':
        helper_keys += '|helper'  # Credential callbacks are not invoked by local commit/reset.
    sections = ()
    if read:
        helper_keys += '|pager|textconv|external|showsignature'
        sections = ('pager',)
    return (any(_git_config_has_helpers(filename, helper_keys, sections)
                for filename in configuration)
            or any(_git_config_has_helpers(filename, helper_keys + '|worktree|bare', sections)
                   for filename in worktree_configuration))


def _git_tree_claims(paths):
    """Git runs checkout-relative helpers even when invoked from a subdirectory.

    A tree effect does not imply Git metadata exclusion: linked checkouts share
    administration outside that tree, so native Git effects need both domains.
    Explicit file destinations retain their own physical containment target.
    """
    claims = set()
    for path in paths:
        binding = _checkout_identity(path)
        root = binding[0] if binding else path
        administrative = bool(binding and (path == os.fspath(binding[1])
            or path.startswith(os.fspath(binding[1]) + os.sep)))
        claims.add(('tree', root if binding and os.path.isdir(path) and not administrative else path))
        if binding:
            claims.add(('metadata', root))
    return claims


def git_metadata_mutation_mode(subcommand, args):
    """Proven native metadata effects, not risk/authorization downgrades.

    Deliberately narrow: unknown modes/options, patch/interactive, combined
    worktree flags and incomplete values cannot shed checkout admission.
    """
    if (subcommand == 'reset' and len(args) in (1, 2)
            and args[0] == '--soft'
            and all(not value.startswith('-') for value in args[1:])):
        return 'soft_reset'
    if subcommand != 'restore':
        return None
    staged = False
    index = 0
    while index < len(args):
        value = args[index]
        index += 1
        if value == '--':
            return 'staged_restore' if staged and index < len(args) else None
        if value in {'--staged', '-S'}:
            staged = True
        elif value in {'-q', '--quiet', '--no-progress'}:
            continue
        elif value in {'--source', '-s'}:
            if index >= len(args) or not args[index] or args[index].startswith('-'):
                return None
            index += 1
        elif value.startswith('--source=') or (value.startswith('-s') and len(value) > 2):
            source = value.split('=', 1)[1] if value.startswith('--') else value[2:]
            if not source or source.startswith('-'):
                return None
        else:
            return None
    return None


def _worktree_lifecycle_paths(lifecycle, cwd):
    """Physical operands, including Git's move-into-existing-directory rule."""
    source = _literal_path(cwd, lifecycle.paths[0])
    if lifecycle.operation == 'remove':
        return (source,)
    destination = _literal_path(cwd, lifecycle.paths[1])
    if os.path.isdir(destination):
        destination = _literal_path(destination, os.path.basename(source))
    return source, destination


# Plumbing queries with no native write path at all (not even the
# opportunistic index refresh of `status`), catalogued by form: every option
# must be listed, so `--output`, `--ext-diff`, `--textconv`, abbreviations and
# future options keep the conservative effects. Verbs the hardened grammar
# catalogues (rev-parse, ls-files, cherry, ...) are judged there instead.
_GIT_DIFF_PLUMBING_OPTIONS = frozenset({
    '-r', '-t', '--root', '--no-commit-id', '--name-only', '--name-status', '--stat',
    '--numstat', '--shortstat', '--summary', '-p', '-u', '--patch', '-s', '--no-patch',
    '-z', '-M', '-C', '-B', '--find-renames', '--find-copies', '--no-renames', '--always',
    '-m', '-c', '--cc', '--raw', '--abbrev', '--full-index', '--diff-filter', '--relative',
    '--cached', '--quiet', '--exit-code', '--merge-base', '--ignore-space-change',
    '--ignore-all-space', '-w', '-b', '-q', '-0', '-1', '-2', '-3', '--no-color', '--color',
    '--pretty', '--format', '--no-ext-diff', '--no-textconv',
})
_GIT_PLUMBING_READ_OPTIONS = {
    'rev-list': frozenset({
        '--max-parents', '--min-parents', '--no-merges', '--merges', '--count', '--all',
        '--branches', '--tags', '--remotes', '--reverse', '--topo-order', '--date-order',
        '--first-parent', '--ancestry-path', '--objects', '--left-right', '--left-only',
        '--right-only', '--cherry-pick', '--cherry-mark', '--boundary', '--parents',
        '--children', '--timestamp', '--abbrev-commit', '--no-walk', '--max-count', '-n',
        '--skip', '--since', '--after', '--until', '--before', '--author', '--committer',
        '--grep', '-i', '-E', '-F', '-P', '--format', '--pretty', '--oneline', '--header',
        '--quiet', '--not', '--simplify-by-decoration',
    }),
    'show-ref': frozenset({
        '--head', '--heads', '--tags', '--branches', '-d', '--dereference', '-s', '--hash',
        '--abbrev', '--verify', '-q', '--quiet', '--exists',
    }),
    'diff-tree': _GIT_DIFF_PLUMBING_OPTIONS,
    'diff-index': _GIT_DIFF_PLUMBING_OPTIONS,
    'diff-files': _GIT_DIFF_PLUMBING_OPTIONS,
    'verify-pack': frozenset({'-v', '--verbose', '-s', '--stat-only'}),
    'var': frozenset({'-l'}),
    'check-ref-format': frozenset({
        '--normalize', '--allow-onelevel', '--no-allow-onelevel', '--refspec-pattern', '--branch',
    }),
}


def _git_plumbing_read(subcommand, args, configs):
    options = _GIT_PLUMBING_READ_OPTIONS.get(subcommand)
    # `_HARMLESS_GIT_CONFIG` is the one `-c` allowlist for every read exemption.
    if options is None or not set(configs) <= _HARMLESS_GIT_CONFIG:
        return False
    for value in args:
        if value == '--':
            break
        # Signature atoms run the configured GPG/SSH verifier.
        if '%G' in value or re.search(r'%\(\*?signature(?::|\))', value):
            return False
        if (value.startswith('-') and value.split('=', 1)[0] not in options
                and not re.fullmatch(r'-[0-9]+|-U[0-9]+', value)):
            return False
    return True


def _git_lease_effects(words, cwd, subcommand, position, paths, ambiguous, environment):
    """Lease effects are separate from destructive-operation approval risk."""
    args = words[position + 1:]

    configs = _git_configs(words, position)
    unresolved_include = _git_inline_include(configs)
    if subcommand == 'worktree' and args[:1] in (['remove'], ['move']):
        lifecycle = git_worktree_lifecycle(args)
        if lifecycle is None:
            raise ValueError('Unresolved Git worktree lifecycle operands')
        operands = lifecycle.paths
        targets = _worktree_lifecycle_paths(lifecycle, cwd)
        source = targets[0]
        if not os.path.isabs(operands[0]):
            owner = _checkout_identity(source)
            invocation = _checkout_identity(cwd)
            if (ambiguous or not owner or owner[0] != source or not invocation
                    or owner[1] != invocation[1]):
                raise ValueError('Unresolved Git worktree lifecycle source; use an explicit absolute path')
        # Removal can inspect the target's status/configuration. Its own
        # worktree config must not hide a callback from the invoking checkout.
        helper_effect = ambiguous or _git_executable_helpers(
            paths | {source}, configs, environment, subcommand)
        return ((_git_tree_claims(paths) if helper_effect else
                 {('metadata', path) for path in paths})
                | {('tree', target) for target in targets})
    if ambiguous or unresolved_include:
        return _git_tree_claims(paths)
    # The hardened inspection grammar proves a local read that never
    # transforms the checkout, hardened or not (an `sh -c` body, grouping,
    # comments, pathspec globs, an output redirect). Its only native write is
    # the opportunistic index refresh of `status`, porcelain `diff` and
    # `describe --dirty`: Git takes index.lock without waiting, skips the
    # refresh while another process holds it, and never changes the result.
    # A lease cannot be scoped to that millisecond. Admission holds it for the
    # whole command on the repository's common Git directory, so one grouped
    # read in front of a long test run blocked every Git writer of every
    # linked worktree for its lifetime (2026-10-25). Configured helpers the
    # read could reach still widen to the checkout tree.
    if (_lease_free_git_segment(shlex.join(words)) is not None
            and not _git_executable_helpers(paths, configs, environment, subcommand, read=True)):
        return set()
    if (_git_plumbing_read(subcommand, args, configs)
            and not _git_executable_helpers(paths, configs, environment, subcommand, read=True)):
        return set()

    if (git_metadata_mutation_mode(subcommand, args)
            and not _git_executable_helpers(paths, configs, environment, subcommand)):
        return {('metadata', path) for path in paths}
    # Staging writes the index and object store; it only READS the working
    # tree. Treating it as a checkout transformation made one `git add -A` an
    # exclusive writer on the whole checkout, so every task running in a linked
    # worktree waited on it (observed 2026-10-22: 543 s waits). Index writers
    # still serialize against each other through the Git metadata domain.
    metadata_only = subcommand in {
        'branch', 'tag', 'update-ref', 'symbolic-ref', 'pack-refs',
        'write-tree', 'add', 'update-index',
    }
    # Commit can run arbitrary hooks/signers; explicit disabling is required.

    # Disabling hooks does not disable configured tag signing. Never let that
    # override skip signing/helper evidence merely because the args omit -s.
    if subcommand == 'tag' and 'core.hooksPath=/dev/null' in configs:
        metadata_only = 'tag.gpgSign=false' in configs

    if any(value.lower().startswith(('core.hookspath=', 'tag.gpgsign=', 'gpg.program='))
           and value != 'core.hooksPath=/dev/null' for value in configs):
        metadata_only = False
    # Signing and reference-transaction hooks are executable tree effects.
    if subcommand == 'tag' and any(value in {'-s', '-u', '--sign', '--local-user'}
                                   or value.startswith(('--local-user=', '-u')) for value in args):
        metadata_only = False
    if metadata_only and 'core.hooksPath=/dev/null' not in configs:
        for path in paths:
            identity = _checkout_identity(path)
            if identity:
                common = os.fspath(identity[1])
                hook = os.path.join(common, 'hooks', 'reference-transaction')
                if os.access(hook, os.X_OK):
                    metadata_only = False
                # `extensions.worktreeConfig` selects an additional config
                # SOURCE; it is a boolean, not a callback. Listing it here made
                # a repository that sets it (this one does) demote every
                # metadata-only verb to a whole-checkout claim. The extra source
                # is inspected below and by _git_executable_helpers instead.
                config = os.path.join(common, 'config')
                if _git_config_has_helpers(config, 'hookspath|gpgsign|include|path'):
                    metadata_only = False
                binding = _checkout_binding(path)
                if binding is not None:
                    worktree_config = os.path.join(binding[2], 'config.worktree')
                    if _git_config_has_helpers(worktree_config, 'hookspath|gpgsign|include|path'):
                        metadata_only = False
                        break
    if subcommand == 'commit':
        helpers_disabled = {'core.hooksPath=/dev/null', 'commit.gpgSign=false',
                            'core.fsmonitor=false'} <= configs
        metadata_only = (helpers_disabled or not _git_executable_helpers(
                            paths, configs, environment, subcommand)) and '--' not in args and not any(
                             value in {'-S', '--gpg-sign', '-a', '--all', '-i', '--include',
                                       '-o', '--only', '-p', '--patch', '--interactive'}
                             or value.startswith(('-S', '--gpg-sign='))
                             for value in args)
    if subcommand == 'commit' and metadata_only:
        index = 0
        while index < len(args):
            value = args[index]
            if value in {'-m', '--message', '-F', '--file', '--author', '--date'} and index + 1 < len(args):
                index += 2
            elif value in {'--amend', '--no-edit', '--allow-empty', '--allow-empty-message',
                           '--no-verify', '-q', '--quiet', '--no-gpg-sign'} or value.startswith(('--message=', '--file=', '--author=', '--date=')):
                index += 1
            else:
                metadata_only = False
                break
        if any((value.lower().startswith('core.hookspath=') and value != 'core.hooksPath=/dev/null')
               or (value.lower().startswith('core.fsmonitor=') and value != 'core.fsmonitor=false')
               or (value.lower().startswith('commit.gpgsign=') and value != 'commit.gpgSign=false')
               for value in configs):
            metadata_only = False
    if metadata_only and 'core.hooksPath=/dev/null' not in configs:
        if _git_config_injected(environment):
            metadata_only = False
        home = environment.get('HOME', '')
        config_paths = ['/etc/gitconfig']
        if home:
            config_paths.extend([os.path.join(home, '.gitconfig'), os.path.join(
                environment.get('XDG_CONFIG_HOME') or os.path.join(home, '.config'), 'git', 'config')])
        for config in config_paths:
            if _git_config_has_helpers(config, 'hookspath|gpgsign|path'):
                metadata_only = False
    if (metadata_only and subcommand in {'add', 'update-index', 'write-tree'}
            and _git_executable_helpers(paths, configs, environment, subcommand)):
        metadata_only = False  # Index writes can run post-index-change and clean filters.
    if metadata_only:
        return {('metadata', path) for path in paths}
    known_checkout_commands = {
        'add', 'commit', 'checkout', 'switch', 'reset', 'restore', 'merge',
        'rebase', 'cherry-pick', 'revert', 'am', 'apply', 'read-tree',
        'checkout-index', 'clean', 'stash', 'pull', 'bisect',
        'branch', 'tag', 'update-ref', 'symbolic-ref', 'pack-refs', 'write-tree',
    }
    # Unknown Git names may be configured !shell aliases. Their effects can
    # traverse nested checkouts, unlike a native source-checkout transform.
    kind = ('root' if subcommand in known_checkout_commands
            and not _git_executable_helpers(paths, configs, environment, subcommand) else 'tree')
    return _git_tree_claims(paths) if kind == 'tree' else {(kind, path) for path in paths}


# Verbs that never act on the invoking checkout. Only exact spellings count:
# parse-options also accepts unambiguous abbreviations (`--sep` is
# --separate-git-dir), which keep the conservative claims.
_GIT_HELP_LISTINGS = frozenset({
    '-a', '--all', '-g', '--guides', '-c', '--config', '--verbose', '--no-verbose',
})
_GIT_INIT_FLAGS = frozenset({'-q', '--quiet', '--bare', '--shared'})
_GIT_INIT_VALUES = frozenset({'-b', '--initial-branch', '--template', '--object-format'})
# No --separate-git-dir (moves an existing gitdir), -u/--upload-pack (runs a
# command) or -c/--config (persists settings that later Git runs honor).
_GIT_CLONE_FLAGS = frozenset({
    '-v', '--verbose', '-q', '--quiet', '--progress', '--no-progress',
    '-n', '--no-checkout', '--bare', '--mirror', '-l', '--local', '--no-local',
    '--no-hardlinks', '-s', '--shared', '--recurse-submodules', '--recursive',
    '--dissociate', '--single-branch', '--no-single-branch', '--tags', '--no-tags',
    '--shallow-submodules', '--no-shallow-submodules', '--remote-submodules',
    '--no-remote-submodules', '--sparse', '--reject-shallow', '--no-reject-shallow',
    '-4', '--ipv4', '-6', '--ipv6', '--also-filter-submodules',
})
_GIT_CLONE_VALUES = frozenset({
    '-b', '--branch', '-o', '--origin', '-j', '--jobs', '--depth', '--shallow-since',
    '--shallow-exclude', '--filter', '--reference', '--reference-if-able',
    '--server-option', '--template',
})
# Configuration that routes `git help <topic>` to a program: man viewers and
# their commands or paths, the help format, browsers and the HTML location.
_GIT_HELP_VIEWER_KEYS = 'viewer|cmd|path|browser|format|htmlpath'
# Environment that injects configuration, traces into named files, swaps Git
# programs, selects a man viewer or moves the object store a repository uses.
_GIT_STANDALONE_ENVIRONMENT = (
    'GIT_CONFIG', 'GIT_TRACE', 'GIT_EXEC_PATH', 'GIT_MAN_VIEWER', 'GIT_OBJECT_DIRECTORY',
)


def _git_operands(args, flags, values, *, optional_values=frozenset(), attached=()):
    """``(operands, flags_seen)`` for exactly spelled options, else None."""
    operands, seen, index = [], set(), 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == '--':
            return operands + list(args[index:]), seen
        if token in flags:
            seen.add(token)
        elif token in values:
            if index >= len(args):
                return None
            index += 1
        elif (token.startswith('--') and '=' in token
              and token.split('=', 1)[0] in values | optional_values):
            continue
        elif any(token.startswith(short) and len(token) > len(short) for short in attached):
            continue
        elif token.startswith('-'):
            return None
        else:
            operands.append(token)
    return operands, seen


def _git_help_viewer_configured(cwd, environment):
    """Whether a configuration source may route a help topic to a program.

    ``git help`` discovers the repository, so its local configuration counts.
    """
    sources = {'/etc/gitconfig'}
    home = environment.get('HOME')
    if home:
        sources.update({os.path.join(home, '.gitconfig'), os.path.join(
            environment.get('XDG_CONFIG_HOME') or os.path.join(home, '.config'), 'git', 'config')})
    binding = _checkout_binding(cwd)
    if binding:
        sources.update({os.path.join(os.fspath(binding[1]), 'config'),
                        os.path.join(binding[2], 'config.worktree')})
    return any(_git_config_has_helpers(source, _GIT_HELP_VIEWER_KEYS) for source in sources)


def _administrative_owner(path):
    """The checkout whose Git administration contains *path*, if any."""
    identity = _checkout_identity(path)
    if identity and (path == os.fspath(identity[1])
                     or path.startswith(os.fspath(identity[1]) + os.sep)):
        return identity[0]
    return None


def _git_standalone_effects(words, cwd, subcommand, position, environment):
    """Native effects of a verb that never acts on the invoking checkout.

    Returns the effect set, or None to keep the conservative effects.
    ``help`` reads documentation; a topic page runs a configured viewer
    (``man.<tool>.cmd``, a browser), so only a listing or a topic with no
    viewer configuration is effect-free. ``init`` writes its target's
    administrative directory. ``clone`` creates its destination, which Git
    refuses when occupied. Inline or environment configuration, a gitfile or link in place of
    the administrative directory and unknown options return None.
    """
    if subcommand not in {'help', 'init', 'clone'} or _git_configs(words, position):
        return None
    if any(value and key.startswith(_GIT_STANDALONE_ENVIRONMENT)
           for key, value in environment.items()):
        return None
    args = words[position + 1:]
    if subcommand == 'help':
        parsed = _git_operands(args, _GIT_HELP_LISTINGS | {'-m', '--man'}, frozenset())
        if parsed is None or len(parsed[0]) > 1:
            return None
        if parsed[0] and _git_help_viewer_configured(cwd, environment):
            return None
        return set()
    if subcommand == 'init':
        parsed = _git_operands(args, _GIT_INIT_FLAGS, _GIT_INIT_VALUES,
                               optional_values=frozenset({'--shared'}), attached=('-b',))
        if parsed is None or len(parsed[0]) > 1:
            return None
        operands, seen = parsed
        target = _literal_path(cwd, operands[0] if operands else '.')
        bare = '--bare' in seen
        administration = target if bare else os.path.join(target, '.git')
        if os.path.islink(administration) or (
                os.path.lexists(administration) and not os.path.isdir(administration)):
            return None  # Re-initialization would follow it to another repository.
        owner = _administrative_owner(administration)
        if owner:  # Re-initializing a checkout's own administration.
            return {('tree', administration), ('metadata', owner)}
        return {('tree', administration)}
    parsed = _git_operands(
        args, _GIT_CLONE_FLAGS, _GIT_CLONE_VALUES,
        optional_values=frozenset({'--recurse-submodules', '--recursive'}),
        attached=('-b', '-o', '-j'))
    if parsed is None or not 1 <= len(parsed[0]) <= 2:
        return None
    operands = parsed[0]
    destination = (_literal_path(cwd, operands[1]) if len(operands) == 2
                   else os.path.realpath(cwd))
    owner = _administrative_owner(destination)
    return {('tree', destination)} | ({('metadata', owner)} if owner else set())


def git_command_scopes(command, base, *, environment, strict=True, certain_leading_cd=False):
    """Return per-Git-segment effects through literal cwd/control flow.

    A failed cd followed by ;/|| may leave either cwd; pipelines cannot move
    their peers' cwd, and a delimited ``$(…)`` body cannot move its parent's:
    the parent resumes in the cwds it had when the body opened. Any other
    closing parenthesis widens to every earlier cwd. A future checkout
    survives only success-dependent && control. Metadata/environment
    redirects participate in the same target set.
    Approval (``strict``) resolves tree effects for Git guard presence and
    raises ValueError on nonliteral/unknown targets for the caller's existing
    gate. Execution preparation (``strict=False``) reads only segment bounds,
    subcommands and execution cwds: tool calls take no workspace lease, so it
    neither derives tree effects nor refuses a target the shell resolves.

    ``certain_leading_cd`` drops the original cwd after a leading cd that
    :func:`leading_cd_enters` proves. The Git guard sets it: it decides on the
    call's own worker right before the command starts. Read preparation keeps
    both candidates and replays the actual cwd at run time.
    """
    scopes = []
    cwds = {os.path.realpath(base or os.getcwd())}
    fallback_cwds = set()
    future_checkouts = set()
    bound_environment = dict(environment)
    # Literal heredoc bodies are stdin bytes, never segments: a commit-message
    # line such as `(git worktree remove …` must not parse as a Git command.
    # The view preserves offsets, so scope starts still index *command*.
    command = _literal_heredoc_body_view(command)
    spans = _split_pipeline_spans(command)
    repeated, reachable = repeated_cwd_changes(command, spans), set()
    openers = literal_loop_body_openers(command, spans)
    # The splitter cuts an unquoted `$(` body into spans of its own; the
    # scanner pairs each with the parenthesis that really closes it.
    substitutions = [(opening, closing) for opening, closing, _body
                     in command_substitution_bodies(command) or ()
                     if command.startswith('$(', opening)]
    substitution = None  # (body end, parent cwds, parent fallback cwds)
    for index, (start, end) in enumerate(spans):
        source = command[start:end].strip()
        # ``2>&1``/``2>/dev/null`` only move Git's own output; they cannot change
        # its effect. Classifying them as unknown arguments made every
        # ``git worktree add … 2>&1 | tail`` a whole-checkout writer.
        discardable = command_segment_discardable_redirect_split(source)
        if discardable is not None:
            source = discardable[0]
        previous_separator, previous_closes, opens = (
            pipeline_span_separator(command[spans[index - 1][1]:start]) if index else ('', False, False))
        if opens:
            previous_separator = ''  # The first command of a subshell or substitution.
        next_separator = (pipeline_span_separator(command[end:spans[index + 1][0]])[0]
                          if index + 1 < len(spans) else '')
        if previous_separator != '&&' or previous_closes:
            future_checkouts.clear()
        resumed = False
        if substitution is not None and start >= substitution[0]:
            body_end, cwds, fallback_cwds = substitution
            substitution = None
            resumed = ')' not in command[body_end:start]
        if substitution is None:
            body_end = next((closing for opening, closing in substitutions
                             if opening < start < closing), None)
            if body_end is not None:
                substitution = (body_end, set(cwds), set(fallback_cwds))
        if (previous_closes and not resumed) or previous_separator in {';', '||'}:
            cwds.update(fallback_cwds)
        reachable |= repeated.get(index, set())
        cwds |= reachable
        if strict and UNRESOLVED_CWD in cwds:
            raise ValueError('Cwd requires unresolved shell control')
        source = peeled_command(source)
        if not source:
            continue
        try:
            bare_words = list(command_segment_words(source))
        except ValueError as exc:
            if strict:
                raise ValueError('Unterminated shell argument') from exc
            bare_words = []
        words = list(bare_words)
        segment_environment = dict(bound_environment)
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            key, value = words.pop(0).split('=', 1)
            segment_environment[key] = value
        for _ in range(_MAX_WRAPPER_DEPTH):
            # Privilege and process wrappers (`timeout 900 git switch`) run
            # Git with this segment's cwd and environment.
            words = unwrap_process_wrappers(words)
            if words[:1] != ['env']:
                break
            # `env` only edits the child's environment; its own options are
            # not Git arguments. `env -u NAME git …` still runs Git, so the
            # scope must be resolved for the wrapped program. An option whose
            # meaning is not understood keeps the remainder opaque.
            words = words[1:]
            while words:
                word = words[0]
                if _ENV_ASSIGNMENT_GUARD_RE.match(word):
                    key, value = words.pop(0).split('=', 1)
                    segment_environment[key] = value
                    continue
                if word in ('-i', '--ignore-environment', '-0', '--null'):
                    words.pop(0)
                    continue
                if word in ('-u', '--unset', '-C', '--chdir'):
                    if len(words) < 2:
                        break
                    words.pop(0)
                    words.pop(0)
                    continue
                if word.startswith('--unset=') or word.startswith('--chdir='):
                    words.pop(0)
                    continue
                break
        if not words:
            bound_environment.update(segment_environment)
        if words[:1] == ['export'] and _static_segment(source):
            for word in words[1:]:
                if _ENV_ASSIGNMENT_GUARD_RE.match(word):
                    key, value = word.split('=', 1)
                    bound_environment[key] = value
        change = shell_cwd_change(source)
        if change is not None:
            targets = (loop_body_cd_targets(openers.get(index))
                       or cwd_change_targets(cwds, change, segment_environment.get('CDPATH', '')))
            if UNRESOLVED_CWD in targets:
                if strict:
                    raise ValueError('Cwd requires unresolved shell control')
                future_checkouts.clear()
            if (certain_leading_cd and change.certain and next_separator != '|'
                    and UNRESOLVED_CWD not in targets
                    and leading_cd_enters(command, start, change.target, targets)):
                cwds = targets
                continue
            fallback_cwds.update(cwds)
            if previous_separator == '|' or next_separator == '|':
                pass  # cd executes in a pipeline subshell.
            elif next_separator == '&&' and previous_separator in {'', ';', '&&'}:
                cwds = targets
            else:
                cwds.update(targets)  # After `a || cd X`, `&& b` also runs when `a` succeeds.
        elif words and os.path.basename(words[0]) == 'git':
            paths, future_paths = set(), set()
            creates_checkout = False
            execution_cwds = set()
            presence_paths = set()
            lifecycle_presence = False
            for cwd in cwds:
                try:
                    (resolved, destination, ambiguous, execution_cwd, subcommand,
                     subcommand_index, directory_steps) = _git_targets(words, cwd, segment_environment)
                    if shell_source_has_expansion(source):
                        if (subcommand == 'worktree'
                                and words[subcommand_index + 1:subcommand_index + 2] in (['remove'], ['move'])):
                            raise ValueError('Git worktree lifecycle requires literal operands')
                        # Approval (strict) keeps refusing every expansion.
                        if strict or _expansion_may_select_target(source):
                            raise ValueError('Git target requires unresolved shell expansion')
                except ValueError:
                    if strict:
                        raise
                    resolved, destination, ambiguous, execution_cwd, subcommand, subcommand_index = {cwd}, None, True, cwd, '', len(words)
                    directory_steps = ()
                execution_cwds.add(execution_cwd)
                if ambiguous:
                    resolved.add(cwd)
                if destination and _static_segment(source) and not os.path.lexists(destination):
                    creates_checkout = True
                    if next_separator == '&&':
                        future_checkouts.add(destination)
                    paths.update(resolved | {destination})
                    continue
                paths.update(resolved)
                future_paths.update(resolved & future_checkouts)
                if not strict:
                    continue

                effects = (None if ambiguous or shell_source_has_expansion(source) else
                           _git_standalone_effects(words, execution_cwd, subcommand,
                                                   subcommand_index, segment_environment))
                if effects is None:
                    effects = _git_lease_effects(
                        words, execution_cwd, subcommand, subcommand_index, resolved, ambiguous,
                        segment_environment)
                paths.update(path for kind, path in effects if kind == 'tree')
                lifecycle = (git_worktree_lifecycle(words[subcommand_index + 1:])
                             if subcommand == 'worktree' else None)
                # Keep metadata exclusion without treating activity at its
                # owner as evidence that the retired tree is in use. Other
                # verbs (e.g. reset --soft) still protect ref/index consumers.
                if lifecycle is not None:
                    lifecycle_presence = True
                    targets = _worktree_lifecycle_paths(lifecycle, execution_cwd)
                    for kind, path in effects:
                        if kind != 'tree':
                            continue
                        # A missing lifecycle target is that exact tree, not
                        # the containing checkout discovered by walking up.
                        identity = None if path in targets else _checkout_identity(path)
                        presence_paths.add(identity[0] if identity else path)
            scopes.append(GitCommandScope(
                start, end, _classify_git_invocation(words[1:]),
                tuple(sorted(paths)), tuple(sorted(future_paths)), creates_checkout,
                tuple(sorted(execution_cwds)), subcommand,
                tuple(sorted(presence_paths)) if lifecycle_presence else None,
                directory_steps=directory_steps))
        elif words:
            # Opaque code cannot attest that a future .git identity survived.
            future_checkouts.clear()
    return tuple(scopes)
