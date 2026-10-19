"""run_command tool + its command-execution machinery.

Extracted from ``lib/project_mod/tools.py`` (2026-06-24) to slim that
dispatch facade.  This module owns the entire shell-execution subsystem:

  - command output cleanup for LLM consumption (``_clean_command_output``)
  - destructive / catastrophic-command guards + rm-trash wrapping
  - project-file snapshot/diff + change recording
  - ``tool_run_command`` and its simple + interactive runners
  - process-tree kill, grep hardening, stdin-reader detection

``tools.py`` re-exports the dispatch-facing symbols from here for backward
compat, so external callers (``from lib.project_mod.tools import
tool_run_command``) are unaffected.  There is no dependency back on
``tools.py`` — the dispatch layer depends on this module, not the reverse.
"""

import os
from pathlib import Path
import re as _re  # alias used by the retained _maybe_wrap_rm_with_trash
import shlex
import stat
import subprocess
import time

from contextvars import ContextVar

# A synchronous outer dispatcher and nested runner share one worker-owned
# receipt. Background adapters install it on their actual executing thread.
_owned_command_write = ContextVar('owned_command_write', default=None)


def _owned_command_quiesced():
    """Runner-only proof: a spawned body and supervised descendants retired."""
    state = _owned_command_write.get()
    if state is not None:
        state['quiesced'] = True

from lib.log import get_logger
from lib.project_mod.config import (
    IGNORE_DIRS,
    MAX_COMMAND_OUTPUT,
    MAX_COMMAND_TIMEOUT,
    SHELL_PREFIX,
)
from lib.project_mod.modifications import _record_modification
from lib.project_mod.snapshot_content import (
    UNDO_BATCH_CONTENT_BYTES, anchored_directory, anchored_parent, file_fingerprint,
)

# ── Pure command analysis lives in command_analysis.py (2026-07-11) ──
# Only the symbols this module uses internally or that `tools.py` / tests
# still import from here are re-imported; everything else is imported from
# `lib.project_mod.command_analysis` directly by its consumers.
from lib.project_mod.command_analysis import (  # noqa: F401
    _bound_scan_segments,

    command_segment_formatter_writes,
    _clean_command_output,
    _extract_device_ids,
    _extract_progress_label,
    _extract_progress_pct,
    _extract_write_targets,
    _filter_changes_by_targets,
    _format_cuda_device_range,
    _grep_filesystem_segment,
    _has_unquoted_shell_metachars,
    _is_catastrophic_delete,
    _is_dangerous_command,
    _is_destructive_command,
    _lease_free_git_command,
    _lease_free_git_segment,
    harden_git_inspections,
    _line_fingerprint,
    _mask_quoted_literals,
    _split_pipeline,
    _split_pipeline_spans,
    _unbounded_recursive_scan_target,
)
from lib.project_mod.command_heredoc import (
    _without_descriptor_duplications, command_admission_view,
)
from lib.project_mod.command_setup import command_setup_view, command_cwd_setup_refusal
from lib.project_mod.tool_admission import recorded_command_source
from lib.project_mod.grep_redirect import plan_grep_redirect

logger = get_logger(__name__)


from lib.project_mod.workspace_lease import workspace_lease_fds
_TIMEOUT_SCAN_ARGS = {}


_DIRECTORY_LISTING_SHORT_FLAGS = frozenset('aAFlh1')
_DIRECTORY_LISTING_LONG_FLAGS = frozenset({
    '--all', '--almost-all', '--classify', '--group-directories-first',
    '--human-readable',
})
_LS_FAST_PATH_EXECUTABLES = frozenset({'ls', '/bin/ls', '/usr/bin/ls'})
_FIND_FAST_PATH_EXECUTABLES = frozenset({
    'find', '/bin/find', '/usr/bin/find',
})
_PYTHON_COMMAND_EXECUTABLE_RE = _re.compile(
    r'^python(?:\d+(?:\.\d+)*)?(?:\.exe)?$', _re.IGNORECASE)


def _looks_like_python_command(command):
    """Cheap import gate for the optional Python cache planner."""
    try:
        tokens = shlex.split(str(command or '').strip(), posix=True)
    except ValueError:
        return False
    return bool(
        tokens
        and _PYTHON_COMMAND_EXECUTABLE_RE.fullmatch(
            os.path.basename(tokens[0]))
    )


def _has_dynamic_shell_syntax(source):
    """Whether *source* needs shell expansion/control semantics.

    A fast-path parser may consume quoted literals, including a quoted glob
    used as a ``find -name`` argument. It must not consume separators,
    redirections, substitutions, or unquoted glob/brace expansion: those stay
    with the real shell. Backslash-escaped characters are static literals.
    """
    in_single = False
    in_double = False
    index = 0
    while index < len(source):
        char = source[index]
        if char == '\\' and not in_single and index + 1 < len(source):
            index += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double and char in '\n\r':
            return True
        elif not in_single and char in '$`':
            return True
        elif not in_single and not in_double and char in ';&|<>(){}*?[]#':
            return True
        index += 1
    return False


def _safe_fast_path_relative_path(path):
    value = str(path or '')
    if (not value or os.path.isabs(value) or value.startswith('~')
            or any(part == '..'
                   for part in value.replace('\\', '/').split('/'))):
        return None
    return value


def parse_safe_directory_listing_command(command):
    """Return a bounded directory-listing request for a plain ``ls`` command.

    The interception is intentionally narrower than shell parsing: one command,
    one relative path, and presentation-only flags. Pipelines, expansions,
    recursion, absolute/parent traversal, and multiple operands keep using the
    ordinary shell so redirect/exit-code semantics are never silently changed.
    """
    source = str(command or '').strip()
    if not source or _has_dynamic_shell_syntax(source):
        return None
    try:
        tokens = shlex.split(source, posix=True)
    except ValueError:
        return None
    if not tokens or tokens[0] not in _LS_FAST_PATH_EXECUTABLES:
        return None

    paths = []
    show_hidden = False
    options_done = False
    for token in tokens[1:]:
        if not options_done and token == '--':
            options_done = True
            continue
        if not options_done and token.startswith('--'):
            option, separator, value = token.partition('=')
            if option == '--color' and (not separator or value in {
                    'always', 'auto', 'never'}):
                continue
            if option not in _DIRECTORY_LISTING_LONG_FLAGS or separator:
                return None
            if option in {'--all', '--almost-all'}:
                show_hidden = True
            continue
        if not options_done and token.startswith('-') and token != '-':
            flags = token[1:]
            if not flags or any(flag not in _DIRECTORY_LISTING_SHORT_FLAGS
                                for flag in flags):
                return None
            if 'a' in flags or 'A' in flags:
                show_hidden = True
            continue
        paths.append(token)

    if len(paths) > 1:
        return None
    path = paths[0] if paths else '.'
    path = _safe_fast_path_relative_path(path)
    if path is None:
        return None
    return {'path': path, 'show_hidden': show_hidden}


def _parse_find_tail_ops(tail_segments):
    """Parse whitelisted pipe tails (``head``/``wc -l``/``sort``) into ops.

    Returns an ordered op list — ``('head', K)``, ``('sort',)``, ``('wc',)`` —
    or None when any segment falls outside the provably-streamable set.
    """
    ops = []
    for segment in tail_segments:
        if _has_dynamic_shell_syntax(segment):
            return None
        try:
            tokens = shlex.split(segment.strip(), posix=True)
        except ValueError:
            return None
        if not tokens:
            return None
        head, args = tokens[0].split('/')[-1], tokens[1:]
        if head == 'head':
            count = 10
            index = 0
            while index < len(args):
                tok = args[index]
                if tok == '-n' and index + 1 < len(args) \
                        and args[index + 1].lstrip('-').isdigit():
                    count = int(args[index + 1])
                    index += 2
                    continue
                if tok.startswith('-n') and tok[2:].isdigit():
                    count = int(tok[2:])
                    index += 1
                    continue
                if re_fullmatch_int(tok):
                    count = int(tok[1:])
                    index += 1
                    continue
                return None
            ops.append(('head', count))
        elif head == 'wc' and args == ['-l']:
            ops.append(('wc',))
        elif head == 'sort' and not args:
            ops.append(('sort',))
        else:
            return None
    return ops


def re_fullmatch_int(token):
    """``head -5`` numeric form (a leading dash followed by digits)."""
    return len(token) > 1 and token[0] == '-' and token[1:].isdigit()


def parse_safe_file_find_command(command):
    """Translate the safe, index-compatible subset of GNU ``find``.

    Accepted shapes have one relative root; basename ``-name``/``-iname``
    predicates (singly or as a ``-name a -o -name b`` union of one case
    mode); optional ``-type f``/``-type d``; optional ``-maxdepth`` /
    ``-mindepth`` bounds; and an optional trailing ``-print``. A pipeline of
    whitelisted stream tails is also accepted: ``| head [-n K|-K]``,
    ``| wc -l``, ``| sort`` in any combination. Complex expressions,
    mutation, dynamic shell input, and unrecognized tail commands
    deliberately return ``None`` and retain the real ``find`` plus its
    existing segment timeout.
    """
    source = str(command or '').strip()
    if not source:
        return None
    spans = _split_pipeline_spans(source)
    if not spans:
        return None
    find_source = source[spans[0][0]:spans[0][1]].strip()
    tail_ops = None
    if len(spans) > 1:
        tail_segments = [source[s:e] for s, e in spans[1:]]
        tail_ops = _parse_find_tail_ops(tail_segments)
        if tail_ops is None:
            return None
    if _has_dynamic_shell_syntax(find_source):
        return None
    try:
        tokens = shlex.split(find_source, posix=True)
    except ValueError:
        return None
    if not tokens or tokens[0] not in _FIND_FAST_PATH_EXECUTABLES:
        return None

    index = 1
    path = '.'
    if index < len(tokens) and not tokens[index].startswith('-'):
        path = tokens[index]
        index += 1
    path = _safe_fast_path_relative_path(path)
    if path is None:
        return None

    patterns = []
    case_modes = set()
    entry_type = 'any'
    max_depth = None
    min_depth = 0
    saw_predicate = False
    pending_union = False
    while index < len(tokens):
        token = tokens[index]
        if token in {'-name', '-iname'}:
            if index + 1 >= len(tokens):
                return None
            pattern = tokens[index + 1]
            if not pattern or '/' in pattern or '\\' in pattern:
                return None
            patterns.append(pattern)
            case_modes.add(token == '-name')
            saw_predicate = True
            pending_union = False
            index += 2
            continue
        if token == '-o':
            # Only the ``-name a -o -name b`` union: an -o must sit between
            # two name predicates (verified on the next iteration).
            if not saw_predicate or pending_union:
                return None
            if index + 2 >= len(tokens) \
                    or tokens[index + 1] not in {'-name', '-iname'}:
                return None
            pending_union = True
            index += 1
            continue
        if token == '-type':
            if entry_type != 'any' or index + 1 >= len(tokens) \
                    or tokens[index + 1] not in ('f', 'd'):
                return None
            entry_type = tokens[index + 1]
            index += 2
            continue
        if token in ('-maxdepth', '-mindepth'):
            if index + 1 >= len(tokens) or not tokens[index + 1].isdigit():
                return None
            if token == '-maxdepth':
                if max_depth is not None:
                    return None
                max_depth = int(tokens[index + 1])
            else:
                if min_depth:
                    return None
                min_depth = int(tokens[index + 1])
            index += 2
            continue
        if token == '-print' and index == len(tokens) - 1:
            index += 1
            continue
        return None
    if pending_union or len(case_modes) > 1:
        return None
    if max_depth is not None and max_depth < min_depth:
        # GNU evaluates both predicates; the result is provably empty unless
        # the root itself qualifies at depth 0 — keep native for the edge.
        return None
    case_sensitive = True if not case_modes else case_modes.pop()
    return {
        'path': path,
        'patterns': patterns or ['*'],
        # Legacy single-pattern alias (remote delegate, older callers).
        'pattern': (patterns or ['*'])[0],
        'case_sensitive': case_sensitive,
        'entry_type': entry_type,
        'max_depth': max_depth,
        'min_depth': min_depth,
        'tail': tail_ops,
        'max_results': 500,
        # The remote project_find_files delegate covers only the legacy
        # subset (single pattern, files only, unbounded depth, no tail).
        'remote_safe': bool(
            len(patterns) <= 1 and entry_type == 'f'
            and max_depth is None and min_depth == 0 and tail_ops is None),
    }


def _timeout_scan_wrapper_args(timeout_binary):
    """Probe one timeout binary and return only options it advertises.

    GNU coreutils before 8.23 has no ``--verbose``; BusyBox variants may only
    expose short signal/kill flags. The duration-only form is the safe final
    fallback, so a compatibility optimization can never make ``find`` fail at
    process startup merely because of the host's timeout version.
    """
    cached = _TIMEOUT_SCAN_ARGS.get(timeout_binary)
    if cached is not None:
        return cached
    try:
        probe = subprocess.run(
            [timeout_binary, '--help'], capture_output=True, text=True,
            timeout=2, check=False)
        help_text = f'{probe.stdout}\n{probe.stderr}'
    except (OSError, subprocess.SubprocessError):
        help_text = ''
    args = []
    if '--verbose' in help_text:
        args.append('--verbose')
    if '--signal' in help_text:
        args.append('--signal=TERM')
    elif _re.search(r'(?m)(?:^|[\s,])-s(?:[\s,=]|$)', help_text):
        args.extend(('-s', 'TERM'))
    if '--kill-after' in help_text:
        args.append('--kill-after=2s')
    elif _re.search(r'(?m)(?:^|[\s,])-k(?:[\s,=]|$)', help_text):
        args.extend(('-k', '2s'))
    result = tuple(args)
    if len(_TIMEOUT_SCAN_ARGS) >= 4:
        _TIMEOUT_SCAN_ARGS.clear()
    _TIMEOUT_SCAN_ARGS[timeout_binary] = result
    return result


_SENSITIVE_ENV_SUFFIX_RE = _re.compile(
    r'(?:^|_)(?:API_KEYS?|TOKENS?|SECRETS?|PASSWORDS?|PASSWDS?|'
    r'PRIVATE_KEY|ACCESS_KEY|AUTH)$')
_SENSITIVE_ENV_EXACT = frozenset({
    'AWS_ACCESS_KEY_ID',
    'DATABASE_URL',
    'GOOGLE_APPLICATION_CREDENTIALS',
    'MYSQL_PWD',
    'PGPASSWORD',
    'REDIS_URL',
    'SSH_AUTH_SOCK',
})


def _is_sensitive_inherited_env(name):
    upper = str(name or '').upper()
    return (upper in _SENSITIVE_ENV_EXACT
            or bool(_SENSITIVE_ENV_SUFFIX_RE.search(upper)))


