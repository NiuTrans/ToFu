"""Resolve Git effects once for approval and checkout writer admission.

Entry point: git_command_scopes(command, base, environment). Classification
comes from command_analysis; checkout identity comes from workspace_lease.
This module only parses literal shell/Git arguments and reads path metadata.
It never executes Git, authorizes writes, owns locks, or caches task identity.
"""
from dataclasses import dataclass
import os
import re

from .command_analysis import (
    _ENV_ASSIGNMENT_GUARD_RE, _GIT_GUARD_GLOBAL_BARE_FLAGS,
    _GIT_GUARD_GLOBAL_VALUE_OPTS, _classify_git_invocation,
    _git_worktree_add_destination, _mask_quoted_literals,
    _split_pipeline_spans, _unwrap_command_parts,
    command_segment_words, shell_source_has_expansion,
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

    lease_scopes: tuple[tuple[str, str], ...] = ()

    def writer_claims(self):
        return tuple(claim for claim in self.lease_scopes
                     if claim[1] not in self.future_paths)

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
    return os.path.realpath(os.path.join(base, path))


def _static_segment(source):
    return not (shell_source_has_expansion(source)
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
            elif option in {'--exec-path', '--namespace', '--config-env'}:
                # Dynamic configuration can enable callbacks, including after
                # literal disabling overrides. Both config-env forms widen.
                ambiguous = True
            index += 2
        elif option.startswith('-C') and len(option) > 2:
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
            ambiguous, cwd, words[index] if index < len(words) else '', index)


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


def _git_config_has_helpers(filename, helper_keys):
    """Read bounded config without includes/subprocesses; uncertainty widens.

    Git accepts a key immediately after a section header, and boolean keys
    without '='. Inspect keys rather than values so those forms cannot hide
    helpers or include paths. This is deliberately not a full Git parser:
    unsupported syntax/continuations cannot prove absence of callbacks.
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
            line = line[section.end():]
            if not line or line.startswith(('#', ';')):
                continue
        key = re.match(r'([A-Za-z][A-Za-z0-9-]*)\s*(?:=|[#;]|$)', line)
        if not key or key[1].lower() in keys:
            return True
    return False


def _git_executable_helpers(paths, configs, environment, subcommand):
    """Bounded conservative evidence for callbacks outside native Git effects."""

    if _git_inline_include(configs):
        return True  # Includes may override even an earlier hooksPath=/dev/null.
    if any(environment.get(key) for key in {
            'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG',
            'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_GLOBAL'}):
        return True
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
    for path in paths:
        identity = _checkout_identity(path)
        if identity:
            common = os.fspath(identity[1])
            configuration.add(os.path.join(common, 'config'))
            if not hooks_disabled and any(os.access(os.path.join(common, 'hooks', hook), os.X_OK)
                                          for hook in hook_names):
                return True
    helper_keys = 'hookspath|fsmonitor|gpgsign|program|command|clean|smudge|process|path|worktreeconfig'
    if subcommand == 'pull':
        helper_keys += '|helper'  # Credential callbacks are not invoked by local commit/reset.
    return any(_git_config_has_helpers(filename, helper_keys) for filename in configuration)


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


def _git_lease_effects(words, cwd, subcommand, position, paths, ambiguous, environment):
    """Lease effects are separate from destructive-operation approval risk."""
    args = words[position + 1:]

    configs = _git_configs(words, position)
    unresolved_include = _git_inline_include(configs)
    if subcommand == 'worktree' and args[:1] in (['remove'], ['move']):
        operands = []
        options_done = False
        for value in args[1:]:
            if value == '--' and not options_done:
                options_done = True
            elif not options_done and value.startswith('-'):
                if value not in {'-f', '--force'}:
                    raise ValueError('Unresolved Git worktree lifecycle option')
            else:
                operands.append(value)
        required = 1 if args[0] == 'remove' else 2
        if len(operands) != required or any(value.startswith('~') for value in operands):
            raise ValueError('Unresolved Git worktree lifecycle operands')
        source = _literal_path(cwd, operands[0])
        if not os.path.isabs(operands[0]):
            owner = _checkout_identity(source)
            invocation = _checkout_identity(cwd)
            if (ambiguous or not owner or owner[0] != source or not invocation
                    or owner[1] != invocation[1]):
                raise ValueError('Unresolved Git worktree lifecycle source; use an explicit absolute path')
        helper_effect = ambiguous or _git_executable_helpers(paths, configs, environment, subcommand)
        return ((_git_tree_claims(paths) if helper_effect else
                 {('metadata', path) for path in paths})
                | {('tree', _literal_path(cwd, value)) for value in operands})
    if ambiguous or unresolved_include:
        return _git_tree_claims(paths)

    if (git_metadata_mutation_mode(subcommand, args)
            and not _git_executable_helpers(paths, configs, environment, subcommand)):
        return {('metadata', path) for path in paths}
    metadata_only = subcommand in {
        'branch', 'tag', 'update-ref', 'symbolic-ref', 'pack-refs',
        'write-tree',
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
                config = os.path.join(common, 'config')
                if _git_config_has_helpers(config, 'hookspath|gpgsign|include|path|worktreeconfig'):
                    metadata_only = False
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
    if subcommand == 'add':
        # Refresh reads stat metadata without staging/filtering file contents.
        metadata_only = ('core.fsmonitor=false' in configs
                         and args[:1] == ['--refresh']
                         and all(not value.startswith('-') or value in {'--refresh', '--', '-q', '--quiet'}
                                 for value in args))
    if metadata_only and 'core.hooksPath=/dev/null' not in configs:
        if any(environment.get(key) for key in {
                'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG',
                'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_GLOBAL'}):
            metadata_only = False
        home = environment.get('HOME', '')
        config_paths = ['/etc/gitconfig']
        if home:
            config_paths.extend([os.path.join(home, '.gitconfig'), os.path.join(
                environment.get('XDG_CONFIG_HOME') or os.path.join(home, '.config'), 'git', 'config')])
        for config in config_paths:
            if _git_config_has_helpers(config, 'hookspath|gpgsign|path'):
                metadata_only = False
    if (metadata_only and subcommand in {'add', 'write-tree'}
            and _git_executable_helpers(paths, configs, environment, subcommand)):
        metadata_only = False  # Refresh/cache-tree writes can run post-index-change.
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
            lease_scopes = set()
            for cwd in cwds:
                try:
                    resolved, destination, ambiguous, execution_cwd, subcommand, subcommand_index = _git_targets(words, cwd, segment_environment)
                    if shell_source_has_expansion(source):
                        if (subcommand == 'worktree'
                                and words[subcommand_index + 1:subcommand_index + 2] in (['remove'], ['move'])):
                            raise ValueError('Git worktree lifecycle requires literal operands')
                        raise ValueError('Git target requires unresolved shell expansion')
                except ValueError as error:
                    if strict or 'lifecycle' in str(error):
                        raise
                    resolved, destination, ambiguous, execution_cwd, subcommand, subcommand_index = {cwd}, None, True, cwd, '', len(words)
                execution_cwds.add(execution_cwd)
                if ambiguous:
                    resolved.add(cwd)
                if destination and _static_segment(source) and not os.path.lexists(destination):
                    creates_checkout = True
                    if next_separator == '&&':
                        future_checkouts.add(destination)
                    # Creation changes Git administration and owns the future
                    # destination throughout subsequent success-dependent work.
                    # Native Git lockfiles do not exclude cooperative raw Edit.
                    configs = _git_configs(words, subcommand_index)
                    helper_effect = _git_executable_helpers(
                        resolved, configs, segment_environment, subcommand)
                    effects = (_git_tree_claims(resolved) if helper_effect else
                               {('metadata', path) for path in resolved}) | {('tree', destination)}
                    lease_scopes.update(effects)
                    paths.update(resolved | {destination})
                    continue
                paths.update(resolved)
                future_paths.update(resolved & future_checkouts)

                effects = _git_lease_effects(
                    words, execution_cwd, subcommand, subcommand_index, resolved, ambiguous, segment_environment)
                lease_scopes.update(effects)
                paths.update(path for kind, path in effects if kind == 'tree')
            scopes.append(GitCommandScope(
                start, end, _classify_git_invocation(words[1:]),
                tuple(sorted(paths)), tuple(sorted(future_paths)), creates_checkout,
                tuple(sorted(execution_cwds)), subcommand, tuple(sorted(lease_scopes))))
        elif words:
            # Opaque code cannot attest that a future .git identity survived.
            future_checkouts.clear()
        previous_separator = next_separator
    return tuple(scopes)