def _get_cmd_env(cwd=None, credential_env=None, strip_credential_vars=None,
                 owner_user_id=None, project_root=None):
    """Build a credential-scoped child environment for one command.

    Persistent CLI paths/caches share the MCP launch contract. The nearest
    valid .venv between cwd and the explicit project root selects Python;
    server-only Python/conda activation and ambient secrets do not propagate.
    Skill and explicitly selected vault capabilities retain their admission.
    Restricted callers receive workspace-local install/cache directories.
    No shell startup script is sourced and no package is installed here.
    """
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    if os.name != 'nt':
        env.setdefault('TERM', 'dumb')

    # Parent-process credentials are machine capabilities, not ordinary shell
    # configuration. Strip common secret names even when they are not yet in
    # the app vault; otherwise an ambient GITHUB_TOKEN / LLM_API_KEY would
    # bypass the explicit per-command capability. Skill bindings and selected
    # vault entries are re-added below after this default-deny pass.
    for var in list(env):
        if _is_sensitive_inherited_env(var):
            env.pop(var, None)

    # The caller resolved this additional set from the same strict vault
    # snapshot as the selected overlay. No vault I/O happens here.
    for var in strip_credential_vars or ():
        env.pop(var, None)

    # Skill-scoped bindings keep their separate declaration/eligibility path.
    try:
        from lib.skills.env import exec_env_overlay
        env.update(exec_env_overlay(
            project_path=cwd, owner_user_id=owner_user_id))
    except Exception as _env_e:
        logger.debug('[run_command] skill env overlay skipped: %s', _env_e)

    # Only values resolved for THIS run_command invocation enter the child.
    if credential_env:
        env.update(credential_env)

    # ── Block user-site-packages (~/.local/) writes & reads ──
    env['PYTHONNOUSERSITE'] = '1'
    env['PIP_USER'] = '0'
    env.pop('PIP_TARGET', None)  # don't redirect pip elsewhere

    # ── Hard guard against polluting the SERVER's own environment ──
    # The #1 contamination incident: a benchmarked agent (or any user) runs
    # ``pip install -e .`` in a workspace; if no per-workspace virtualenv is
    # active, pip falls through to the conda env that launched the tofu
    # server and writes an editable ``__editable__*.pth`` / ``.egg-link``
    # straight into the SERVER's site-packages — silently hijacking
    # ``import pytest`` / ``import sphinx`` / ``import matplotlib`` for the
    # whole env (see scrub_envs.py and the swebench-env-isolation memories).
    #
    # ``PIP_REQUIRE_VIRTUALENV=1`` makes pip REFUSE to install unless a real
    # virtualenv (one with a ``pyvenv.cfg`` → ``sys.prefix != base_prefix``)
    # is active. A conda env is NOT a virtualenv by this test, so the tofu
    # env is protected, while a per-task ``.venv/`` (activated below) is
    # allowed. This converts a silent cross-env corruption into a loud,
    # local "Could not find an activated virtualenv (required)" error in the
    # agent's own command output — which is the correct, recoverable signal.
    #
    # Protect by default; a self-hosted user who genuinely wants the agent to
    # install into the server's own (conda/base) environment can opt out by
    # launching the server with ``TOFU_ALLOW_GLOBAL_PIP=1``.
    if os.environ.get('TOFU_ALLOW_GLOBAL_PIP', '').strip().lower() not in (
            '1', 'true', 'yes', 'on'):
        env['PIP_REQUIRE_VIRTUALENV'] = '1'
    else:
        env.pop('PIP_REQUIRE_VIRTUALENV', None)

    # ── Strip server-process-only env vars that pollute subprocess ──
    # These come from the conda env that launched the tofu server.
    # The agent's subprocess in a workspace MUST NOT inherit them or
    # `python` / `pip` will resolve to the wrong env. CONDA_PREFIX is the
    # critical one: pip/python use it to locate the install target, so
    # leaving it set points writes back at the server's env even after the
    # other CONDA_* markers are gone.
    for key in ('CONDA_DEFAULT_ENV', 'CONDA_PROMPT_MODIFIER',
                'CONDA_SHLVL', 'CONDA_EXE', '_CE_CONDA', '_CE_M',
                'CONDA_PREFIX', 'CONDA_PREFIX_1', 'CONDA_PREFIX_2'):
        env.pop(key, None)
    # LD_LIBRARY_PATH from server (sglang env) breaks Python ABI in
    # workspace conda envs (different libpython.so). Strip it.
    env.pop('LD_LIBRARY_PATH', None)
    # Server's PYTHONPATH (spark-3.0 jars etc.) interferes with workspace
    # source imports. Reset.
    env.pop('PYTHONPATH', None)

    # Build from the explicit project anchor, so sticky subdirectories keep
    # the same Python packages. Never search above this authorized boundary.
    from lib.tools.process_environment import apply_tool_paths, validate_environment
    root = os.path.realpath(project_root or cwd) if (project_root or cwd) else None

    # Managed chat output can live beneath the source checkout's data directory.
    # Plain `git status` must not discover that server repository as its own.
    # Explicit SDK/project bindings outside this namespace are unchanged.
    if root:
        from pathlib import Path
        from runtime_path_policy import get_runtime_base
        managed_base = Path(get_runtime_base()) / 'data' / 'conversation_workspaces'
        relative = Path(root).relative_to(managed_base) if Path(root).is_relative_to(managed_base) else None
        if relative is not None and len(relative.parts) >= 2:
            ceiling = str(managed_base / relative.parts[0])
            env['GIT_CEILING_DIRECTORIES'] = os.pathsep.join(filter(None, [
                env.get('GIT_CEILING_DIRECTORIES'), ceiling]))
            for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR'):
                env.pop(key, None)
    from lib.project_mod.abs_path_guard import is_restricted
    restricted = is_restricted()
    if restricted:
        # Remote commands cannot reuse a machine-wide installed-tool directory.
        if cwd:
            for key in ('UV_CACHE_DIR', 'XDG_CACHE_HOME', 'npm_config_cache',
                        'PIP_CACHE_DIR', 'XDG_DATA_HOME', 'UV_PYTHON_INSTALL_DIR',
                        'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR', 'CARGO_HOME',
                        'npm_config_prefix', 'NPM_CONFIG_PREFIX'):
                env.pop(key, None)
            env['HOME'] = os.path.realpath(cwd)
        cache_root = os.path.join(root, '.tofu', 'tool-cache') if root else None
    else:
        cache_root = os.environ.get('TOFU_MCP_CACHE_DIR') or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'data', 'mcp-cache')
    apply_tool_paths(env, cache_root=cache_root)
    # A server activation marker does not identify the command's interpreter.
    env.pop('VIRTUAL_ENV', None)
    env.pop('PYTHONHOME', None)
    if cwd and root:
        current = os.path.realpath(cwd)
        if os.path.commonpath([current, root]) != root:
            raise ValueError('Command directory is outside its environment root')
        while True:
            virtualenv = os.path.join(current, '.venv')
            venv_bin = os.path.join(virtualenv, 'Scripts' if os.name == 'nt' else 'bin')
            if os.path.isfile(os.path.join(virtualenv, 'pyvenv.cfg')):
                python = os.path.join(venv_bin, 'python.exe' if os.name == 'nt' else 'python')
                if restricted and os.path.commonpath([os.path.realpath(virtualenv), root]) != root:
                    raise ValueError('Project .venv escapes its authorized environment root')
                if not os.path.isfile(python) or not os.access(python, os.X_OK):
                    from lib.project_mod.python_unavailable import block_unavailable_python
                    block_unavailable_python(env, cache_root=cache_root)
                    break
                env['VIRTUAL_ENV'] = virtualenv
                env['PATH'] = venv_bin + os.pathsep + env.get('PATH', os.defpath)
                env['PYTHONPATH'] = os.pathsep.join(
                    [os.path.join(current, 'src'), current]
                    if os.path.isdir(os.path.join(current, 'src')) else [current])
                break
            if current == root:
                break
            current = os.path.dirname(current)

    # Portable sandbox env-jail (restricted/agent-run principals only):
    #   point HOME/TMPDIR inside the workspace and prepend the rm/mv shim dir
    #   to PATH. Applied LAST so the shim dir wins over venv/system bins.
    #   Local desktop/CLI callers are not restricted → untouched.
    try:
        from lib.project_mod.abs_path_guard import is_restricted
        if cwd and is_restricted():
            from lib.project_mod import portable_sandbox
            portable_sandbox.prepare_env(env, cwd)
    except Exception as e:
        logger.debug('[RunCommand] portable sandbox env prep skipped: %s', e)

    validate_environment(env)
    return env


def _sticky_cwd_capture_enabled():
    """True unless disabled via TOFU_STICKY_CWD=0. Unix-only (needs POSIX sh)."""
    if os.name == 'nt':
        return False
    return os.environ.get('TOFU_STICKY_CWD', '').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _build_cwd_capture(exec_command, capture_file):
    """Wrap *exec_command* so the shell's FINAL cwd lands in *capture_file*.

    The cwd is written to a dedicated file — NEVER stdout — so command output
    can never spoof the captured value (the sentinel-framing bug that makes a
    shared-stdin persistent shell fragile). The user command's exit status is
    preserved: we stash ``$?`` immediately after it and re-exit with it, so
    callers still see the real return code.

    A trailing ``cd subdir`` inside *exec_command* persists to the ``pwd`` here
    because both run in the same ``sh -c`` invocation. On any failure the file
    simply stays empty and the caller safe-degrades (anchor unchanged).
    """
    # Newline-separated (not '; ') so a trailing `# comment` in the user
    # command cannot swallow the trailer.
    return (
        f'{exec_command}\n'
        f'__tofu_rc=$?\n'
        f'command pwd -P > {capture_file} 2>/dev/null || true\n'
        f'exit $__tofu_rc\n'
    )


def _read_cwd_capture(capture_file):
    """Read + unlink a cwd-capture file. Returns the captured path or None."""
    try:
        with open(capture_file, encoding='utf-8', errors='replace') as f:
            val = f.read().strip()
    except OSError as e:
        logger.debug('[run_command] cwd capture read failed: %s', e)
        val = None
    finally:
        try:
            os.unlink(capture_file)
        except OSError as e:
            logger.debug('[run_command] capture file unlink failed, using fallback: %s', e)
            pass
    return val or None



# ── Read & write tools are now in read_tools.py / write_tools.py ──
# All functions re-exported at the top of this file.






# ═══════════════════════════════════════════════════════
#  Filesystem snapshot helpers for run_command tracking
# ═══════════════════════════════════════════════════════

# Max depth to scan for file changes after run_command (avoid scanning huge trees)
_SNAPSHOT_MAX_FILES = 5000
_SNAPSHOT_MAX_DEPTH = 6
_SCOPED_SNAPSHOT_MAX_TARGETS = 64
from runtime_guards import resolve_resource_budget
_SNAPSHOT_MAX_ENTRIES = resolve_resource_budget(
    'TOFU_COMMAND_SNAPSHOT_MAX_ENTRIES', minimum=1, maximum=20000)
_SNAPSHOT_MAX_SECONDS = resolve_resource_budget(
    'TOFU_COMMAND_SNAPSHOT_MAX_SECONDS', minimum=1, maximum=5)
_snapshot_clock = time.monotonic

# Directories to exclude from snapshot IN ADDITION to IGNORE_DIRS.
# Most false positives from autonomously-mutating files (e.g. log rotation)
# are handled by _filter_changes_by_targets() which compares snapshot diffs
# against the command's actual write targets.  This set is only for dirs
# whose internal churn is so extreme that even snapshotting them is wasteful
# (thousands of small files changing every second).
_SNAPSHOT_EXTRA_IGNORE = {
    'pgdata',           # legacy PostgreSQL data dir (if present from old installs)
}


# ── rm → trash rewrite (recoverable deletion) ───────────────────────
# Make ordinary deletes reversible: instead of executing ``rm`` directly,
# we prepend a shell function that MOVES targets into a per-workspace
# ``.tofu_trash/`` directory. This is self-contained (no trash-cli / gio
# dependency, works on headless FUSE mounts) and survives flag variations
# because the agent's literal ``rm ...`` call hits our function first.
# Disable with ``TOFU_RM_TRASH=0``. Each delete lazily prunes trash entries
# older than ``TOFU_TRASH_TTL_DAYS`` (default 7; 0 = keep forever).
_RM_TRASH_ENABLED = os.environ.get('TOFU_RM_TRASH', '1') not in ('0', 'false', 'False')
_TRASH_DIRNAME = os.environ.get('TOFU_TRASH_DIRNAME', '.tofu_trash')
# Lazy retention: on each delete, prune trash timestamp-dirs older than this
# many days so the recoverable-delete bin can't grow unbounded. Set to 0 to
# disable pruning (keep forever). Default 7 days.
try:
    _TRASH_TTL_DAYS = int(os.environ.get('TOFU_TRASH_TTL_DAYS', '7'))
except (ValueError, TypeError) as e:
    logger.debug('Invalid TOFU_TRASH_TTL_DAYS, defaulting to 7: %s', e)
    _TRASH_TTL_DAYS = 7


def _trash_workspace_root(cwd):
    """Resolve the enclosing workspace (git) root for the recoverable-delete bin.

    The undo bin belongs to the WORKSPACE, not to whatever subdirectory the
    command happens to run in: a sticky ``cd lib/tasks_pkg`` used to drop a
    ``.tofu_trash/`` full of stale source copies right inside the source
    tree (found 2026-08-20: 161K of old orchestrator snapshots under
    ``lib/tasks_pkg/.tofu_trash``). Walk up from ``cwd`` to the nearest
    directory containing a ``.git`` entry (directory OR git-worktree file);
    fall back to ``cwd`` itself when there is none (temp dirs, un-versioned
    workspaces — the historical behaviour).
    """
    if not cwd or not os.path.isabs(cwd):
        return cwd
    d = os.path.normpath(cwd)
    while True:
        if os.path.exists(os.path.join(d, '.git')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return cwd
        d = parent

def _maybe_wrap_rm_with_trash(command, cwd):
    """Prepend an ``rm`` shell-function shim that trashes instead of deletes.

    Returns the (possibly rewritten) command. No-op when disabled, when the
    command has no ``rm`` segment, or when the command already references the
    trash dir (avoid double-wrapping). The shim moves each target under
    ``<workspace-root>/.tofu_trash/<timestamp>/`` preserving relative layout
    (the workspace root is resolved by ``_trash_workspace_root`` so a sticky
    ``cd`` into a subdirectory never drops the bin inside the source tree);
    ``-f`` on
    a missing file still succeeds (exit 0) to match ``rm`` semantics. On each
    delete it also lazily prunes trash entries older than
    ``TOFU_TRASH_TTL_DAYS`` (default 7) so the bin can't grow unbounded.

    The trash is the WORKSPACE's undo bin, not the machine's. An absolute
    operand that resolves OUTSIDE ``cwd`` (the routine
    ``rm -rf /tmp/wt_fill`` temp-worktree cleanup) is deleted DIRECTLY by the
    real ``rm`` inside the shim — moving it into the project trash is a
    cross-device copy of the whole tree into the workspace (minutes on a
    FUSE mount) and fills the bin with non-project temp data. This path only
    became live when the blunt ``rm -rf /`` dangerous-pattern regex was
    removed (2026-07-28): before that, every absolute delete was refused
    upstream, so the shim never saw an outside-workspace target. Relative
    and in-workspace absolute operands keep full trash semantics. The
    boundary is textual (normpath prefix) and only applied when ``cwd`` is
    itself absolute; otherwise every operand keeps the old trash behaviour
    (fail-safe toward recoverability).
    """
    if not _RM_TRASH_ENABLED or not command:
        return command
    # Cheap pre-check: only wrap when an `rm` actually appears as a command
    # token (not inside a path/string like `confirm.txt`).
    if not _re.search(r'(^|[|&;]|\s)rm(\s|$)', command):
        return command
    if _TRASH_DIRNAME in command:
        return command
    trash_base = _trash_workspace_root(cwd)
    trash_root = os.path.join(trash_base or '.', _TRASH_DIRNAME)
    _ws = (os.path.normpath(trash_base)
           if trash_base and os.path.isabs(trash_base) else None)
    if _ws == os.sep:
        _ws = None  # root cwd: every absolute path is "inside" — trash all
    # Lazy retention prune: drop trash timestamp-dirs older than the TTL so the
    # bin can't grow unbounded. Runs at the START of the command, while `rm` is
    # still the real binary (before the function below shadows it). No-op when
    # the trash dir doesn't exist yet or TTL is disabled (0).
    prune = ''
    if _TRASH_TTL_DAYS > 0:
        prune = (
            'find "{trash}" -mindepth 1 -maxdepth 1 -type d -mtime +{ttl} '
            '-exec rm -rf {} + 2>/dev/null; '
        ).replace('{ttl}', str(_TRASH_TTL_DAYS)).replace('{trash}', trash_root)
    # POSIX shell function: route every `rm` through a move-to-trash that
    # ignores rm's own flags (-r/-f/-rf/...) and trashes the file operands.
    # The trash root is made self-ignoring (a `.gitignore` containing `*`) so
    # trashed files never leak into git in WHATEVER repo the command runs in —
    # cwd may be a cloned target repo (e.g. an eval workdir) that has its own
    # git and no knowledge of our top-level .gitignore. Without this, `git add
    # -A` there would sweep trashed files into the captured diff.
    # `_td` is created LAZILY on the first trashed operand so an
    # outside-only delete leaves no empty timestamp dir behind; `_rc`
    # surfaces a real-rm failure for the direct-delete branch (the trash
    # branch keeps its historical always-0 best-effort semantics).
    outside_branch = ''
    if _ws:
        outside_branch = (
            'case "$_a" in '
            '"{ws}"|"{ws}/"*) ;; '
            '/*) command rm -rf -- "$_a" || _rc=$?; continue;; '
            'esac; '
        ).replace('{ws}', _ws)
    shim = prune + (
        'rm() { '
        '_td=; _rc=0; '
        'for _a in "$@"; do '
        'case "$_a" in -*) continue;; esac; '
        '[ -e "$_a" ] || [ -L "$_a" ] || continue; '
        + outside_branch +
        'if [ -z "$_td" ]; then '
        '_td="{trash}/$(date +%Y%m%d_%H%M%S_%N)"; mkdir -p "$_td"; '
        '[ -f "{trash}/.gitignore" ] || printf "*\\n" > "{trash}/.gitignore" 2>/dev/null; '
        'fi; '
        'mkdir -p "$_td/$(dirname "$_a")" 2>/dev/null; '
        'mv "$_a" "$_td/$_a" 2>/dev/null || cp -a "$_a" "$_td/$_a" 2>/dev/null; '
        'done; return $_rc; }; '
    ).replace('{trash}', trash_root)
    return shim + command
    # All segments are known read-only
    return False


def _snapshot_scope_rel_targets(write_targets):
    """Return bounded literal paths, or None when shell expansion is required."""
    if not write_targets or len(write_targets) > _SCOPED_SNAPSHOT_MAX_TARGETS:
        return None
    clean = set()
    for value in write_targets:
        if not isinstance(value, str) or not value or len(value) > 4096:
            return None
        if value[0] in ('"', "'"):
            if len(value) < 2 or value[-1] != value[0]:
                return None
            value = value[1:-1]
        if any(char in value for char in '*?[$`~{}\\\x00\n\r\"\''):
            return None
        normalized = os.path.normpath(value)
        if (os.path.isabs(normalized) or normalized in ('.', '..')
                or normalized.startswith('..' + os.sep)):
            return None
        clean.add(normalized)
    return clean


def _snapshot_targets(base_path, targets):
    clean = _snapshot_scope_rel_targets(targets)
    if clean is None:
        raise ValueError('Snapshot targets must be bounded literal relative paths')
    return _snapshot_project_files(base_path, _targets=clean)


class _FileSnapshot(dict):
    """Only a complete walk can prove that an unobserved path is absent."""
    def __init__(self):
        super().__init__()
        self.complete = True
        self.fingerprints = {}
        self.root_identity = None

    def capture(self, relative, metadata):
        if stat.S_ISREG(metadata.st_mode):
            self[relative] = (metadata.st_mtime, metadata.st_size)
            self.fingerprints[relative] = file_fingerprint(metadata)
        else:
            self.complete = False


def _snapshot_project_files(base_path, *, _targets=None):
    """Stream a bounded DFS; count directories and ignored entries as work.

    Retain at most 5,000 file metadata pairs and depth + 1 directory iterators.
    An error or budget exhaustion marks absence evidence incomplete. Readers
    must never interpret that omission as a file creation/deletion.
    """
    snapshot = _FileSnapshot()
    remaining = _SNAPSHOT_MAX_ENTRIES
    deadline = _snapshot_clock() + _SNAPSHOT_MAX_SECONDS
    base = os.path.abspath(base_path)
    try:
        root = os.stat(base, follow_symlinks=False)
        if not stat.S_ISDIR(root.st_mode):
            snapshot.complete = False
            return snapshot
        snapshot.root_identity = (root.st_dev, root.st_ino)
    except OSError:
        snapshot.complete = False
        return snapshot

    def exhausted():
        if (remaining <= 0 or len(snapshot) >= _SNAPSHOT_MAX_FILES
                or _snapshot_clock() >= deadline):
            snapshot.complete = False
            return True
        return False

    def visit(directory, depth):
        nonlocal remaining
        if exhausted():
            return
        try:
            relative_directory = os.path.relpath(directory, base)
            with anchored_directory(base, relative_directory, snapshot.root_identity) as descriptor, os.scandir(descriptor) as entries:
                for entry in entries:
                    if exhausted():
                        return
                    remaining -= 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in IGNORE_DIRS or entry.name in _SNAPSHOT_EXTRA_IGNORE:
                                continue
                            if depth >= _SNAPSHOT_MAX_DEPTH:
                                snapshot.complete = False
                            else:
                                visit(os.path.join(directory, entry.name), depth + 1)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            snapshot.capture(os.path.relpath(os.path.join(directory, entry.name), base), st)
                    except OSError:
                        snapshot.complete = False
                    if remaining <= 0:
                        snapshot.complete = False
                        return
        except (OSError, ValueError, NotImplementedError) as exc:
            snapshot.complete = False
            logger.debug('[Snapshot] scan failed for %s: %s', directory, exc)

    if _targets is None:
        visit(base, 0)
        return snapshot
    # Parent targets cover their descendants. Sort only the <=64 admitted
    # targets, never a directory's potentially unbounded entries.
    selected = []
    for relative in sorted(_targets):
        if exhausted():
            break
        if any(relative.startswith(parent + os.sep) for parent in selected):
            continue
        selected.append(relative)
        parts = relative.split(os.sep)
        if len(parts) - 1 > _SNAPSHOT_MAX_DEPTH:
            snapshot.complete = False
            continue
        if any(part in IGNORE_DIRS or part in _SNAPSHOT_EXTRA_IGNORE for part in parts[:-1]):
            continue
        if remaining < len(parts):
            snapshot.complete = False
            break
        remaining -= len(parts)
        try:
            with anchored_parent(base, relative, snapshot.root_identity) as (parent, name):
                metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                if parts[-1] not in IGNORE_DIRS and parts[-1] not in _SNAPSHOT_EXTRA_IGNORE:
                    if len(parts) > _SNAPSHOT_MAX_DEPTH:
                        snapshot.complete = False
                    else:
                        visit(os.path.join(base, relative), len(parts))
            elif stat.S_ISREG(metadata.st_mode):
                if len(snapshot) >= _SNAPSHOT_MAX_FILES:
                    snapshot.complete = False
                    break
                snapshot.capture(relative, metadata)
        except FileNotFoundError:
            pass  # A known absent target is valid creation evidence.
        except (OSError, ValueError, NotImplementedError):
            snapshot.complete = False
    return snapshot


def _diff_snapshots(base_path, before, after):
    """Compare two snapshots to find created, deleted, and modified files.

    Each snapshot maps ``rel_path → (mtime, size)``.  A file is reported
    ``modified`` when its mtime or size changed. Incomplete snapshots cannot
    establish absence, so they never infer creation or deletion from omission.

    Returns list of dicts: [{rel_path, change_type}] where change_type is
    'created', 'deleted', or 'modified'.
    """
    changes = []
    all_paths = set(before.keys()) | set(after.keys())
    for rel in sorted(all_paths):
        in_before = rel in before
        in_after = rel in after
        if in_after and not in_before and getattr(before, 'complete', True):
            changes.append({'rel_path': rel, 'change_type': 'created'})
        elif in_before and not in_after and getattr(after, 'complete', True):
            changes.append({'rel_path': rel, 'change_type': 'deleted'})
        elif in_before and in_after and before[rel] != after[rel]:
            changes.append({'rel_path': rel, 'change_type': 'modified'})
    return changes


def _record_run_command_changes(base_path, changes, conv_id=None, task_id=None):
    """Record file changes detected by run_command for undo support.

    For deleted files, saves the original content so it can be recreated.
    For modified files, saves the original content for restoration.
    For created files, records them so they can be deleted on undo.
    """
    recorded = []
    fingerprint_byte_budget = [UNDO_BATCH_CONTENT_BYTES]
    for change in changes:
        action = change['change_type']
        if action not in ('created', 'deleted', 'modified'):
            continue
        original = None if action == 'created' else change.get('original_content')
        if _record_modification(
                base_path, 'run_command', change['rel_path'],
                original_content=original, conv_id=conv_id,
                task_id=task_id, action=action,
                fingerprint_byte_budget=fingerprint_byte_budget):
            recorded.append({'path': change['rel_path'], 'action': action})
    return recorded


# ═══════════════════════════════════════════════════════
#  Tool Implementation: run_command
# ═══════════════════════════════════════════════════════

# Stdin detection uses /proc/<pid>/syscall for definitive read(0,...) detection.
# Checked each iteration of the select() loop (~every 0.2s). No timing heuristics.


def _display_command(command):
    """Bounded single-line command echo for the result header.

    The ``$ <cmd>`` header is the result's identity in the transcript and the
    prefix anchor of the structural error stamp (``_stamp_execution_error``
    keys on successful results carrying an execution-layer header), so it
    stays — but a heredoc or multi-KB one-liner must not be echoed in full.
    Multi-line commands fold to their first line with an explicit marker.
    """
    text = (command or '').strip()
    first_line, sep, _rest = text.partition('\n')
    display = first_line.rstrip()
    if sep:
        display += ' …'
    if len(display) > 200:
        display = display[:200] + '…'
    return display


def _format_run_output(command, stdout, stderr, exit_code, timed_out=False,
                       aborted=False, interrupted_by=''):
    """Format command output into the standard result text.

    ``interrupted_by`` marks a PER-COMMAND interrupt (user button / stall
    watchdog) — distinct from ``aborted`` (whole-task Stop): the partial
    output is preserved and the task CONTINUES with this result fed back to
    the model."""
    output_parts = []
    if stdout.strip():
        output_parts.append(stdout)
    if stderr.strip():
        if stdout.strip():
            output_parts.append(f'\n[stderr]\n{stderr}')
        else:
            output_parts.append(stderr)

    output = ''.join(output_parts).strip()
    output = _clean_command_output(output)

    if len(output) > MAX_COMMAND_OUTPUT:
        head_size = MAX_COMMAND_OUTPUT * 3 // 4
        tail_size = MAX_COMMAND_OUTPUT // 4
        output = (output[:head_size]
                  + f'\n\n… [output truncated: {len(output):,} chars total] …\n\n'
                  + output[-tail_size:])

    result_text = f'$ {_display_command(command)}\n'
    if output:
        result_text += f'{output}\n'
    if interrupted_by:
        result_text += (f'\n[Command interrupted by {interrupted_by}]'
                        f'\n[exit code: -1]')
    elif aborted:
        result_text += '\n[Command aborted by user]\n[exit code: -1]'
    elif timed_out:
        result_text += '\n[Command timed out]\n[exit code: -1]'
    else:
        result_text += f'\n[exit code: {exit_code}]'
    return result_text


def validate_command_cwd(base, command, *, roots=None):
    """Reject invalid local cwd without spawning or silently changing intent."""
    if base and not os.path.isdir(base):
        guidance = ''
        registered = list((roots or {}).items())
        if registered:
            owners = [(label, state) for label, state in registered
                      if Path(os.path.abspath(base)).is_relative_to(
                          os.path.abspath(state['path']))]
            if owners:
                label, state = max(owners, key=lambda item: len(item[1]['path']))
                guidance += f'\nResolved workspace root: {label} ({state["path"]!r}).'
            guidance += '\nRegistered roots (use rootname:subdir or inspect with list_dir):'
            for label, state in registered[:8]:
                guidance += f'\n  {label}: {state["path"]!r}'
            if len(registered) > 8:
                guidance += f'\n  ... {len(registered) - 8} more registered roots.'
            guidance += '\nKeep the historical repository identity; do not append its path to another root.'
        return (
            f'$ {command}\n\n'
            f'Error: Invalid working_dir {base!r}: directory does not exist '
            'or is not a directory. Command was not started. '
            'Correct working_dir to an existing directory before retrying; '
            'do not repeat the same invalid path. No fallback directory was used.'
            + guidance
        )
    return None


def command_background_refusal(command):
    """Reject unmanaged detach before snapshots, fastpaths, or subprocesses.

    Tool-level backgrounding is supervised: its worker retains admission until
    execution/history ends. Shell detach can close inherited lease descriptors.
    The configured prefix is executed shell source, not a readonly exemption.
    """
    original = recorded_command_source(command)
    admitted = command if original is None else original
    refusal = command_cwd_setup_refusal(admitted or '', inherited_cdpath=os.environ.get('CDPATH', ''))
    if refusal:
        return refusal
    source = command_setup_view(command_admission_view(
        f'{SHELL_PREFIX} {admitted}' if SHELL_PREFIX else (admitted or '')))
    shell_shape = _mask_quoted_literals(source)
    detached = (
        _re.search(r'(?<![&<>])&(?![&>])', shell_shape)
        or _re.search(r'(?:^|[;|\s])(?:nohup|disown|setsid)(?:\s|$)', shell_shape)
    )
    if not detached:
        # Quoting or an absolute executable path must not hide the command word.
        from .command_analysis import (
            _ENV_ASSIGNMENT_GUARD_RE, _unwrap_command_parts, command_segment_words,
        )
        for segment in _split_pipeline(source):
            try:
                words = list(command_segment_words(segment))
            except ValueError:
                continue  # Malformed shell source is still leased, never executed as readonly.
            while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
                words = words[1:]
            words = _unwrap_command_parts(words)
            # An unparsed heredoc's body is not a sequence of shell command
            # heads. Keep its conservative writer claim, not a payload-based
            # dynamic-executable refusal. Proven literal bodies were masked.
            if '<<' not in source and words and any(character in words[0] for character in '$`'):
                return ('Error [workspace-lease-background-unsupported]: unresolved dynamic '
                        'executable cannot retain a verified managed child lifetime. '
                        'Use a literal executable or understood static setup; command was not started.')
            if words and os.path.basename(words[0]) in {'nohup', 'disown', 'setsid'}:
                detached = True
                break
    if detached:
        return ('Error [workspace-lease-background-unsupported]: unmanaged shell '
                'background/detach cannot retain a verified writer lease. '
                'Use the tool background option with a foreground command; '
                'command was not started.')
    return None


def _segment_needs_command_writer(segment, cwd):
    """Separate literal opaque executables from unsafe shell/utility shapes."""

    formatter = command_segment_formatter_writes(segment)
    if formatter is not None:
        return formatter[0]
    from .command_analysis import (
        _command_checkout_mutation, _ENV_ASSIGNMENT_GUARD_RE, _unwrap_command_parts,
        is_literal_sed_filter, command_segment_words,
    )
    # Quote contents are executable arguments, not shell write syntax. Remove
    # descriptor duplication only at unquoted positions; it opens no file.
    shell_source = _without_descriptor_duplications(segment)
    if (os.name == 'posix' and not SHELL_PREFIX
            and _lease_free_git_segment(segment) == segment):
        return False
    if (_command_checkout_mutation(_mask_quoted_literals(shell_source), cwd)
            or _has_dynamic_shell_syntax(shell_source)):
        return True
    try:
        words = list(command_segment_words(segment))
    except ValueError:
        return True
    while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
        words = words[1:]
    words = _unwrap_command_parts(words)
    if not words:
        return False
    name = os.path.basename(words[0])
    if name == 'sed' and is_literal_sed_filter(words[1:]):
        return False
    # These interpreters/wrappers hide shell effects; Git outside the complete
    # hardened exemption may refresh indexes or invoke config-driven helpers.
    if name in {
            'git', 'sh', 'bash', 'dash', 'zsh', 'ksh', 'fish', 'csh', 'tcsh',
            'env', 'eval', 'exec', 'source', '.', 'sudo', 'doas', 'xargs',
            'if', 'for', 'while', 'until', 'case', 'function',
            'sed', 'awk', 'gawk', 'mawk', 'nawk', 'goawk',
            'cp', 'mv', 'rm', 'rmdir', 'unlink', 'mkdir', 'touch', 'install', 'ln',
            'tee', 'truncate', 'chmod', 'chown', 'chgrp', 'patch', 'rsync',
            'dd', 'tar', 'unzip', 'zip', 'curl', 'wget',
    }:
        return True
    if name == 'sort':
        from .command_analysis import command_segment_targets
        parsed = command_segment_targets(segment)
        return bool(parsed.targets) or not parsed.complete
    if name == 'find':
        from .command_analysis import command_segment_targets
        parsed = command_segment_targets(segment)
        return bool(parsed.targets) or not parsed.complete
    if name == 'uniq' and len([word for word in words[1:] if not word.startswith('-')]) >= 2:
        return True
    return False


def _regular_command_file(path, cwd, *, must_exist=False, allow_missing_parents=False):
    """Resolve a regular leaf without aliases/tree/metadata side effects.

    Output redirects may have missing ancestors; existing ones must be real
    directories. Metadata is admission evidence, not a filesystem sandbox.
    Hard links and any symlink component widen to ROOT rather than pretending
    path-keyed FILE claims cover every alias.
    """
    if (not path or path == '-' or path.startswith('~') or path.endswith('/')
            or any(part in {'..', '.git'} for part in Path(path).parts)):
        return None
    absolute = os.path.abspath(os.path.join(cwd, path))
    if '.git' in Path(absolute).parts:
        return None
    from .workspace_lease import WorkspaceBusyError, _checkout_identity
    try:
        identity = _checkout_identity(cwd)
        if identity is not None:
            common = os.fspath(identity[1])
            if os.path.commonpath((absolute, common)) == common:
                return None  # includes --separate-git-dir metadata outside cwd
        for parent in Path(absolute).parents:
            try:
                info = parent.lstat()
            except FileNotFoundError:
                if must_exist or not allow_missing_parents:
                    return None
                continue  # mkdir earlier in a chain may create missing ancestors
            if not stat.S_ISDIR(info.st_mode):
                return None
        try:
            info = os.lstat(absolute)
        except FileNotFoundError:
            return None if must_exist else absolute
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        return absolute
    except (OSError, UnicodeError, ValueError, WorkspaceBusyError):
        return None


def _literal_file_writer_target(segment, cwd):
    """Narrow one declared writer to a FILE claim, else keep ROOT authority.

    Do not reuse the lossy snapshot target heuristic for this decision. Only
    static supported argv/flags and a single verified regular leaf qualify.
    Opaque runtime + a simple output redirect retains the existing opaque
    scheduling policy; the redirect is not proof of its program's write set.
    """
    from .command_analysis import (
        command_segment_targets, command_segment_words, _static_writer_operands,
        _ENV_ASSIGNMENT_GUARD_RE,
    )
    parsed_targets = command_segment_targets(segment)
    try:
        original_words = list(command_segment_words(segment))
    except ValueError:
        return None
    while original_words and _ENV_ASSIGNMENT_GUARD_RE.match(original_words[0]):
        original_words.pop(0)
    if original_words and os.path.basename(original_words[0]) in {'sudo', 'doas', 'env', 'command', 'exec'}:
        return None
    if '\n' in segment or '\r' in segment:
        return None
    shape = _mask_quoted_literals(segment)
    redirects = tuple(_re.finditer(r'>+', shape))
    if redirects:
        if '<' in shape or len(redirects) != 1 or redirects[0].group() not in {'>', '>>'}:
            return None
        redirect = redirects[0]
        before = segment[:redirect.start()]
        descriptor = _re.search(r'(?<!\S)([0-9]+)$', before)
        if descriptor:
            if descriptor.group(1) not in {'1', '2'}:
                return None
            before = before[:descriptor.start()]
        after = segment[redirect.end():].strip()
        if (_has_dynamic_shell_syntax(before) or _has_dynamic_shell_syntax(after)
                or _segment_needs_command_writer(before, cwd)):
            return None
        targets = parsed_targets.targets
        return (_regular_command_file(targets[0], cwd, allow_missing_parents=True)
                if len(targets) == 1 else None)
    if _has_dynamic_shell_syntax(segment):
        return None
    words = parsed_targets.words
    if not words:
        return None
    name, args = os.path.basename(words[0]), list(words[1:])
    if name == 'cp':
        operands = _static_writer_operands(args, {
            '-f', '-i', '-n', '-v', '-T', '--force', '--interactive',
            '--no-clobber', '--verbose', '--no-target-directory',
        })
        if (operands is None or len(operands) != 2
                or _regular_command_file(operands[0], cwd, must_exist=True) is None):
            return None
    elif name not in {'touch', 'tee', 'truncate', 'sort'}:
        return None  # moves/deletes/backups/editors/trees retain ROOT
    if not parsed_targets.complete or len(parsed_targets.targets) != 1:
        return None
    target = parsed_targets.targets[0]
    return _regular_command_file(target, cwd)


def _git_shell_output_claims(segment, cwd):
    """Union shell-opened files with Git effects, including failed Git invocations."""
    from .command_analysis import command_segment_output_targets
    outputs = command_segment_output_targets(segment)
    if not outputs.complete:
        raise ValueError('Unresolved Git shell output target; use a literal output filename')
    claims = set()
    for target in outputs.targets:
        path = _regular_command_file(target, cwd, allow_missing_parents=True)
        claims.add(('file', path) if path is not None else
                   ('tree', os.path.realpath(os.path.join(cwd, target))))
    return claims


def _literal_directory_writer_targets(segment, cwd):
    """Exact mkdir operands own their trees, never the unrelated starting cwd.

    These are ROOT claims on actual targets (not directory/file aliases). Unknown
    flags, expansions, modes and redirects retain the existing cwd fallback.
    """
    from .command_analysis import command_segment_targets, command_segment_words
    if _has_dynamic_shell_syntax(segment):
        return None
    try:
        original_words = command_segment_words(segment)
    except ValueError:
        return None
    if original_words and os.path.basename(original_words[0]) in {'sudo', 'doas', 'env', 'command', 'exec'}:
        return None
    parsed = command_segment_targets(segment)
    words = parsed.words
    if not words or os.path.basename(words[0]) != 'mkdir':
        return None
    operands = parsed.targets
    if not parsed.complete or not operands or any(not path or path.startswith('~') for path in operands):
        return None
    target_paths = set()
    for operand in operands:
        if '..' in Path(operand).parts:
            # mkdir -p creates each component before processing parent traversal.
            # Final realpath alone loses writes in the cwd or a peer checkout.
            target_paths.add(os.path.realpath(cwd))
            prefix = cwd
            for part in Path(operand).parts:
                prefix = os.path.join(prefix, part)
                target_paths.add(os.path.realpath(prefix))
        else:
            target_paths.add(os.path.realpath(os.path.join(cwd, operand)))
    targets = tuple(sorted(target_paths))
    from .workspace_lease import WorkspaceBusyError, _checkout_identity
    try:
        identity = _checkout_identity(cwd)
        if identity is not None:
            common = os.fspath(identity[1])
            if any(os.path.commonpath((target, common)) == common for target in targets):
                targets = tuple(sorted(set(targets) | {os.path.realpath(cwd)}))
                # External metadata owns its source; preserve peer prefix claims too.
    except (OSError, UnicodeError, ValueError, WorkspaceBusyError):
        return None
    return targets


def _literal_filesystem_claims(segment, cwd):
    """Exact plain utility operands; unsupported flags retain tree fallback."""
    from .command_analysis import command_segment_words, _static_writer_operands
    if _has_dynamic_shell_syntax(segment):
        return None
    try:
        words = command_segment_words(segment)
    except ValueError:
        return None
    if not words:
        return None
    name = os.path.basename(words[0])
    flags = {
        'rm': {'-f', '-r', '-R', '-rf', '-fr', '--force', '--recursive'},
        'unlink': set(), 'rmdir': {'--ignore-fail-on-non-empty'},
        'mv': {'-f', '-i', '-n', '-v', '-T', '--force', '--no-clobber', '--no-target-directory'},
        'cp': {'-f', '-i', '-n', '-v', '-T', '-r', '-R', '-a', '--recursive', '--no-target-directory'},
        'touch': set(), 'tee': {'-a', '--append'},
    }
    if name not in flags:
        return None
    if any(value in {'-T', '--no-target-directory'} for value in words[1:]):
        return None
    operands = _static_writer_operands(list(words[1:]), flags[name])
    if not operands or any(value.startswith('~') for value in operands):
        return None
    def claim(value):
        file = _regular_command_file(value, cwd)
        if file:
            return {('file', file)}
        lexical = os.path.abspath(os.path.join(cwd, value))
        return {('tree', lexical), ('tree', os.path.realpath(lexical))}
    if name not in {'mv', 'cp'}:
        return set().union(*(claim(value) for value in operands))
    if len(operands) < 2:
        return None
    sources, destination = operands[:-1], operands[-1]
    destination_path = os.path.join(cwd, destination)
    into_directory = os.path.isdir(destination_path)
    if len(sources) > 1 and not into_directory:
        return None
    claims = set()
    for source in sources:
        target = (os.path.join(destination, os.path.basename(source.rstrip('/')))
                  if into_directory else destination)
        if name == 'mv':
            claims.update(claim(source))
        if os.path.isdir(os.path.join(cwd, source)):
            lexical = os.path.abspath(os.path.join(cwd, target))
            claims.update({('tree', lexical), ('tree', os.path.realpath(lexical))})
        else:
            claims.update(claim(target))
    return claims


def _command_claim_order(claim):
    """One global checkout order, then ROOT before FILE within that checkout."""

    from .workspace_lease import lease_claim_order
    return lease_claim_order(claim)


def _network_probe_execution_source(command):
    """Bind lease exemption to the exact source hardened at execution."""
    if os.name != 'posix' or SHELL_PREFIX:
        return None
    from .command_network_probe import prepare_network_probe
    original = recorded_command_source(command)
    admitted = command if original is None else original
    planned = prepare_network_probe(admitted)
    if planned is None:
        return None
    if original is None:
        return planned
    # The trusted adapter already verified its exact wrapper. Only replace
    # one verbatim original body; drift/ambiguous embedding keeps exclusion.
    if command.count(original) != 1:
        return None
    return command.replace(original, planned, 1)


def command_write_lease_claims(base, command, *, _owned_formatter_paths=None):
    """Resolve typed (scope, path) writes for both runner and outer dispatch.

    Verified literal regular-file target sets claim FILE, not their cwd.
    Git metadata uses its own domain; destructive directories and ambiguous
    shell writes use TREE. Known Git checkout transformations retain ROOT.

    Opaque test/build/interpreter commands request no lifetime read lease. The
    snapshot heuristic remains separate. Only a literal, non-pipeline ``cd``
    followed by ``&&`` can drop its old cwd: failure then prevents the mutation.
    Other shell control shapes retain fallback cwd claims, and earlier writes
    never disappear when a later cd changes the execution directory.
    """
    from .command_analysis import (
        _ENV_ASSIGNMENT_GUARD_RE, _unwrap_command_parts, command_segment_words,
        command_segment_literal_targets, command_segment_write_scope_refusal,
    )
    from .git_command_scope import git_command_scopes
    if not command or not command.strip():
        return ()
    original = recorded_command_source(command)
    if original is None and os.name == 'posix' and not SHELL_PREFIX and _lease_free_git_command(command) is not None:
        return ()

    if _network_probe_execution_source(command) is not None:
        return ()
    admitted = command if original is None else original
    refusal = command_cwd_setup_refusal(admitted, inherited_cdpath=os.environ.get('CDPATH', ''))
    if refusal:
        raise ValueError(refusal)
    root = os.path.realpath(base or os.getcwd())
    analysis_source = (harden_git_inspections(admitted)
                       if original is None and os.name == 'posix' and not SHELL_PREFIX else admitted)
    source = command_setup_view(command_admission_view(
        f'{SHELL_PREFIX} {analysis_source}' if SHELL_PREFIX else analysis_source))
    if not SHELL_PREFIX and not any(
            _segment_needs_command_writer(segment, root) for segment in _split_pipeline(source)):
        return ()

    claims = {('tree', root)} if SHELL_PREFIX else set()
    cwds = {root}
    fallback_cwds = set()
    spans = _split_pipeline_spans(source)
    try:
        git_scopes = {scope.start: scope for scope in git_command_scopes(
            source, root, environment=os.environ, strict=False)}
    except ValueError as error:
        if 'lifecycle' in str(error):
            raise
        git_scopes = {}  # Nonliteral targets retain tree-wide admission.
    previous_separator = ''
    for index, (start, end) in enumerate(spans):
        segment = source[start:end].strip()

        try:
            scope_refusal = command_segment_write_scope_refusal(segment)
        except ValueError:
            scope_refusal = None  # Malformed syntax retains the existing ROOT fallback.
        if scope_refusal:
            raise ValueError(scope_refusal)
        next_separator = (source[end:spans[index + 1][0]].strip()
                          if index + 1 < len(spans) else '')
        if previous_separator in {';', '||'}:
            cwds.update(fallback_cwds)
        try:
            words = list(command_segment_words(segment))
        except ValueError:
            words = []  # Unknown syntax retains every possible execution cwd below.
        bare_words = list(words)
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words = words[1:]
        words = _unwrap_command_parts(words)

        cd_path = None
        if (bare_words and bare_words[0] == 'cd'
                and not _has_dynamic_shell_syntax(segment)):
            cd_args = bare_words[1:]
            if cd_args[:1] == ['--']:
                cd_args = cd_args[1:]
            if (len(cd_args) == 1 and not cd_args[0].startswith(('-', '~'))
                    and (not os.environ.get('CDPATH') or os.path.isabs(cd_args[0])
                         or cd_args[0].startswith(('./', '../')))):
                cd_path = cd_args[0]
        if cd_path is not None:
            targets = {os.path.realpath(os.path.join(cwd, cd_path)) for cwd in cwds}
            fallback_cwds.update(cwds)
            if previous_separator == '|' or next_separator == '|':
                pass  # Pipeline cd runs in a subshell; it cannot change a peer's cwd.
            elif next_separator == '&&':
                cwds = targets
            else:
                cwds.update(targets)  # Failed cd followed by ;/|| still permits writes.
            previous_separator = next_separator
            continue

        for cwd in cwds:
            # Read segments are hardened at execution; neighbouring writes
            # retain their own admission instead of upgrading the query.
            is_git = start in git_scopes or bool(words and os.path.basename(words[0]) == 'git')

            if is_git:
                claims.update(_git_shell_output_claims(segment, cwd))
            if (is_git and original is None and not SHELL_PREFIX and os.name == 'posix'
                    and _lease_free_git_segment(segment) == segment):
                continue
            if not is_git and not _segment_needs_command_writer(segment, cwd):
                continue
            formatter = command_segment_formatter_writes(segment)
            if formatter is not None:
                writes, targets, complete, owned = formatter
                paths = tuple(os.path.realpath(os.path.join(cwd, target)) for target in targets)
                regular = complete and all(
                    _regular_command_file(target, cwd, must_exist=True) == path
                    for target, path in zip(targets, paths))
                if writes and regular:
                    claims.update(('file', path) for path in paths)
                    if owned and _owned_formatter_paths is not None:
                        _owned_formatter_paths.update(paths)
                elif writes:
                    claims.add(('tree', cwd))
                    claims.update(('tree', path) for path in paths)
                continue
            directory_targets = _literal_directory_writer_targets(segment, cwd)
            if directory_targets is not None:
                # Plain additive mkdir does not replace source content.
                if (any('.git' in Path(path).parts for path in directory_targets)
                        or '..' in segment or os.path.realpath(cwd) in directory_targets):
                    claims.update(('tree', path) for path in directory_targets)
                continue
            filesystem_claims = _literal_filesystem_claims(segment, cwd)
            if filesystem_claims is not None:
                claims.update(filesystem_claims)
                continue
            file_target = _literal_file_writer_target(segment, cwd)
            if file_target is not None:
                claims.add(('file', file_target))
                continue
            if is_git:
                scope = git_scopes.get(start)
                if scope is None:
                    claims.add(('tree', cwd))
                else:
                    claims.update(scope.writer_claims())
                continue
            claims.add(('tree', cwd))
            for target in command_segment_literal_targets(segment):
                claims.add(('tree', os.path.realpath(os.path.join(cwd, target))))
        previous_separator = next_separator
    # Full-source ambiguity (e.g. an unterminated quote) must fail closed even
    # when the segment parser cannot identify a particular mutating command.
    plain_data = not _has_dynamic_shell_syntax(source)
    for segment in _split_pipeline(source):
        try:
            words = list(command_segment_words(segment))
        except ValueError:
            plain_data = False
            break
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words = words[1:]
        words = _unwrap_command_parts(words)
        if not words or os.path.basename(words[0]) not in {
                'cp', 'mv', 'rm', 'mkdir', 'touch', 'echo', 'printf', 'tee', 'truncate', 'install'}:
            plain_data = False
    if not plain_data and not claims and not git_scopes:
        claims = {('tree', cwd) for cwd in cwds}
    # Canonical effect order is independent of linked checkout aliases. Both
    # wrappers pre-admit the complete physical barriers with workspace_claims_lease.
    return tuple(sorted(claims, key=_command_claim_order))


def command_write_lease_paths(base, command):
    """Legacy path-only export derived from the typed admission authority."""
    return tuple(sorted({path for _scope, path in command_write_lease_claims(base, command)}))


def _with_command_writer(function):
    """Keep write admission through the supervised worker's entire lifetime."""
    import functools
    import inspect
    from contextlib import ExitStack
    from lib.project_mod.tool_admission import Cancellation, admission_timeout
    from lib.project_mod.workspace_lease import (
        WorkspaceBusyError, WorkspaceLeaseCancelled, workspace_claims_lease,
    )
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(base, command, *args, **kwargs):
        if not command or not command.strip():
            return function(base, command, *args, **kwargs)
        refusal = command_background_refusal(command)
        if refusal:
            return refusal
        if (os.name == 'posix' and not SHELL_PREFIX
                and recorded_command_source(command) is None):
            command = harden_git_inspections(command)
        owned_paths = set()
        claims = command_write_lease_claims(base, command, _owned_formatter_paths=owned_paths)
        # Mixed/opaque writers never inherit a formatter's observation authority.
        if any(scope != 'file' or path not in owned_paths for scope, path in claims):
            owned_paths.clear()
        if not claims:
            return function(base, command, *args, **kwargs)
        bound = signature.bind(base, command, *args, **kwargs).arguments
        # The outer dispatch body carries execution callbacks/control in its
        # explicit kwargs payload; runner callers have ordinary/positional args.
        control = bound.get('kwargs') or {}
        cancel = Cancellation(bound.get('task', control.get('task')),
                              bound.get('runtime_context', control.get('runtime_context')))
        wait_timeout = admission_timeout()
        with ExitStack() as stack:
            try:
                stack.enter_context(workspace_claims_lease(claims,
                    timeout=wait_timeout, cancel_event=cancel))
                if cancel.is_set():
                    raise WorkspaceLeaseCancelled('Workspace lease admission cancelled')
                if command_write_lease_claims(base, command) != claims:
                    return ('Error [workspace-target-changed]: Command targets changed during writer admission. '
                            'Re-read the workspace and reconcile; command was not started.')
            except WorkspaceLeaseCancelled as error:
                return f'Error [workspace-lease-cancelled]: {error}. Command was not started.'
            except WorkspaceBusyError as error:
                return (f'Error [workspace-busy]: {error}. Command was not started; '
                        'retry after the conflicting lease finishes.')
            if _owned_command_write.get() is not None or not owned_paths:
                return function(base, command, *args, **kwargs)
            from lib.tasks_pkg.workspace_delivery import execution_root_key
            from lib.write_freshness import (
                advance_project_read_cache_generation,
                capture_owned_write, reconcile_owned_write,
            )
            task = bound.get('task', control.get('task'))
            # Direct dispatch has an explicit conversation fallback; runner-only
            # calls without a task have no observation identity and create none.
            identity = execution_root_key(task) if task else bound.get('conv_id')
            evidence = tuple(item for path in sorted(owned_paths)
                             if (item := capture_owned_write(identity, path)) is not None)
            token = _owned_command_write.set({'quiesced': False})
            try:
                result = function(base, command, *args, **kwargs)
                if _owned_command_write.get()['quiesced']:
                    # Fence cached bytes even with no eligible observation or
                    # a failed CAS; completion, not token advancement, owns this.
                    advance_project_read_cache_generation()
                    for item in evidence:
                        reconcile_owned_write(item)
                return result
            finally:
                _owned_command_write.reset(token)

    return wrapped


@_with_command_writer
def tool_run_command(base, command, timeout=None, stdin_callback=None, task=None,
                     on_chunk=None, cwd_sink=None, on_spawn=None,
                     credentials=None, on_grep_intercept=None,
                     runtime_context=None, environment_root=None, frozen_input_paths=None):
    """Execute a shell command with optional interactive stdin support.

    Args:
        base: Working directory for the command.
        command: Shell command string to execute.
        timeout: Timeout in seconds (0 = unlimited, None = auto-detect).
        cwd_sink: Optional callback ``fn(abs_cwd)`` invoked once with the
            shell's FINAL working directory after the command completes, so a
            trailing ``cd`` inside the command can update the caller's sticky
            per-conversation cwd. Captured via a dedicated temp file (never
            stdout, so it cannot be spoofed by output); the command's real exit
            code is preserved. Any failure simply skips the callback.
        stdin_callback: Optional callback ``fn(prompt_text) -> str`` that is
            called when the subprocess appears to be waiting for stdin input.
            The callback should block until the user provides input (or return
            None to send EOF).  If not provided, stdin is closed immediately
            (original non-interactive behavior).
        task: Optional task dict — when provided, the subprocess is killed
            if ``task['aborted']`` becomes True (cooperative abort).
        credentials: Vault entry-name array selected for this child only.
        on_chunk: Optional callback ``fn(stream, text)`` invoked for each
            output chunk as soon as it is read from the subprocess.  ``stream``
            is ``'stdout'`` or ``'stderr'``.  Used to forward output to the
            frontend as a streaming SSE ``tool_progress`` event so the user
            sees output incrementally instead of waiting for the command to
            finish.  Exceptions raised inside the callback are logged and
            swallowed — they must NOT abort the command.
        on_spawn: Optional callback ``fn(exec_start_ms, deadline_ms|None)``
            invoked ONCE, the instant the subprocess is actually spawned, with
            the EFFECTIVE budget — i.e. after the cross-DC multiplier and the
            ``MAX_COMMAND_TIMEOUT`` clamp have been applied. This is the only
            point at which the real deadline exists: the caller's requested
            ``timeout`` is NOT it (a DolphinFS path multiplies it ×3), and the
            round's ``tStart`` is NOT the start (a write-approval gate can sit
            minutes between announce and spawn). Both values are epoch
            MILLISECONDS so they compare directly against the browser's
            ``Date.now()``. ``deadline_ms`` is None when the command runs
            unbounded, which is the DEFAULT. Exceptions are logged and
            swallowed — telemetry must never abort the command.
        on_grep_intercept: Internal display-only callback
            ``fn(number_of_segments)`` invoked after filesystem grep segments
            have been rewritten to the runtime ``grep_search`` helper. It is
            never written to stdout/stderr or the model-visible tool result.
    """
    if not command or not command.strip():
        return 'Error: Empty command.'

    if ((runtime_context is not None and runtime_context.cancellation_requested)
            or (task is not None and task.get('aborted'))):
        return _format_run_output(
            command, '', '', -1, timed_out=False, aborted=True)

    if not base:
        base = os.path.expanduser('~')

    cwd_error = validate_command_cwd(base, command)
    if cwd_error:
        return cwd_error

    input_plan = None
    if frozen_input_paths is not None:
        try:
            from lib.evolution.input_binding import prepare_input_binding
            input_plan = prepare_input_binding(task, frozen_input_paths)
        except Exception as exc:
            logger.debug('tool_run_command failure handled: %s', type(exc).__name__)
            return f'Error: Frozen-input observation refused before command allocation: {str(exc)[:300]}'

    try:
        from lib.credentials_vault import resolve_run_command_env
        requested_credentials = [] if credentials is None else credentials
        strip_credential_vars, credential_env = resolve_run_command_env(
            requested_credentials)
    except (TypeError, ValueError) as e:
        logger.debug('[run_command] credential request rejected: %s', e)
        return f'Error: Credential request rejected: {e}'
    except Exception as e:
        logger.error('[run_command] credential resolution failed: %s', e)
        return 'Error: Credential request rejected: vault unavailable.'

    # Resolve timeout — NO DEFAULT CEILING.
    #   A build / test suite / pip install / big grep is a WAIT, not a crash,
    #   and the old auto-detect (60s FS-heavy, 300s otherwise) SIGKILLed the
    #   process tree mid-run and returned "[Command timed out]" for work that
    #   was progressing fine. That is strictly worse than the transport read
    #   timeouts already removed (lib/llm/_transport.py): a dropped socket
    #   loses a connection, this loses the build.
    #
    #   Ending a long command is the USER's call, and that path is already
    #   independent of this one: both run loops poll ``task['aborted']`` every
    #   ~0.2s and kill the process tree on Stop. So removing the ceiling costs
    #   no control — it only stops the clock from deciding.
    #
    #   A caller (or the model) may still pass an explicit budget; 0 means
    #   unlimited and is preserved for back-compat with existing call sites.
    #   ``MAX_COMMAND_TIMEOUT`` (None by default) still clamps an explicit one.
    #   Pinned by tests/project_mod/test_no_backend_timeouts.py.
    if timeout is None:
        timeout = None                      # no ceiling
    elif not isinstance(timeout, (int, float)):
        timeout = None                      # unparseable → wait, don't guess
    elif int(timeout) == 0:
        timeout = None                      # explicit "unlimited"
    elif MAX_COMMAND_TIMEOUT is not None:
        timeout = max(1, min(int(timeout), MAX_COMMAND_TIMEOUT))
    else:
        timeout = max(1, int(timeout)) if timeout > 0 else None

    if _is_dangerous_command(command):
        return 'Error: Command blocked for safety: matches dangerous pattern.'

    # Catastrophic top-level deletion guard — refuse `rm /mnt`, `rm -rf /`,
    #   `rmdir /home`, etc. OUTRIGHT. These are never legitimate and are the
    #   exact failure mode that wiped shared paths during a benchmark run.
    #   Checked before the trash rewrite so a shallow target is never even
    #   moved to trash — it is rejected.
    _cat = _is_catastrophic_delete(command, base)
    if _cat is not None:
        logger.error('[run_command] BLOCKED catastrophic delete of %r (cwd=%s): %.200s',
                     _cat, base, command)
        return (f"Error: Command blocked for safety: refusing to delete "
                f"top-level path '{_cat}'. Deleting filesystem roots or "
                f"first-level directories (e.g. /mnt, /home, /data) is not "
                f"permitted. Delete only specific paths inside your workspace.")

    # Plan the direct-find budget before classifying an ancestor scan.  The
    # guard must judge the command that will actually execute: previously it
    # rejected ``find`` here, then (unreachably) added a native timeout much
    # later.  Keep the plan execution-side only so logs and the shell echo
    # still show the caller's original command.
    _scan_timeout_plan = None
    _scan_guard_source = command
    if (timeout is None
            and os.name == 'posix'
            and os.environ.get('TOFU_RUN_SCAN_SEGMENT_TIMEOUT', '1') != '0'):
        import shutil
        _timeout_binary = shutil.which('timeout')
        if _timeout_binary:
            try:
                _scan_seconds = int(os.environ.get(
                    'TOFU_RUN_SCAN_SEGMENT_TIMEOUT_S', '40'))
            except (TypeError, ValueError) as _scan_timeout_e:
                logger.debug('[run_command] invalid scanner segment timeout: %s',
                             _scan_timeout_e)
                _scan_seconds = 40
            # Detect a direct find with pure parsing first.  Host-option
            # probing launches ``timeout --help`` and must not happen on a
            # command the outer resource guard will refuse before any spawn.
            _, _find_segments = _bound_scan_segments(
                command, _timeout_binary, _scan_seconds, ())
            if _find_segments:
                _timeout_args = _timeout_scan_wrapper_args(_timeout_binary)
                _scan_guard_source, _planned_bounded_scans = _bound_scan_segments(
                    command, _timeout_binary, _scan_seconds, _timeout_args)
                _scan_timeout_plan = (
                    _timeout_binary, _scan_seconds, _timeout_args)

    # Unbounded recursive-scan guard ( B2). A recursive scan of a
    #   workspace ANCESTOR or a FUSE mount root can crawl for hours on network
    #   filesystems — measured 2026-07-31: `grep -rn "mcp>=1.0.0" ../` ran
    #   2.5h with zero output and wedged task 96c56840 (no timeout existed to
    #   end it). Refused ONLY when the call itself is unbounded (no explicit
    #   `timeout` arg) and the scan segment is not self-bounding (coreutils
    #   `timeout` wrapper). In-workspace scans, sibling dirs and bounded
    #   scans are all legitimate and stay open. TOFU_RUN_SCAN_GUARD=0 opts out.
    if timeout is None and os.environ.get('TOFU_RUN_SCAN_GUARD', '1') != '0':
        _scan = _unbounded_recursive_scan_target(_scan_guard_source, base)
        if _scan is not None:
            # WARNING, not ERROR — this is a resource-boundary refusal, not a
            # destructive-command classification.
            logger.warning('[run_command] BLOCKED unbounded recursive scan '
                           'target=%r (cwd=%s): %.200s', _scan, base, command)
            return (f"Error: Command blocked by resource guard: unbounded recursive "
                    f"scan of '{_scan}'. Recursive scans of a workspace "
                    f"ancestor or a FUSE mount root can crawl for hours on "
                    f"network filesystems and hang the whole task (measured: "
                    f"2.5h on `grep -rn … ../`). Narrow it: (1) scan a "
                    f"specific subdirectory instead; (2) pass an explicit "
                    f"`timeout` to run_command; or (3) wrap the scan with "
                    f"coreutils `timeout <secs>`.")

    # Filesystem-grep TRANSPARENT RUNTIME REDIRECT. Planning only replaces
    #   the grep executable token (GNU fallback splice) or the whole grep
    #   argv (rg translation) with spawn-free equivalents; the search starts
    #   when the shell reaches that segment, so earlier writes, cd,
    #   substitutions, redirections, pipelines and &&/|| retain native shell
    #   ordering. When the call carries no explicit timeout the splice is
    #   wrapped in the host-probed coreutils ``timeout`` (exit 124), the
    #   same segment-budget contract direct ``find`` scans get.
    #   Stream filters (`ps aux | grep python`) are not intercepted.
    #   TOFU_RUN_GREP_GUARD=0 disables the layer; TOFU_GREP_REDIRECT=0
    #   reverts to refuse-always; TOFU_GREP_RG=0 forces the GNU splice.
    _grep_spliced = None
    if os.environ.get('TOFU_RUN_GREP_GUARD', '1') != '0':
        _plan = None
        if os.environ.get('TOFU_GREP_REDIRECT', '1') != '0':
            try:
                # An explicit caller timeout stays authoritative and
                # suppresses the segment wrap; otherwise the planner
                # applies the env deadline itself.
                _grep_timeout_wrap = False if timeout is not None else None
                _plan = plan_grep_redirect(
                    command, base, timeout_wrap=_grep_timeout_wrap)
            except Exception as _gr_e:
                logger.error('[run_command] grep redirect planning failed: %s',
                             _gr_e, exc_info=True)
                _plan = None
        if _plan is not None and _plan.rewritten is not None:
            logger.info('[run_command] spliced %d filesystem grep(s) '
                        '(backend=%s, planning %.3fs) '
                        '(cwd=%s): %.200s', _plan.n_redirected,
                        _plan.backend or 'gnu', _plan.elapsed,
                        base, command)
            _grep_spliced = _plan.rewritten
            if on_grep_intercept is not None:
                try:
                    on_grep_intercept(_plan.n_redirected)
                except Exception as _gr_cb_e:
                    logger.debug('[run_command] grep intercept callback failed: %s',
                                 _gr_cb_e)
        else:
            _gseg = (_plan.refused_segment if _plan is not None else None) \
                or _grep_filesystem_segment(command)
            if _gseg is not None:
                # WARNING, not ERROR (owner 2026-08-05): a policy nudge for
                # the agent to re-route, not an application failure.
                logger.warning('[run_command] BLOCKED filesystem grep '
                               '(cwd=%s, reason=%s): %.200s',
                               base,
                               getattr(_plan, 'refusal_reason', None),
                               command)
                _why = ''
                if _plan is not None and _plan.refusal_reason:
                    _why = ('\nAutomatic in-process execution was not '
                            f'possible here: {_plan.refusal_reason}.')
                return (
                    'Error: Command intercepted: this `grep` reads the '
                    'filesystem, which belongs to the dedicated `grep_search` '
                    f'tool, not run_command. Refused segment: `{_gseg[:150]}`'
                    + _why + '\n'
                    'Translate:\n'
                    "  - `grep -rn 'X' lib/`   -> grep_search(pattern='X', path='lib')\n"
                    "  - `grep -n 'X' f.py`    -> grep_search(pattern='X', path='f.py')\n"
                    "  - `grep -c 'X' dir/`    -> grep_search(pattern='X', path='dir', count_only=True)\n"
                    "  - `... | head -20`      -> grep_search(..., max_results=20) "
                    '(never pipe to head)\n'
                    'grep_search is 5x+ faster on this network filesystem '
                    '(ripgrep; .gitignore- and junk-dir-aware), caps runaway '
                    "output via max_results, and won't wedge for minutes in a "
                    'FUSE bad window (measured: a `grep -rn ... | head` pipeline '
                    'ran 17m+ with zero output). Filtering ANOTHER command\'s '
                    'output through grep stays allowed '
                    '(`pytest 2>&1 | grep PASS`, `ps aux | grep python`) — only '
                    'grep with file/dir operands or `-r` is intercepted. '
                    'Escape hatch: TOFU_RUN_GREP_GUARD=0.')

    # Cross-DC timeout adjustment — multiply timeout for remote DolphinFS clusters
    try:
        from lib.cross_dc import get_timeout_multiplier
        multiplier = get_timeout_multiplier(base)
        if multiplier > 1.0 and timeout is not None:
            original_timeout = timeout
            timeout = int(timeout * multiplier)
            logger.info('[run_command] Cross-DC timeout adjustment: %ds → %ds (×%.0f) for %s',
                        original_timeout, timeout, multiplier, base)
    except Exception as e:
        logger.debug('[run_command] Cross-DC check skipped: %s', e)

    # Non-silent grep hardening: when the user runs a recursive `grep -r ...`
    #   in a non-pipeline form, inject `-I` (skip binary) and `--exclude-dir=`
    #   for each entry in IGNORE_DIRS so cross-DC FUSE mounts don't time out.
    #   Real grep still does the work — output format and regex flavor are
    #   unchanged. The injected flags appear in the `$ ...` echo so the change
    #   is visible. Skipped when the grep redirect already spliced this
    #   command: the splice carries its own guards and the echo must not
    #   claim flags the executed search does not use.
    # Disable with TOFU_RUN_HARDEN_GREP=0.
    hardened = command if (stdin_callback or _grep_spliced is not None) \
        else _maybe_harden_grep_command(command)
    if hardened != command:
        logger.info('[run_command] Hardened grep: %s → %s',
                    command[:120], hardened[:160])
        _safe_on_chunk(on_chunk, 'stderr',
                       '[run_command] auto-added grep flags for FUSE/binary safety\n')
        command = hardened

    # Scanner segment budget: direct `find` remains a real streaming
    #   command, but cannot wedge forever in a FUSE/NFS syscall loop. The
    #   timeout wraps only the find segment, so downstream sort/head/xargs
    #   keep normal pipe backpressure. A caller-provided run_command timeout
    #   is already authoritative and suppresses this internal budget.
    _exec_source = _grep_spliced if _grep_spliced is not None else command

    _probe_source = _network_probe_execution_source(command)
    if _probe_source is not None:
        # Same complete plan as admission; no replay or approval/display rewrite.
        # First-option -q prevents implicit curlrc from adding hidden writes.
        _exec_source = _probe_source
    if _scan_timeout_plan is not None:
        _timeout_binary, _scan_seconds, _timeout_args = _scan_timeout_plan
        _exec_source, _bounded_scans = _bound_scan_segments(
            _exec_source, _timeout_binary, _scan_seconds, _timeout_args)
        if _bounded_scans:
            logger.info('[run_command] bounded %d find segment(s) to %ds '
                        '(cwd=%s): %.200s', _bounded_scans, _scan_seconds,
                        base, command)
            _safe_on_chunk(
                on_chunk, 'stderr',
                f'[run_command] find scan capped at {_scan_seconds}s '
                'to prevent network-filesystem stalls\n')

    # cp → parallel engine splice. A standalone cp segment inside a compound
    #   command is replaced by the owned parallel-copy helper (measured 2.9x
    #   on FUSE tree copies; rsync/tar-pipe measured SLOWER than plain cp).
    #   Only the provably-equivalent argv surface is rewritten — everything
    #   else keeps native cp byte-for-byte. A LONE cp command never reaches
    #   this: tools.py executes it in-process one layer up (zero spawn).
    #   Disable with TOFU_RUN_CP_FASTPATH=0.
    if os.environ.get('TOFU_RUN_CP_FASTPATH', '1') != '0':
        try:
            from lib.project_mod import copy_engine as _copy_engine
            _cp_spliced = _copy_engine.plan_cp_splice(_exec_source)
            if _cp_spliced is not None:
                logger.info('[run_command] routed cp segment(s) to the '
                            'parallel copy engine (cwd=%s): %.200s',
                            base, command)
                _exec_source = _cp_spliced
        except Exception as _cp_e:
            logger.debug('[run_command] cp splice planning skipped: %s',
                         _cp_e)

    # rm → trash rewrite: ordinary deletes become recoverable (moved to a
    #   per-workspace .tofu_trash/ instead of unlinked). Applied to the
    #   EXECUTED command only — the displayed/logged `command` stays clean so
    #   the model sees its own `rm ...` echoed back, not the shim. Catastrophic
    #   deletes are already rejected above and never reach here. The grep
    #   redirect splice and scanner budget ride the same exec-side-only seam.
    exec_command = _maybe_wrap_rm_with_trash(_exec_source, base)

    # Build the child environment once. Besides avoiding duplicate policy
    # work, this is the explicit seam where command egress can select one
    # direct/proxy route without mutating process-global environment state.
    # Only lightweight probes race; ``exec_command`` is still spawned once.
    try:
        child_env = _get_cmd_env(
            base, credential_env, strip_credential_vars,
            owner_user_id=(task or {}).get('_userId'),
            project_root=environment_root)
    except Exception as _env_e:
        logger.error(
            'run_command environment setup failed (cwd=%s): %s',
            base, _env_e, exc_info=True)
        return (f'$ {command}\n\n'
                f'Error starting command: {_env_e}\n'
                f'[exit code: -1]')

    if _probe_source is not None:
        # curl has environment-selected file sinks outside curlrc/argv too.
        # Explicit setup of these names does not qualify for the probe plan.
        for name in ('SSLKEYLOGFILE', 'QLOGDIR', 'CURL_MEMDEBUG'):
            child_env.pop(name, None)

    network_prepared = None
    network_env_baseline = dict(child_env)
    try:
        from lib.project_mod.network_command import (
            apply_network_environment,
            format_network_preflight_failure,
            prepare_network_command,
        )
        network_prepared = prepare_network_command(
            command, child_env, task=task)
        if network_prepared is not None and network_prepared.error:
            logger.warning(
                '[CommandNet] route unavailable target=%s routes=%s '
                'decision=%s',
                network_prepared.target.display_origin,
                network_prepared.attempted_route_ids,
                network_prepared.decision_reason)
            return format_network_preflight_failure(
                command, network_prepared)
        apply_network_environment(child_env, network_prepared)
        if network_prepared is not None and network_prepared.selected:
            route = network_prepared.route
            logger.info(
                '[CommandNet] selected route=%s mode=%s target=%s '
                'decision=%s probe_ms=%.0f',
                route.route_id, route.mode,
                network_prepared.target.display_origin,
                network_prepared.decision_reason,
                network_prepared.probe_ms)
            _safe_on_chunk(
                on_chunk, 'stderr',
                '[network] selected %s for %s (%s)\n'
                % (route.mode, network_prepared.target.display_origin,
                   network_prepared.decision_reason))
    except Exception as _network_e:
        # The network layer is an optimization/policy adapter, not a second
        # command authority. An internal planner fault preserves historical
        # inherited-environment execution and remains visible in operations.
        network_prepared = None
        child_env.clear()
        child_env.update(network_env_baseline)
        logger.warning(
            '[CommandNet] planning failed; using inherited route: %s',
            _network_e, exc_info=True)

    # Sticky-cwd capture (layer 2): wrap the command so its FINAL cwd is
    #   written to a dedicated temp file, preserving the exit code. Only when a
    #   sink is provided AND not sandboxed — a jailed TMPDIR would make the
    #   host-side read miss, so under portable_sandbox we skip capture and rely
    #   on the explicit-working_dir stickiness (layer 1) alone. Restricted probe
    #   here so the capture file's host path is valid.
    _capture_file = None
    if cwd_sink is not None and _sticky_cwd_capture_enabled():
        try:
            from lib.project_mod.abs_path_guard import is_restricted
            _restricted_now = is_restricted()
        except Exception as e:
            logger.debug('[RunCommand] is_restricted probe failed, assuming '
                         'unrestricted: %s', e)
            _restricted_now = False
        if not _restricted_now:
            try:
                import tempfile
                fd, _capture_file = tempfile.mkstemp(prefix='tofu_cwd_', suffix='.txt')
                os.close(fd)
                exec_command = _build_cwd_capture(exec_command, _capture_file)
            except OSError as _cap_e:
                logger.debug('[run_command] cwd capture setup skipped: %s', _cap_e)
                _capture_file = None

    shell_prefix = SHELL_PREFIX
    full_command = f'{shell_prefix} {exec_command}' if shell_prefix else exec_command

    python_cache_activation = None
    if _looks_like_python_command(command):
        try:
            from lib.project_mod.python_bytecode_cache import (
                prepare_python_bytecode_cache,
            )
            python_cache_activation = prepare_python_bytecode_cache(
                command, base, task, child_env)
        except Exception as _cache_e:
            logger.debug('[python-cache] planning skipped: %s', _cache_e)

    # Portable sandbox (restricted/agent-run principals only). On a host
    #   that allows it this wraps the command in a real isolation backend
    #   (bwrap/podman); on a locked host it is a no-op here and containment
    #   comes from the HOME/TMPDIR jail + rm shim applied in _get_cmd_env.
    #   Local desktop/CLI callers are NOT restricted → completely unaffected.
    try:
        from lib.project_mod.abs_path_guard import is_restricted
        if is_restricted() and base:
            from lib.project_mod import portable_sandbox
            full_command = portable_sandbox.wrap_command(
                full_command,
                base,
                writable_cache_dir=(
                    python_cache_activation.namespace
                    if python_cache_activation is not None else None
                ),
            )
    except Exception as _sb_e:  # never let sandbox wiring break command exec
        logger.debug('[run_command] portable_sandbox.wrap skipped: %s', _sb_e)

    timeout_str = f'{timeout}s' if timeout else 'unlimited'
    logger.info('run_command: $ %s  (timeout=%s, cwd=%s, interactive=%s)',
                command[:120], timeout_str, base, bool(stdin_callback))

    def _flush_cwd_capture():
        """Read the capture file (if any) and feed the sink. Best-effort."""
        if not _capture_file:
            return
        captured = _read_cwd_capture(_capture_file)
        if captured:
            try:
                cwd_sink(captured)
            except Exception as _e:
                logger.debug('[run_command] cwd_sink raised: %s', _e)

    def _finalize_command():
        _flush_cwd_capture()
        if python_cache_activation is None:
            return
        try:
            from lib.project_mod.python_bytecode_cache import (
                release_python_bytecode_cache,
            )
            release_python_bytecode_cache(python_cache_activation)
        except Exception as _cache_e:
            logger.debug('[python-cache] release skipped: %s', _cache_e)

    def _finalize_network_output(result_text):
        if network_prepared is None:
            return result_text
        try:
            from lib.project_mod.network_command import finalize_network_command
            return finalize_network_command(network_prepared, result_text)
        except Exception as _network_e:
            logger.warning(
                '[CommandNet] outcome finalization failed: %s',
                _network_e, exc_info=True)
            return result_text

    if input_plan is not None:
        try:
            from lib.project_mod.file_read_probe import run_observed_process
            from lib.compat import get_shell_args
            report = run_observed_process(input_plan.project_path,
                list(input_plan.expected_input_sha256), get_shell_args(full_command),
                timeout_seconds=timeout, task=task, on_chunk=on_chunk, on_spawn=on_spawn,
                runtime_context=runtime_context, child_env=child_env,
                expected_input_sha256=input_plan.expected_input_sha256,
                admission_check=lambda: input_plan.check_current(task),
                working_dir=base, display_command=command)
            return _finalize_network_output(report['evidence_result'])
        except Exception as exc:
            logger.debug('tool_run_command failure handled: %s', type(exc).__name__)
            return f'Error: Frozen-input observation refused or failed; inspect before retrying: {str(exc)[:300]}'
        finally:
            _finalize_command()

    from lib.project_mod.git_inspection_execution import (
        GitInspectionPreparationError, prepare_git_inspections,
    )
    preparation_finished = False
    try:
        # Source transformation is internal to execution. Approval, writer
        # claims, snapshots and display stay bound to the admitted command.
        with prepare_git_inspections(full_command, base, environment=child_env) as execution_full_command:
            preparation_finished = True
            if not stdin_callback:
                result_text = _run_command_simple(
                    command, execution_full_command, timeout, base, task=task,
                    on_chunk=on_chunk, on_spawn=on_spawn,
                    runtime_context=runtime_context, child_env=child_env,
                    credential_env=credential_env,
                    strip_credential_vars=strip_credential_vars)
            else:
                result_text = _run_command_interactive(
                    command, execution_full_command, timeout, base, stdin_callback,
                    on_chunk=on_chunk, task=task, on_spawn=on_spawn,
                    runtime_context=runtime_context, child_env=child_env,
                    credential_env=credential_env,
                    strip_credential_vars=strip_credential_vars)
            return _finalize_network_output(result_text)
    except GitInspectionPreparationError as error:
        if preparation_finished:
            raise  # Exceptions in the executed body are never admission refusals.
        return f'Error [git-inspection-preparation]: {error}. Command was not started.'
    finally:
        _finalize_command()


def _pop_cmd_interrupt(task, proc_pid=None):
    """Consume a pending PER-COMMAND interrupt request, if any.

    ``task['_cmd_interrupt']`` is set by the interrupt-command endpoint
    (``source='user'``, routes/chat_poll_abort.py) or by the stuck-task
    reaper (``source='watchdog'`` with a ``note`` like "no output for
    1818s" — lib/tasks_pkg/manager/_maintenance.py). Returns the
    ``interrupted_by`` label for the result marker, or None when no
    interrupt was requested. POPPED on read so a LATER command in the same
    task never sees a stale request. Unlike ``task['aborted']`` this never
    stops the task — the partial result goes back to the model and the
    turn continues.

    ``proc_pid`` guards a microsecond race: the flag can be planted AFTER
    this runner's final check but BEFORE ``_subprocess_pid`` is popped —
    without a pid match the NEXT command in the same task would consume a
    request meant for an already-finished one and die at spawn. Planters
    stamp the pid they saw; a flag stamped for a DIFFERENT pid is stale
    (its command already exited) and is voided, never honoured.
    """
    if not task:
        return None
    intr = task.get('_cmd_interrupt')
    if not intr:
        return None
    if isinstance(intr, dict):
        flag_pid = intr.get('pid')
        if flag_pid is not None and proc_pid is not None and flag_pid != proc_pid:
            task.pop('_cmd_interrupt', None)   # stale — void, never honour
            logger.debug('[run_command] voided stale _cmd_interrupt for pid=%s '
                         '(current=%s)', flag_pid, proc_pid)
            return None
        src = intr.get('source') or 'user'
        note = intr.get('note') or ''
    else:
        src, note = 'user', ''
    task.pop('_cmd_interrupt', None)
    if src == 'watchdog':
        base = 'stall-watchdog'
        if note:
            base += f': {note}'
        return (f'{base} — partial output above; the task was NOT stopped. '
                'The command may have partially taken effect. Inspect output '
                'and current state before repeating any operation; do not '
                'automatically replay writes. Continue independent work. '
                'For a verified safe retry, narrow the scope or use background execution.')
    return 'user'


def _safe_on_spawn(on_spawn, timeout, *, deadline_epoch=None):
    """Invoke ``on_spawn(exec_start_ms, deadline_ms)``, swallowing any exception.

    Called at the ONE instant both facts are true: the subprocess exists, and
    ``timeout`` already carries the effective budget (post cross-DC multiplier,
    post clamp). ``deadline_ms`` is None for an unbounded command.

    Mirrors ``_safe_on_chunk``: the callback comes from the SSE layer, and a
    bug in event emission MUST NOT abort the subprocess.
    """
    if not on_spawn:
        return
    try:
        now_ms = time.time() * 1000.0
        deadline_ms = (now_ms + timeout * 1000.0) if timeout else None
        if deadline_epoch is not None:
            deadline_ms = min(deadline_ms, deadline_epoch * 1000.0) if deadline_ms else deadline_epoch * 1000.0
        on_spawn(now_ms, deadline_ms)
    except Exception as e:
        logger.debug('[run_command] on_spawn callback raised: %s', e)


def _safe_on_chunk(on_chunk, stream, text):
    """Invoke an on_chunk callback, swallowing any exception.

    The callback is user-supplied (comes from the SSE layer).  A bug in the
    frontend-event emission MUST NOT abort the subprocess.

    Also the LIVENESS heartbeat for long commands. Both run loops funnel every
    output chunk through here, so it is the one place that proves "this
    subprocess is still producing" — the signal a swarm sub-agent has no other
    way to emit while blocked inside a tool (measured: a ``pytest`` child ran
    >1h and the agent looked silent the whole time). The heartbeat is a no-op
    outside a swarm, and deliberately sits ABOVE the ``on_chunk`` guard below:
    output proves life whether or not anyone subscribed to it.
    """
    if text:
        try:
            from lib.swarm.liveness import notify_tool_progress
            notify_tool_progress('subprocess_output')
        except Exception as e:
            logger.debug('[run_command] liveness heartbeat skipped: %s', e)
    if not on_chunk or not text:
        return
    try:
        on_chunk(stream, text)
    except Exception as e:
        logger.debug('[run_command] on_chunk callback raised: %s', e)


class _BoundedCommandStream:
    """Bound subprocess capture while the full stream flows through on_chunk."""

    def __init__(self, limit=MAX_COMMAND_OUTPUT):
        self.limit = max(1, int(limit))
        self.head_limit = self.limit * 3 // 4
        self.tail_limit = max(1, self.limit - self.head_limit)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def append(self, chunk):
        data = bytes(chunk or b'')
        if not data:
            return
        self.total += len(data)
        room = self.head_limit - len(self.head)
        if room > 0:
            self.head.extend(data[:room])
            data = data[room:]
        if data:
            self.tail.extend(data)
            if len(self.tail) > self.tail_limit:
                del self.tail[:-self.tail_limit]

    def render(self):
        if self.total <= self.limit:
            return bytes(self.head + self.tail)
        marker = (
            f'\n\n… [stream capture truncated: {self.total:,} bytes total] …\n\n'
        ).encode('utf-8')
        return bytes(self.head) + marker + bytes(self.tail)


def run_command_process(argv, *, command, timeout, base, task=None,
                        on_chunk=None, on_spawn=None, runtime_context=None,
                        child_env, namespace_supervisor=False, deadline_epoch=None, process_guard=None, extra_pass_fds=()):
    """Run trusted structured launch arguments through the command lifecycle.

    Internal adapters own argv/environment admission. This port never parses
    argv as a host shell or retries a failed launch with the display command.
    It shares bounded output, Stop, command interruption, timeout and settlement
    with ordinary project commands; it grants no project write publication.
    """
    if (not isinstance(argv, (tuple, list)) or not argv
            or not all(isinstance(arg, str) and '\0' not in arg for arg in argv)
            or not os.path.isabs(argv[0])):
        raise ValueError('process launch requires absolute structured arguments')
    if process_guard is not None and not callable(process_guard):
        raise ValueError('process guard must be callable')
    if (not isinstance(extra_pass_fds, (tuple,list)) or any(type(fd) is not int or fd < 3 for fd in extra_pass_fds)):
        raise ValueError('borrowed process descriptors must be integer FDs above standard streams')
    if extra_pass_fds and os.name != 'posix':
        raise ValueError('borrowed process descriptors require POSIX')
    for fd in extra_pass_fds: os.fstat(fd)
    return _run_command_simple(
        command, '', timeout, base, task=task, on_chunk=on_chunk,
        on_spawn=on_spawn, runtime_context=runtime_context, child_env=child_env,
        process_argv=tuple(argv), namespace_supervisor=namespace_supervisor,
        deadline_epoch=deadline_epoch, process_guard=process_guard, extra_pass_fds=tuple(extra_pass_fds))


def _run_command_simple(command, full_command, timeout, base, task=None, on_chunk=None,
                        on_spawn=None, runtime_context=None, child_env=None,
                        credential_env=None,
                        strip_credential_vars=None, process_argv=None,
                        namespace_supervisor=False, deadline_epoch=None, process_guard=None, extra_pass_fds=()):
    """Execute command with abort-awareness + incremental output streaming.

    Reads stdout/stderr in non-blocking 64 KB chunks using ``safe_select_pipes``
    (same primitive as the interactive path).  Each chunk is appended to the
    accumulator AND forwarded to ``on_chunk(stream, text)`` if provided, so
    callers can stream output to the frontend as it arrives instead of
    waiting for the command to finish.

    When *task* is provided, the subprocess PID is stored on the task dict
    so the abort handler can kill it directly.  The loop checks
    ``task['aborted']`` on every tick (~0.2s) and terminates if set.
    """
    from lib.compat import get_shell_args, safe_select_pipes, set_pipe_nonblocking
    from lib.tools.process_environment import validate_environment
    if ((task is not None and task.get('aborted')) or (runtime_context is not None
            and runtime_context.cancellation_requested)):
        return _format_run_output(command, '', '', -1, aborted=True)
    if deadline_epoch is not None and time.time() >= deadline_epoch:
        return _format_run_output(command, '', '', -1, timed_out=True)
    inherited_lease_fds = workspace_lease_fds()
    try:
        proc = subprocess.Popen(
            process_argv if process_argv is not None else get_shell_args(full_command),
            **({'pass_fds': tuple(dict.fromkeys((*inherited_lease_fds, *extra_pass_fds)))} if os.name == 'posix' else {}),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=False,  # binary mode for non-blocking I/O
            cwd=base,
            env=validate_environment(child_env if child_env is not None else _get_cmd_env(
                base, credential_env, strip_credential_vars)),
            start_new_session=True,  # own process group for clean kill
        )
    except (FileNotFoundError, NotADirectoryError) as e:
        logger.warning('run_command: cannot start (cwd=%s): %s', base, e)
        return (f'$ {command}\n\n'
                f'Error starting command: {e}\n'
                f'[exit code: -1]')
    except Exception as e:
        logger.error('run_command Popen error (cwd=%s): %s', base, e, exc_info=True)
        return (f'$ {command}\n\n'
                f'Error starting command: {e}\n'
                f'[exit code: -1]')

    if os.name == 'posix':
        # start_new_session=True establishes this identity even if the leader
        # exits before cancellation reaches cleanup; descendants retain it.
        proc._tofu_owned_pgid = proc.pid

        proc._tofu_inherited_workspace_lease = bool(inherited_lease_fds)
    # Register the process before invoking callbacks. A callback may publish or
    # persist live state; during that work Stop must already have a PID/PGID it
    # can signal.
    if task is not None:
        task['_subprocess_pid'] = proc.pid
        task['_subprocess_pgid'] = getattr(proc, '_tofu_owned_pgid', None)

    if process_guard is not None:
        try:
            process_guard(proc)
        except Exception as exc:
            _kill_process_tree(proc, namespace_supervisor=namespace_supervisor)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None: stream.close()
            if task is not None and task.get('_subprocess_pid') == proc.pid:
                task.pop('_subprocess_pid', None);task.pop('_subprocess_pgid', None)
            return _format_run_output(command, '',
                f'Process guard failed after allocation: {type(exc).__name__}', -1)

    # Spawn clock — the subprocess now EXISTS and `timeout` already holds the
    # effective budget, so this is the earliest and only point at which a
    # truthful deadline can be published.
    _safe_on_spawn(on_spawn, timeout, deadline_epoch=deadline_epoch)

    # Set stdout/stderr to non-blocking.  On platforms where this fails

    unregister_cancel = None
    # (Windows), safe_select_pipes degrades to short-timeout polling.
    nonblocking_ok = all(
        set_pipe_nonblocking(fd) for fd in (proc.stdout, proc.stderr)
    )
    if not nonblocking_ok:
        logger.debug('run_command: non-blocking pipe setup failed — using polling I/O')

    stdout_chunks = _BoundedCommandStream()
    stderr_chunks = _BoundedCommandStream()
    start_time = time.monotonic()
    timed_out = False
    aborted = False
    interrupted_by = ''

    def _drain_after_kill():
        """Best-effort tail drain after a kill — grab whatever was already
        buffered in the pipe before SIGTERM landed."""
        for fd, bucket, sname in (
            (proc.stdout, stdout_chunks, 'stdout'),
            (proc.stderr, stderr_chunks, 'stderr'),
        ):
            try:
                rest = fd.read()
            except (BlockingIOError, OSError, ValueError) as _e_audit:
                logger.debug('[tools] _drain_after_kill caught %s: %s', type(_e_audit).__name__, _e_audit)
                rest = None
            if rest:
                bucket.append(rest)
                _safe_on_chunk(on_chunk, sname,
                               rest.decode('utf-8', errors='replace'))

    try:
        if runtime_context is not None:
            unregister_cancel = runtime_context.register_cancel_callback(
                lambda: _signal_process_tree_termination(
                    proc, namespace_supervisor=namespace_supervisor))
        while True:
            # ── timeout ──
            elapsed = time.monotonic() - start_time
            if ((timeout is not None and elapsed >= timeout)
                    or (deadline_epoch is not None and time.time() >= deadline_epoch)):
                logger.info('run_command timed out after %ss — killing PID %d',
                            timeout, proc.pid)
                _kill_process_tree(proc, namespace_supervisor=namespace_supervisor)
                _drain_after_kill()
                timed_out = True
                break

            # ── abort ──
            if ((task and task.get('aborted')) or (runtime_context is not None
                    and runtime_context.cancellation_requested)):
                logger.info('[run_command] Task aborted — killing subprocess PID %d: %s',
                            proc.pid, command[:80])
                _kill_process_tree(proc, namespace_supervisor=namespace_supervisor)
                _drain_after_kill()
                aborted = True
                break

            # ── per-command interrupt (user button / stall watchdog) ──
            #   Unlike abort this does NOT touch task['aborted']: the partial
            #   result goes back to the model and the turn CONTINUES.
            _intr_by = _pop_cmd_interrupt(task, proc.pid)
            if _intr_by:
                logger.info('[run_command] Command interrupted (%s) — killing '
                            'subprocess PID %d: %s',
                            _intr_by[:60], proc.pid, command[:80])
                _kill_process_tree(proc, namespace_supervisor=namespace_supervisor)
                _drain_after_kill()
                interrupted_by = _intr_by
                break

            retcode = proc.poll()

            # ── drain available output ──
            got_output = False
            try:
                readable = safe_select_pipes(
                    [proc.stdout, proc.stderr], timeout=0.2
                )
            except (ValueError, OSError) as _e_audit:
                logger.debug('[tools] _run_command_simple caught %s: %s', type(_e_audit).__name__, _e_audit)
                readable = []

            for fd in readable:
                try:
                    chunk = fd.read(65536)
                except (BlockingIOError, OSError) as _e_audit:
                    logger.debug('[tools] _run_command_simple caught %s: %s', type(_e_audit).__name__, _e_audit)
                    chunk = None
                if chunk:
                    got_output = True
                    if fd is proc.stdout:
                        stdout_chunks.append(chunk)
                        _safe_on_chunk(on_chunk, 'stdout',
                                       chunk.decode('utf-8', errors='replace'))
                    else:
                        stderr_chunks.append(chunk)
                        _safe_on_chunk(on_chunk, 'stderr',
                                       chunk.decode('utf-8', errors='replace'))

            # ── exit condition: process ended and no more buffered data ──
            if retcode is not None and not got_output and getattr(proc, '_tofu_inherited_workspace_lease', False):
                from lib.project_mod.process_group import group_has_live_members
                if group_has_live_members(proc._tofu_owned_pgid):
                    time.sleep(0.02)  # Pipes may already be EOF; avoid a hot observation loop.
                    continue  # The admitted writer still owns live inheriting descendants.
            if retcode is not None and not got_output:
                for fd, bucket, sname in (
                    (proc.stdout, stdout_chunks, 'stdout'),
                    (proc.stderr, stderr_chunks, 'stderr'),
                ):
                    try:
                        rest = fd.read()
                    except (BlockingIOError, OSError) as _e_audit:
                        logger.debug('[tools] _run_command_simple caught %s: %s', type(_e_audit).__name__, _e_audit)
                        rest = None
                    if rest:
                        bucket.append(rest)
                        _safe_on_chunk(on_chunk, sname,
                                       rest.decode('utf-8', errors='replace'))
                break
    except Exception as e:
        logger.error('run_command loop error: %s', e, exc_info=True)
        try:
            _kill_process_tree(proc, namespace_supervisor=namespace_supervisor)
        except Exception as ke:
            logger.debug('run_command: _kill_process_tree during cleanup failed: %s', ke)
        # Clean up task ref
        if task is not None:
            task.pop('_subprocess_pid', None)
            task.pop('_subprocess_pgid', None)
        return (f'$ {command}\n\n'
                f'Error executing command: {e}\n'
                f'[exit code: -1]')
    finally:
        for fd in (proc.stdout, proc.stderr):
            try:
                fd.close()
            except (OSError, AttributeError) as _e_audit:
                logger.debug('[tools] _run_command_simple caught %s: %s', type(_e_audit).__name__, _e_audit)
                pass

        if unregister_cancel is not None:
            unregister_cancel()
    # Clean up task ref
    if task is not None:
        task.pop('_subprocess_pid', None)
        task.pop('_subprocess_pgid', None)

    _owned_command_quiesced()
    stdout = stdout_chunks.render().decode('utf-8', errors='replace')
    stderr = stderr_chunks.render().decode('utf-8', errors='replace')

    if interrupted_by:
        return _format_run_output(command, stdout, stderr, -1,
                                  interrupted_by=interrupted_by)
    if timed_out:
        return _format_run_output(command, stdout, stderr, -1, timed_out=True)
    if aborted:
        return _format_run_output(command, stdout, stderr, -1,
                                  timed_out=False, aborted=True)

    logger.info('run_command done: exit=%d, stdout=%dch, stderr=%dch',
                proc.returncode, len(stdout), len(stderr))
    return _format_run_output(command, stdout, stderr, proc.returncode)


def _signal_namespace_child(proc):
    """Kill the supervised namespace init through a race-safe host pidfd.

    The trusted launch is prlimit -> unshare --fork, with one direct namespace
    child. Killing that init also kills detached namespace descendants. Leave
    unshare alive to wait/reap it; a process-group signal would kill the waiter.
    """
    import signal
    from rootless_vm.pidfd import open_process, send_signal

    if proc.poll() is not None:
        return False
    try:
        with open(f'/proc/{proc.pid}/task/{proc.pid}/children', encoding='ascii') as handle:
            children = handle.read().split()
        if len(children) != 1:
            return False
        child = int(children[0])
        descriptor = open_process(child)
        try:
            # Recheck ancestry after acquiring the stable handle. Never signal
            # a recycled PID or an unrelated process supplied by guest output.
            with open(f'/proc/{proc.pid}/task/{proc.pid}/children', encoding='ascii') as handle:
                if str(child) not in handle.read().split():
                    return False
            send_signal(descriptor, signal.SIGKILL)
            return True
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return False


def _signal_process_tree_termination(proc, *, namespace_supervisor=False):
    """Request process-group termination without waiting on the caller thread."""
    import signal

    if proc.poll() is not None:
        return
    if namespace_supervisor:
        # A cancel racing namespace bootstrap remains visible to the read loop.
        # Do not kill the supervising waiter before it can reap its child.
        _signal_namespace_child(proc)
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass


def _kill_process_tree(proc, *, namespace_supervisor=False):
    """Retire the owned group and inherited resources, not merely its leader."""
    guard = getattr(proc, '_file_read_cleanup', None)
    if callable(guard) and not namespace_supervisor:
        try:
            guard(proc)
            # A settled tracer/leader is not proof inherited group FDs closed.
        except Exception as exc:
            logger.warning('File-read tracer cleanup failed: %s', type(exc).__name__)
    pid = proc.pid
    if namespace_supervisor:
        deadline = time.monotonic() + 3
        while proc.poll() is None and time.monotonic() < deadline:
            if _signal_namespace_child(proc):
                try:
                    proc.wait(timeout=3)
                    return
                except subprocess.TimeoutExpired:
                    break
            time.sleep(0.02)
    if os.name == 'posix':
        from lib.project_mod.process_group import retire_process_group
        pgid = getattr(proc, '_tofu_owned_pgid', None)
        if pgid is None:
            try:
                pgid = os.getpgid(pid)
            except ProcessLookupError:
                proc.wait(timeout=2)
                return  # Unregistered direct caller has no surviving group witness.
        retire_process_group(pgid, wait_leader=lambda seconds: proc.wait(timeout=seconds))
        logger.info('[run_command] Retired process group pgid=%d (pid=%d)', pgid, pid)
        return
    # Windows lacks POSIX groups; keep its direct-child compatibility boundary.
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=2)


# ─────────────────────────────────────────────────────────────────────────
# Non-silent `grep -r` hardening for run_command
# ─────────────────────────────────────────────────────────────────────────
# Real grep is kept (no rewrite to rg).  We only inject flags the user did
# not already pass:
#   -I                       — skip binary files
#   --exclude-dir=<name>     — one per IGNORE_DIRS entry (mirrors grep_search)
#   --color=never            — keeps output stable for downstream pipes
# Activated only when:
#   - the command's first token is a bare grep family binary (grep / egrep /
#     fgrep, with optional path prefix like /usr/bin/grep)
#   - the user passed `-r` / `-R` / `--recursive` (non-recursive grep doesn't
#     suffer from the FUSE timeout problem and the injected exclude-dir flags
#     would be useless)
#   - the command contains no unquoted shell metachars that imply a pipeline
#     (`|`, `;`, `&`, `<`, `>`, backtick, `$(`) — we never modify pipelines.
#   - shlex.split parses cleanly — malformed quoting bails to no-op.
# Kill switch: TOFU_RUN_HARDEN_GREP=0.

_GREP_HARDEN_BINARIES = frozenset({'grep', 'egrep', 'fgrep'})


def _maybe_harden_grep_command(command):
    """Return a possibly-augmented copy of ``command`` for grep -r invocations.

    See the block comment above for activation conditions.  On any uncertainty
    (parse error, pipeline detected, env-var off, non-grep command, no -r) we
    return the input unchanged.
    """
    if os.environ.get('TOFU_RUN_HARDEN_GREP', '1') == '0':
        return command
    if not command or _has_unquoted_shell_metachars(command):
        return command
    try:
        import shlex
        tokens = shlex.split(command, posix=True)
    except ValueError as _e_audit:
        logger.debug('[tools] _maybe_harden_grep_command caught %s: %s', type(_e_audit).__name__, _e_audit)
        return command
    if not tokens:
        return command
    head = tokens[0]
    head_base = os.path.basename(head)
    if head_base not in _GREP_HARDEN_BINARIES:
        return command

    # Look at the flag tokens (everything starting with '-' before a non-flag
    # token that isn't an argument to a flag).  We only need to know what's
    # already specified — we don't try to re-interpret the user's command.
    has_recursive = False
    has_I = False
    has_color = False
    has_exclude_dirs = set()
    for tok in tokens[1:]:
        if not tok.startswith('-'):
            continue
        if tok in ('-r', '-R', '--recursive', '--dereference-recursive'):
            has_recursive = True
            continue
        # Combined short flags like `-rni` — scan each char.
        if tok.startswith('-') and not tok.startswith('--') and len(tok) > 1:
            for ch in tok[1:]:
                if ch in ('r', 'R'):
                    has_recursive = True
                if ch == 'I':
                    has_I = True
        if tok == '-I':
            has_I = True
        if tok.startswith('--color'):
            has_color = True
        if tok.startswith('--exclude-dir'):
            # Form: --exclude-dir=foo  or  --exclude-dir foo  (latter is the
            # next token; we don't bother tracking it precisely — we only use
            # this set to skip duplicate injections).
            if '=' in tok:
                has_exclude_dirs.add(tok.split('=', 1)[1].strip("'\""))

    if not has_recursive:
        # Without -r grep doesn't walk a tree; the timeout class doesn't apply.
        return command

    inject = []
    if not has_I:
        inject.append('-I')
    if not has_color:
        inject.append('--color=never')
    for d in sorted(IGNORE_DIRS):
        if d in has_exclude_dirs:
            continue
        inject.append(f'--exclude-dir={d}')

    if not inject:
        return command

    # Insert injected flags right after the grep binary token.  We do this on
    # the original command STRING (not the shlex-split tokens) to preserve the
    # user's exact quoting of pattern + paths.
    # Find the end of the head token in the original string.
    stripped = command.lstrip()
    leading_ws = command[:len(command) - len(stripped)]
    # head token may be quoted; find its end by walking with the same quote
    # tracker used for metachar detection.
    end = 0
    in_single = in_double = False
    while end < len(stripped):
        ch = stripped[end]
        if ch == '\\' and not in_single and end + 1 < len(stripped):
            end += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double and ch.isspace():
            break
        end += 1
    head_str = stripped[:end]
    rest_str = stripped[end:]
    return f'{leading_ws}{head_str} {" ".join(inject)}{rest_str}'


# Commands that read stdin as a data source (piped input) rather than for
# interactive prompting.  When these are detected as "reading stdin", it's
# almost always a false positive — they inherited our stdin pipe and are
# treating it as a data stream (e.g. `rg` reads stdin when it's not a tty).
_NON_INTERACTIVE_COMMANDS = frozenset({
    'rg', 'grep', 'egrep', 'fgrep', 'ag', 'ack',
    'sort', 'uniq', 'wc', 'head', 'tail', 'cat', 'tac', 'rev',
    'awk', 'gawk', 'mawk', 'sed', 'tr', 'cut', 'paste', 'join',
    'xargs', 'tee', 'comm', 'diff', 'patch',
    'jq', 'yq', 'csvtool', 'column',
    'md5sum', 'sha256sum', 'sha1sum', 'base64',
    'less', 'more', 'bat', 'hexdump', 'xxd', 'od',
    'perl', 'ruby',  # often used as one-liners in pipes
})


# Sentinel returned when only non-interactive commands are reading stdin.
# The caller should close stdin (send EOF) so they can proceed.
_STDIN_NON_INTERACTIVE = 'non_interactive'


def _is_any_child_reading_stdin(parent_pid, stdin_pipe_ino, *, stop_event=None):
    """Check if any descendant of *parent_pid* is blocked in read(2) on our stdin pipe.

    Uses ``/proc/<pid>/syscall`` to definitively detect:
      - syscall_nr == 0 (read)
      - arg0 == 0 (fd 0 = stdin)
    combined with verifying that the child's fd 0 inode matches our stdin
    pipe inode (to avoid false positives from unrelated processes).

    Excludes known non-interactive commands (rg, grep, sort, etc.) that read
    stdin as a data source rather than for user interaction.

    Returns:
        - ``(pid, comm)`` tuple if an interactive process is reading stdin
        - ``_STDIN_NON_INTERACTIVE`` if only non-interactive commands are
          reading stdin (caller should close stdin to send EOF)
        - ``None`` if no process is reading our stdin pipe

    **Linux-only**: requires /proc filesystem. Returns None on macOS/Windows.
    """
    from lib.compat import HAS_PROCFS
    if not HAS_PROCFS:
        return None  # stdin detection unavailable on this platform
    try:
        pids_to_check = _collect_descendants(parent_pid)
    except OSError as e:
        logger.debug('[StdinDetect] _collect_descendants failed: %s', e)
        return None

    found_non_interactive = False

    for pid in pids_to_check:
        if stop_event is not None and stop_event.is_set():
            return None
        try:
            # Does this process's fd 0 point to our stdin pipe?
            fd0_ino = os.stat(f'/proc/{pid}/fd/0').st_ino
            if fd0_ino != stdin_pipe_ino:
                continue

            # Read the current syscall
            with open(f'/proc/{pid}/syscall') as f:
                sc = f.read().strip()
            if stop_event is not None and stop_event.is_set():
                return None
            parts = sc.split()
            if not parts or parts[0] == 'running':
                continue
            syscall_nr = int(parts[0])
            arg0 = int(parts[1], 16)

            # syscall 0 = read, arg0 = 0 means fd 0 (stdin)
            if syscall_nr == 0 and arg0 == 0:
                try:
                    with open(f'/proc/{pid}/comm') as f:
                        comm = f.read().strip()
                except OSError as _e_audit:
                    logger.debug('[tools] _is_any_child_reading_stdin caught %s: %s', type(_e_audit).__name__, _e_audit)
                    comm = '?'

                # Skip known non-interactive commands that read stdin as
                # a data source.  These inherit our stdin pipe but are NOT
                # prompting the user — they just treat stdin as input data.
                if comm in _NON_INTERACTIVE_COMMANDS:
                    logger.debug('[StdinDetect] Ignoring non-interactive %s '
                                 '(pid=%d) reading stdin — data consumer, '
                                 'not interactive prompt', comm, pid)
                    found_non_interactive = True
                    continue

                return (pid, comm)
        except (OSError, ValueError, IndexError) as _e_audit:
            # Process may have exited between checks — harmless
            logger.debug('[tools] _is_any_child_reading_stdin caught %s: %s', type(_e_audit).__name__, _e_audit)
            continue

    # If we found non-interactive readers but no interactive ones,
    # signal the caller to close stdin so they get EOF and can proceed.
    if found_non_interactive:
        return _STDIN_NON_INTERACTIVE
    return None


def _collect_descendants(parent_pid):
    """Return list of all descendant PIDs (children, grandchildren, …) including parent.

    **Linux-only**: requires /proc filesystem. On macOS/Windows, returns
    only the parent PID (no descendant walking).
    """
    from lib.compat import HAS_PROCFS
    # Build a quick pid→children map from /proc
    children_map = {}  # ppid → [pid, …]
    if not HAS_PROCFS:
        return [parent_pid]
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            with open(f'/proc/{pid}/stat') as f:
                stat_line = f.read()
            # PPID is field 4 (after the comm field which is in parens)
            ppid = int(stat_line.split(')')[-1].split()[1])
            children_map.setdefault(ppid, []).append(pid)
        except (OSError, ValueError, IndexError) as _e_audit:
            logger.debug('[tools] _collect_descendants caught %s: %s', type(_e_audit).__name__, _e_audit)
            pass  # Expected: process may exit between readdir and stat

    # BFS from parent_pid
    result = [parent_pid]
    queue = [parent_pid]
    while queue:
        p = queue.pop()
        for child in children_map.get(p, []):
            result.append(child)
            queue.append(child)
    return result


def _run_command_interactive(command, full_command, timeout, base, stdin_callback,
                              on_chunk=None, task=None, on_spawn=None,
                              runtime_context=None, child_env=None,
                              credential_env=None,
                              strip_credential_vars=None):
    """Popen-based execution with stdin detection and interactive input.

    When *task* is provided, the subprocess PID/PGID is stored on the task
    dict (so the abort handler can SIGTERM it directly) and the loop checks
    ``task['aborted']`` every tick — mirroring ``_run_command_simple``. Without
    this the interactive path was NOT cooperatively abort-aware: a Stop while a
    long command ran left an unkillable subprocess and a tool round stuck in
    "Running…".


    Uses non-blocking I/O on stdout/stderr.  On Linux, periodically checks
    ``/proc/<pid>/syscall`` to definitively detect when a child process
    is blocked reading from our stdin pipe.  On macOS/Windows, stdin
    detection is disabled (commands run non-interactively).
    """
    from lib.compat import get_shell_args, set_pipe_nonblocking
    from lib.tools.process_environment import validate_environment
    inherited_lease_fds = workspace_lease_fds()
    try:
        proc = subprocess.Popen(
            get_shell_args(full_command),
            **({'pass_fds': inherited_lease_fds} if os.name == 'posix' else {}),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=base,
            env=validate_environment(child_env if child_env is not None else _get_cmd_env(
                base, credential_env, strip_credential_vars)),
            text=False,  # binary mode for non-blocking I/O
            start_new_session=True,  # own process group for clean kill
        )
    except (FileNotFoundError, NotADirectoryError) as e:
        # Bad cwd / missing shell binary — user-error, not a bug. Keep log
        # concise (no traceback) so error.log isn't flooded when callers
        # pass a non-existent project path.
        logger.warning('run_command: cannot start (cwd=%s): %s', base, e)
        return (f'$ {command}\n\n'
                f'Error starting command: {e}\n'
                f'[exit code: -1]')
    except Exception as e:
        logger.error('run_command Popen error (cwd=%s): %s', base, e, exc_info=True)
        return (f'$ {command}\n\n'
                f'Error starting command: {e}\n'
                f'[exit code: -1]')

    if os.name == 'posix':
        proc._tofu_owned_pgid = proc.pid  # This Popen also owns a new session.

        proc._tofu_inherited_workspace_lease = bool(inherited_lease_fds)
    # Register before callbacks/setup so Stop can signal the process throughout
    # every post-Popen operation, including live-state persistence.
    if task is not None:
        task['_subprocess_pid'] = proc.pid
        task['_subprocess_pgid'] = getattr(proc, '_tofu_owned_pgid', None)

    # Set stdout/stderr to non-blocking (no-op on Windows, uses threading there).
    # If this fails on an unusual platform, interactive I/O will still work via
    # polling with small timeouts — it just won't be as responsive.
    nonblocking_ok = all(
        set_pipe_nonblocking(fd) for fd in (proc.stdout, proc.stderr)
    )
    if not nonblocking_ok:
        logger.warning('run_command: non-blocking pipe setup failed — falling back to polling I/O')

    # Same contract as _run_command_simple; PID/PGID is already visible.
    _safe_on_spawn(on_spawn, timeout)

    # Get the inode of our stdin pipe so we can match it in /proc

    unregister_cancel = None
    if runtime_context is not None:
        unregister_cancel = runtime_context.register_cancel_callback(
            lambda: _signal_process_tree_termination(proc))

    try:
        stdin_pipe_ino = os.fstat(proc.stdin.fileno()).st_ino
    except OSError as _e_audit:
        logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
        stdin_pipe_ino = None

    stdout_chunks = _BoundedCommandStream()
    stderr_chunks = _BoundedCommandStream()
    start_time = time.monotonic()
    stdin_closed = False
    timed_out = False
    aborted = False
    interrupted_by = ''
    stdin_probe = None

    try:
        from lib.compat import HAS_PROCFS
        from lib.project_mod.stdin_probe import StdinProbe
        if HAS_PROCFS and stdin_pipe_ino is not None:
            try:
                stdin_probe = StdinProbe(_is_any_child_reading_stdin, proc.pid, stdin_pipe_ino)
            except Exception as exc:
                logger.debug('stdin sensor unavailable: %s', type(exc).__name__)
        while True:
            # ── abort: user clicked Stop (cooperative, ~0.2s granularity) ──
            if task and task.get('aborted'):
                logger.info('[run_command] Task aborted — killing interactive subprocess '
                            'PID %d: %s', proc.pid, command[:80])
                _kill_process_tree(proc)
                aborted = True
                break

            # ── per-command interrupt (user button / stall watchdog) ──
            _intr_by = _pop_cmd_interrupt(task, proc.pid)
            if _intr_by:
                logger.info('[run_command] Interactive command interrupted (%s) — '
                            'killing subprocess PID %d: %s',
                            _intr_by[:60], proc.pid, command[:80])
                _kill_process_tree(proc)
                interrupted_by = _intr_by
                break

            # Check timeout
            elapsed = time.monotonic() - start_time
            if timeout and elapsed > timeout:
                # Expected outcome of user-declared timeout budget —
                # caller already surfaces [TIMEOUT] in stdout.
                logger.info('run_command timed out after %ss (interactive)', timeout)
                timed_out = True
                _kill_process_tree(proc)
                break

            # Check if process has finished
            retcode = proc.poll()

            # Read available stdout/stderr (non-blocking)
            got_output = False
            from lib.compat import safe_select_pipes
            try:
                readable = safe_select_pipes(
                    [proc.stdout, proc.stderr], timeout=0.2
                )
            except (ValueError, OSError) as _e_audit:
                # fd already closed
                logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
                readable = []

            for fd in readable:
                try:
                    chunk = fd.read(65536)
                    if chunk:
                        got_output = True
                        if fd is proc.stdout:
                            stdout_chunks.append(chunk)
                            _safe_on_chunk(on_chunk, 'stdout',
                                           chunk.decode('utf-8', errors='replace'))
                        else:
                            stderr_chunks.append(chunk)
                            _safe_on_chunk(on_chunk, 'stderr',
                                           chunk.decode('utf-8', errors='replace'))
                except (BlockingIOError, OSError) as _e_audit:
                    logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
                    pass

            if retcode is not None and not got_output and getattr(proc, '_tofu_inherited_workspace_lease', False):
                from lib.project_mod.process_group import group_has_live_members
                if group_has_live_members(proc._tofu_owned_pgid):
                    time.sleep(0.02)
                    continue  # The same owned writer lifetime applies to interactive I/O.
            if retcode is not None and not got_output:
                # Process exited and no more data — drain remaining
                try:
                    rest_out = proc.stdout.read()
                    if rest_out:
                        stdout_chunks.append(rest_out)
                        _safe_on_chunk(on_chunk, 'stdout',
                                       rest_out.decode('utf-8', errors='replace'))
                except (BlockingIOError, OSError) as _e_audit:
                    logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
                    pass
                try:
                    rest_err = proc.stderr.read()
                    if rest_err:
                        stderr_chunks.append(rest_err)
                        _safe_on_chunk(on_chunk, 'stderr',
                                       rest_err.decode('utf-8', errors='replace'))
                except (BlockingIOError, OSError) as _e_audit:
                    logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
                    pass
                break

            # A user steer may hand this process to a background owner. The
            # original tool round can no longer surface or resolve an
            # interactive prompt, so close stdin exactly once while retaining
            # stdout/stderr draining, timeout, and completion handling.
            if (task and task.get('_background_detached')
                    and not stdin_closed):
                try:
                    proc.stdin.close()
                except OSError as _e_audit:
                    logger.debug(
                        '[run_command] detached stdin close failed: %s',
                        _e_audit)
                stdin_closed = True
                if stdin_probe is not None:
                    stdin_probe.close()

            # Stdin detection: check /proc/pid/syscall for read(0, ...) on our pipe
            if (retcode is None and not stdin_closed
                    and stdin_pipe_ino is not None):
                reader = None
                if stdin_probe is not None:
                    try:
                        reader = stdin_probe.poll()
                    except Exception as exc:
                        logger.debug('stdin sensor unavailable: %s', type(exc).__name__)
                        stdin_probe.close()
                        stdin_probe = None

                # Non-interactive commands (rg, grep, sort, …) are reading
                # our stdin pipe as a data source.  Close stdin immediately
                # so they receive EOF and proceed (or fall back to directory
                # search).  Without this, they block forever waiting on
                # data that will never come.
                if reader is _STDIN_NON_INTERACTIVE:
                    logger.info('run_command: non-interactive command(s) '
                                'reading stdin — closing pipe to send EOF')
                    try:
                        proc.stdin.close()
                    except OSError as _e_audit:
                        logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
                        pass
                    stdin_closed = True
                    if stdin_probe is not None:
                        stdin_probe.close()
                    continue

                if reader:
                    reader_pid, reader_comm = reader
                    # Gather what we have so far as the "prompt" context
                    partial_out = (stdout_chunks.render() + stderr_chunks.render()).decode(
                        'utf-8', errors='replace')
                    # Extract last few lines as the prompt hint
                    lines = partial_out.rstrip().split('\n')
                    prompt_hint = '\n'.join(lines[-5:]) if lines else ''

                    logger.info('run_command: child PID %d (%s) is reading stdin, '
                                'prompt_hint=%.200s', reader_pid, reader_comm, prompt_hint)

                    user_input = stdin_callback(prompt_hint)

                    if user_input is None:
                        # User declined / task aborted — close stdin
                        logger.info('run_command: stdin_callback returned None, closing stdin')
                        try:
                            proc.stdin.close()
                        except OSError as _e_audit:
                            logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
                            pass
                        stdin_closed = True
                        if stdin_probe is not None:
                            stdin_probe.close()
                    else:
                        # Write user input to stdin
                        input_bytes = user_input.encode('utf-8')
                        if not input_bytes.endswith(b'\n'):
                            input_bytes += b'\n'
                        try:
                            proc.stdin.write(input_bytes)
                            proc.stdin.flush()
                            if stdin_probe is not None:
                                stdin_probe.invalidate()
                        except (BrokenPipeError, OSError) as e:
                            logger.warning('run_command: stdin write failed: %s', e)
                            stdin_closed = True
                            if stdin_probe is not None:
                                stdin_probe.close()

                        logger.info('run_command: wrote %d bytes to stdin', len(input_bytes))

    except Exception as e:
        logger.error('run_command interactive loop error: %s', e, exc_info=True)
        try:
            _kill_process_tree(proc)
        except OSError as _e_audit:
            logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
            pass
        return (f'$ {command}\n\n'
                f'Error during interactive execution: {e}\n'
                f'[exit code: -1]')
    finally:
        if stdin_probe is not None:
            stdin_probe.close()
        # Clean up
        for fd in (proc.stdin, proc.stdout, proc.stderr):
            try:
                fd.close()
            except (OSError, AttributeError) as _e_audit:
                logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
        if unregister_cancel is not None:
            unregister_cancel()
        if task is not None:
            task.pop('_subprocess_pid', None)
            task.pop('_subprocess_pgid', None)

    _owned_command_quiesced()
    stdout = stdout_chunks.render().decode('utf-8', errors='replace')
    stderr = stderr_chunks.render().decode('utf-8', errors='replace')

    if interrupted_by:
        return _format_run_output(command, stdout, stderr, -1,
                                  interrupted_by=interrupted_by)
    if aborted:
        return _format_run_output(command, stdout, stderr, -1,
                                  timed_out=False, aborted=True)
    exit_code = proc.returncode if not timed_out else -1
    logger.info('run_command done (interactive): exit=%d, stdout=%dch, stderr=%dch',
                exit_code, len(stdout), len(stderr))
    return _format_run_output(command, stdout, stderr, exit_code, timed_out=timed_out)
