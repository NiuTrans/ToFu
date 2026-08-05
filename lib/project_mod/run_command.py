"""run_command tool + its command-execution machinery.

Extracted from ``lib/project_mod/tools.py`` (2026-06-24) to slim that
dispatch facade.  This module owns the entire shell-execution subsystem:

  - command output cleanup for LLM consumption (``_clean_command_output``)
  - destructive / catastrophic-command guards + rm-trash wrapping
  - project-file snapshot/diff + change recording
  - ``tool_run_command`` and its simple + interactive runners
  - process-tree kill, grep hardening, stdin-reader detection

``tools.py`` re-exports every public symbol from here for backward compat,
so external callers (``from lib.project_mod.tools import tool_run_command``)
are unaffected.  There is no dependency back on ``tools.py`` — the dispatch
layer depends on this module, not the reverse.
"""

import os
import re as _re  # alias used by the retained _maybe_wrap_rm_with_trash
import subprocess
import time

from lib.log import get_logger
from lib.project_mod.config import (
    DANGEROUS_PATTERNS,  # noqa: F401  — re-exported for back-compat callers
    IGNORE_DIRS,
    MAX_COMMAND_OUTPUT,
    MAX_COMMAND_TIMEOUT,
    SHELL_PREFIX,
)
from lib.project_mod.modifications import _record_modification

# ── Pure command analysis relocated to command_analysis.py (2026-07-11) ──
# Re-imported here so both `lib.project_mod.run_command` and (via tools.py)
# `lib.project_mod.tools` keep exposing these symbols unchanged.
from lib.project_mod.command_analysis import (  # noqa: F401
    _ANSI_ESC_RE,
    _DANGEROUS_RE,
    _DELETE_COMMANDS,
    _DEVICE_RE,
    _FS_HEAVY_RE,
    _GIT_DESTRUCTIVE_SUBCOMMANDS,
    _GIT_READONLY_SUBCOMMANDS,
    _MIN_DELETE_DEPTH,
    _PROGRESS_RE,
    _READONLY_COMMANDS,
    _REDIRECT_PATTERN,
    _REDIRECT_TO_DEV_NULL,
    _SED_INPLACE,
    _WRITE_TARGET_COMMANDS,
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
    _line_fingerprint,
    _mask_quoted_literals,
    _split_pipeline,
    _unbounded_recursive_scan_target,
)

logger = get_logger(__name__)


def _get_cmd_env(cwd=None):
    """Return an env dict for subprocess calls spawned by run_command.

    Subprocesses inherit the server's full environment (PATH, PYTHONPATH,
    CONDA_PREFIX, etc.) so ``python`` / ``pip`` / installed CLIs resolve
    exactly the way they would in the shell that started the server.
    Tofu users who launched via the standard install land in the Tofu
    conda env (the marker re-execs server.py into it); headless-API users
    who launched from their own venv keep their own env. Either way, no
    GUI knobs, no per-workspace markers — what the user sees is what the
    agent gets.

    Adds two cosmetic tweaks for cleaner captured output:
      - PYTHONUNBUFFERED=1   so child python prints flush immediately
      - TERM=dumb (Unix)     so tools skip ANSI cursor/color escapes

    Workspace isolation (Nov 2026):
      Each task spawns ``run_command`` calls in its own working directory
      (project root or per-task workspace). When the agent runs ``pip
      install``, ``python`` etc., we MUST NOT let those leak into:
        - ``~/.local/`` (user site-packages — shared across all tasks)
        - server's conda env (the host running tofu)
        - other tasks' workspaces
      So we set:
        - ``PYTHONNOUSERSITE=1``  → pip never writes to ~/.local
        - ``PIP_USER=0``           → pip never auto-falls-back to --user
        - Strip ``LD_LIBRARY_PATH`` from sglang/conda envs (server-only)
        - Strip ``CONDA_DEFAULT_ENV`` etc. so the subprocess doesn't see
          the server's conda activation
        - If the workspace has a ``.venv/`` (created by tofu-experiment's
          harness), prepend its ``bin/`` to PATH and set ``VIRTUAL_ENV``.
    """
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    if os.name != 'nt':
        env.setdefault('TERM', 'dumb')

    # Skill credential overlay: vault-configured per-skill env vars (set in
    # Settings → Skills) ride every subprocess, so a skill's documented
    # os.environ['SOME_KEY'] lookup works without restarting the server.
    # Never raises (degrades to {}); the child env is never logged.
    try:
        from lib.skills.env import exec_env_overlay
        env.update(exec_env_overlay(project_path=cwd))
    except Exception as _env_e:
        logger.debug('[run_command] skill env overlay skipped: %s', _env_e)

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

    # ── Per-workspace venv activation ──
    # tofu-experiment creates {workspace}/.venv/ for each (instance, tool).
    # When we run a command in that workspace, prefer its venv binaries.
    if cwd:
        venv_bin = os.path.join(cwd, '.venv', 'bin')
        if os.path.isdir(venv_bin):
            env['VIRTUAL_ENV'] = os.path.join(cwd, '.venv')
            # Prepend venv/bin to PATH (rest of PATH stays intact for
            # access to system tools like git/grep/sed).
            current_path = env.get('PATH', '/usr/bin:/bin')
            env['PATH'] = f'{venv_bin}:{current_path}'
            # Also ensure subprocess sees workspace source on PYTHONPATH
            # (for C-extension repos where pip install -e . fails).
            src_dir = os.path.join(cwd, 'src')
            if os.path.isdir(src_dir):
                env['PYTHONPATH'] = f'{src_dir}:{cwd}'
            else:
                env['PYTHONPATH'] = cwd

    # ★ Portable sandbox env-jail (restricted/agent-run principals only):
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
#  ★ Filesystem snapshot helpers for run_command tracking
# ═══════════════════════════════════════════════════════

# Max depth to scan for file changes after run_command (avoid scanning huge trees)
_SNAPSHOT_MAX_FILES = 5000
_SNAPSHOT_MAX_DEPTH = 6

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


def _maybe_wrap_rm_with_trash(command, cwd):
    """Prepend an ``rm`` shell-function shim that trashes instead of deletes.

    Returns the (possibly rewritten) command. No-op when disabled, when the
    command has no ``rm`` segment, or when the command already references the
    trash dir (avoid double-wrapping). The shim moves each target under
    ``<cwd>/.tofu_trash/<timestamp>/`` preserving relative layout; ``-f`` on
    a missing file still succeeds (exit 0) to match ``rm`` semantics. On each
    delete it also lazily prunes trash entries older than
    ``TOFU_TRASH_TTL_DAYS`` (default 7) so the bin can't grow unbounded.

    ★ The trash is the WORKSPACE's undo bin, not the machine's. An absolute
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
    trash_root = os.path.join(cwd or '.', _TRASH_DIRNAME)
    _ws = os.path.normpath(cwd) if cwd and os.path.isabs(cwd) else None
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


def _snapshot_project_files(base_path):
    """Take a lightweight snapshot of the project file tree (path → mtime).

    Captures only files that pass the ignore filter and are within a
    reasonable depth/count.  Used before/after run_command to detect
    what files were created, deleted, or modified.
    """
    snapshot = {}  # rel_path → mtime (float)
    count = 0
    base_len = len(base_path.rstrip('/')) + 1
    try:
        for dirpath, dirnames, filenames in os.walk(base_path, followlinks=False):
            # Depth check
            rel_dir = dirpath[base_len:] if len(dirpath) > base_len else ''
            depth = rel_dir.count(os.sep) + 1 if rel_dir else 0
            if depth > _SNAPSHOT_MAX_DEPTH:
                dirnames.clear()
                continue
            # Prune ignored dirs in-place — exclude per-project ignore + DB engine dirs
            # Note: dot-dirs like .tofu/.project_sessions are still walked
            # so that destructive commands targeting them are tracked.
            dirnames[:] = [
                d for d in dirnames
                if d not in IGNORE_DIRS
                and d not in _SNAPSHOT_EXTRA_IGNORE
            ]
            for fname in filenames:
                if count >= _SNAPSHOT_MAX_FILES:
                    break
                fp = os.path.join(dirpath, fname)
                rel = fp[base_len:]
                try:
                    st = os.stat(fp)
                    snapshot[rel] = st.st_mtime
                except OSError as e:
                    logger.debug('[Snapshot] stat failed for %s: %s', rel, e)
                count += 1
            if count >= _SNAPSHOT_MAX_FILES:
                break
    except OSError as e:
        logger.debug('[Snapshot] os.walk error for %s: %s', base_path, e)
    return snapshot


def _diff_snapshots(base_path, before, after):
    """Compare two snapshots to find created, deleted, and modified files.

    Returns list of dicts: [{rel_path, change_type}] where change_type is
    'created', 'deleted', or 'modified'.
    """
    changes = []
    all_paths = set(before.keys()) | set(after.keys())
    for rel in sorted(all_paths):
        in_before = rel in before
        in_after = rel in after
        if in_after and not in_before:
            changes.append({'rel_path': rel, 'change_type': 'created'})
        elif in_before and not in_after:
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
    for ch in changes:
        rel = ch['rel_path']
        ct = ch['change_type']
        os.path.join(base_path, rel)

        if ct == 'deleted':
            # File was deleted — original content was saved in ch['original_content']
            original = ch.get('original_content')
            _record_modification(
                base_path, 'run_command', rel,
                original_content=original,
                conv_id=conv_id, task_id=task_id,
            )
            recorded.append({'path': rel, 'action': 'deleted'})
        elif ct == 'created':
            # File was created — mark as not-existed for undo-by-delete
            _record_modification(
                base_path, 'run_command', rel,
                original_content=None,  # signals "didn't exist before"
                conv_id=conv_id, task_id=task_id,
            )
            recorded.append({'path': rel, 'action': 'created'})
        elif ct == 'modified':
            # File content changed — original content saved in ch['original_content']
            original = ch.get('original_content')
            _record_modification(
                base_path, 'run_command', rel,
                original_content=original,
                conv_id=conv_id, task_id=task_id,
            )
            recorded.append({'path': rel, 'action': 'modified'})
    return recorded


# ═══════════════════════════════════════════════════════
#  ★ Tool Implementation: run_command
# ═══════════════════════════════════════════════════════

# Stdin detection uses /proc/<pid>/syscall for definitive read(0,...) detection.
# Checked each iteration of the select() loop (~every 0.2s). No timing heuristics.


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

    result_text = f'$ {command}\n'
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


def tool_run_command(base, command, timeout=None, stdin_callback=None, task=None,
                     on_chunk=None, cwd_sink=None, on_spawn=None):
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
    """
    if not command or not command.strip():
        return 'Error: Empty command.'

    if not base:
        base = os.path.expanduser('~')

    # ★ Resolve timeout — NO DEFAULT CEILING.
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
    #   Pinned by tests/test_no_backend_timeouts.py.
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

    # ★ Catastrophic top-level deletion guard — refuse `rm /mnt`, `rm -rf /`,
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

    # ★ Unbounded recursive-scan guard (pt_8524e0ec B2). A recursive scan of a
    #   workspace ANCESTOR or a FUSE mount root can crawl for hours on network
    #   filesystems — measured 2026-07-31: `grep -rn "mcp>=1.0.0" ../` ran
    #   2.5h with zero output and wedged task 96c56840 (no timeout existed to
    #   end it). Refused ONLY when the call itself is unbounded (no explicit
    #   `timeout` arg) and the scan segment is not self-bounding (coreutils
    #   `timeout` wrapper). In-workspace scans, sibling dirs and bounded
    #   scans are all legitimate and stay open. TOFU_RUN_SCAN_GUARD=0 opts out.
    if timeout is None and os.environ.get('TOFU_RUN_SCAN_GUARD', '1') != '0':
        _scan = _unbounded_recursive_scan_target(command, base)
        if _scan is not None:
            # WARNING, not ERROR — a policy nudge, not an application failure.
            logger.warning('[run_command] BLOCKED unbounded recursive scan '
                           'target=%r (cwd=%s): %.200s', _scan, base, command)
            return (f"Error: Command blocked for safety: unbounded recursive "
                    f"scan of '{_scan}'. Recursive scans of a workspace "
                    f"ancestor or a FUSE mount root can crawl for hours on "
                    f"network filesystems and hang the whole task (measured: "
                    f"2.5h on `grep -rn … ../`). Narrow it: (1) scan a "
                    f"specific subdirectory instead; (2) pass an explicit "
                    f"`timeout` to run_command; or (3) wrap the scan with "
                    f"coreutils `timeout <secs>`.")

    # ★ Filesystem-grep redirect guard (2026-08-04). The tool description has
    #   advised grep_search-over-grep for months, yet grep-via-run_command
    #   kept recurring — measured: a two-grep pipeline sat RUNNING 17m04s in
    #   a FUSE bad window with zero output and no timeout to end it. Advice
    #   is not enforcement: refuse grep-family segments that read the
    #   FILESYSTEM (file/dir operands or -r) and translate the call to
    #   grep_search. Stream filters (`pytest 2>&1 | grep PASS`,
    #   `ps aux | grep python`), rg, git grep and timeout-wrapped shapes all
    #   stay legal. TOFU_RUN_GREP_GUARD=0 opts out.
    if os.environ.get('TOFU_RUN_GREP_GUARD', '1') != '0':
        _gseg = _grep_filesystem_segment(command)
        if _gseg is not None:
            # WARNING, not ERROR (owner 2026-08-05): a policy nudge for the
            # agent to re-route, not an application failure — 127 ERRORs in
            # 2.5 days was pure teaching-loop noise burying real errors.
            logger.warning('[run_command] BLOCKED filesystem grep (cwd=%s): %.200s',
                           base, command)
            return (
                'Error: Command intercepted: this `grep` reads the filesystem, '
                'which belongs to the dedicated `grep_search` tool, not '
                f'run_command. Refused segment: `{_gseg[:150]}`\n'
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

    # ★ Cross-DC timeout adjustment — multiply timeout for remote DolphinFS clusters
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

    # ★ Non-silent grep hardening: when the user runs a recursive `grep -r ...`
    # in a non-pipeline form, inject `-I` (skip binary) and `--exclude-dir=` for
    # each entry in IGNORE_DIRS so cross-DC FUSE mounts don't time out.  Real
    # grep still does the work — output format and regex flavor are unchanged.
    # The injected flags appear in the `$ ...` echo so the change is visible.
    # Disable with TOFU_RUN_HARDEN_GREP=0.
    hardened = _maybe_harden_grep_command(command) if not stdin_callback else command
    if hardened != command:
        logger.info('[run_command] Hardened grep: %s → %s',
                    command[:120], hardened[:160])
        _safe_on_chunk(on_chunk, 'stderr',
                       '[run_command] auto-added grep flags for FUSE/binary safety\n')
        command = hardened

    # ★ rm → trash rewrite: ordinary deletes become recoverable (moved to a
    #   per-workspace .tofu_trash/ instead of unlinked). Applied to the
    #   EXECUTED command only — the displayed/logged `command` stays clean so
    #   the model sees its own `rm ...` echoed back, not the shim. Catastrophic
    #   deletes are already rejected above and never reach here.
    exec_command = _maybe_wrap_rm_with_trash(command, base)

    # ★ Sticky-cwd capture (layer 2): wrap the command so its FINAL cwd is
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

    # ★ Portable sandbox (restricted/agent-run principals only). On a host
    #   that allows it this wraps the command in a real isolation backend
    #   (bwrap/podman); on a locked host it is a no-op here and containment
    #   comes from the HOME/TMPDIR jail + rm shim applied in _get_cmd_env.
    #   Local desktop/CLI callers are NOT restricted → completely unaffected.
    try:
        from lib.project_mod.abs_path_guard import is_restricted
        if is_restricted() and base:
            from lib.project_mod import portable_sandbox
            full_command = portable_sandbox.wrap_command(full_command, base)
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

    # ── Non-interactive fast path (no stdin_callback) ──
    if not stdin_callback:
        try:
            return _run_command_simple(command, full_command, timeout, base, task=task,
                                       on_chunk=on_chunk, on_spawn=on_spawn)
        finally:
            _flush_cwd_capture()

    # ── Interactive path: Popen with stdin pipe + stdin detection ──
    try:
        return _run_command_interactive(command, full_command, timeout, base, stdin_callback,
                                        on_chunk=on_chunk, task=task, on_spawn=on_spawn)
    finally:
        _flush_cwd_capture()


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
                f'Retry with a narrower scope or an explicit timeout.')
    return 'user'


def _safe_on_spawn(on_spawn, timeout):
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


def _run_command_simple(command, full_command, timeout, base, task=None, on_chunk=None,
                        on_spawn=None):
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
    try:
        proc = subprocess.Popen(
            get_shell_args(full_command),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=False,  # binary mode for non-blocking I/O
            cwd=base,
            env=_get_cmd_env(base),
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

    # ★ Spawn clock — the subprocess now EXISTS and `timeout` already holds the
    #   effective budget, so this is the earliest and only point at which a
    #   truthful deadline can be published. Fired before the read loop so the
    #   countdown appears immediately rather than at the first heartbeat tick.
    _safe_on_spawn(on_spawn, timeout)

    # Store PID on task so abort handler can kill it directly
    if task is not None:
        task['_subprocess_pid'] = proc.pid
        task['_subprocess_pgid'] = None
        try:
            task['_subprocess_pgid'] = os.getpgid(proc.pid)
        except OSError as _e_audit:
            logger.debug('[tools] _run_command_simple caught %s: %s', type(_e_audit).__name__, _e_audit)
            pass

    # Set stdout/stderr to non-blocking.  On platforms where this fails
    # (Windows), safe_select_pipes degrades to short-timeout polling.
    nonblocking_ok = all(
        set_pipe_nonblocking(fd) for fd in (proc.stdout, proc.stderr)
    )
    if not nonblocking_ok:
        logger.debug('run_command: non-blocking pipe setup failed — using polling I/O')

    stdout_chunks = []   # list[bytes]
    stderr_chunks = []   # list[bytes]
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
        while True:
            # ── timeout ──
            elapsed = time.monotonic() - start_time
            if timeout is not None and elapsed >= timeout:
                logger.info('run_command timed out after %ss — killing PID %d',
                            timeout, proc.pid)
                _kill_process_tree(proc)
                _drain_after_kill()
                timed_out = True
                break

            # ── abort ──
            if task and task.get('aborted'):
                logger.info('[run_command] Task aborted — killing subprocess PID %d: %s',
                            proc.pid, command[:80])
                _kill_process_tree(proc)
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
                _kill_process_tree(proc)
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
            _kill_process_tree(proc)
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

    # Clean up task ref
    if task is not None:
        task.pop('_subprocess_pid', None)
        task.pop('_subprocess_pgid', None)

    stdout = b''.join(stdout_chunks).decode('utf-8', errors='replace')
    stderr = b''.join(stderr_chunks).decode('utf-8', errors='replace')

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


def _kill_process_tree(proc):
    """Kill a subprocess and all its children via process group, with fallback."""
    import signal
    pid = proc.pid
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=2)
        logger.info('[run_command] Killed process group pgid=%d (pid=%d)', pgid, pid)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug('[run_command] Process group kill failed: %s — trying direct kill', e)
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception as e2:
            logger.warning('[run_command] Direct kill also failed for pid=%d: %s', pid, e2)


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


def _is_any_child_reading_stdin(parent_pid, stdin_pipe_ino):
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
        try:
            # Does this process's fd 0 point to our stdin pipe?
            fd0_ino = os.stat(f'/proc/{pid}/fd/0').st_ino
            if fd0_ino != stdin_pipe_ino:
                continue

            # Read the current syscall
            with open(f'/proc/{pid}/syscall') as f:
                sc = f.read().strip()
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
                              on_chunk=None, task=None, on_spawn=None):
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
    try:
        proc = subprocess.Popen(
            get_shell_args(full_command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=base,
            env=_get_cmd_env(base),
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

    # Set stdout/stderr to non-blocking (no-op on Windows, uses threading there).
    # If this fails on an unusual platform, interactive I/O will still work via
    # polling with small timeouts — it just won't be as responsive.
    nonblocking_ok = all(
        set_pipe_nonblocking(fd) for fd in (proc.stdout, proc.stderr)
    )
    if not nonblocking_ok:
        logger.warning('run_command: non-blocking pipe setup failed — falling back to polling I/O')

    # ★ Spawn clock — same contract as _run_command_simple: the subprocess
    #   exists and `timeout` already carries the effective budget, so this is
    #   the earliest truthful deadline. Both runners must publish it, else an
    #   interactive command (the ones that block LONGEST) would be the only
    #   kind with no countdown.
    _safe_on_spawn(on_spawn, timeout)

    # Store PID/PGID on task so the abort handler can kill it directly
    # (mirrors _run_command_simple) — the interactive path was previously
    # missing this, leaving Stop unable to kill a long interactive command.
    if task is not None:
        task['_subprocess_pid'] = proc.pid
        task['_subprocess_pgid'] = None
        try:
            task['_subprocess_pgid'] = os.getpgid(proc.pid)
        except OSError as _e_audit:
            logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)

    # Get the inode of our stdin pipe so we can match it in /proc
    try:
        stdin_pipe_ino = os.fstat(proc.stdin.fileno()).st_ino
    except OSError as _e_audit:
        logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
        stdin_pipe_ino = None

    stdout_chunks = []
    stderr_chunks = []
    start_time = time.monotonic()
    stdin_closed = False
    timed_out = False
    aborted = False
    interrupted_by = ''

    try:
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
                proc.kill()
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

            # ★ Stdin detection: check /proc/pid/syscall for read(0, ...) on our pipe
            if (retcode is None and not stdin_closed
                    and stdin_pipe_ino is not None):
                reader = _is_any_child_reading_stdin(proc.pid, stdin_pipe_ino)

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
                    continue

                if reader:
                    reader_pid, reader_comm = reader
                    # Gather what we have so far as the "prompt" context
                    partial_out = b''.join(stdout_chunks + stderr_chunks).decode('utf-8', errors='replace')
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
                    else:
                        # Write user input to stdin
                        input_bytes = user_input.encode('utf-8')
                        if not input_bytes.endswith(b'\n'):
                            input_bytes += b'\n'
                        try:
                            proc.stdin.write(input_bytes)
                            proc.stdin.flush()
                        except (BrokenPipeError, OSError) as e:
                            logger.warning('run_command: stdin write failed: %s', e)
                            stdin_closed = True

                        logger.info('run_command: wrote %d bytes to stdin', len(input_bytes))

    except Exception as e:
        logger.error('run_command interactive loop error: %s', e, exc_info=True)
        try:
            proc.kill()
        except OSError as _e_audit:
            logger.debug('[tools] _run_command_interactive caught %s: %s', type(_e_audit).__name__, _e_audit)
            pass
        return (f'$ {command}\n\n'
                f'Error during interactive execution: {e}\n'
                f'[exit code: -1]')
    finally:
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
            proc.kill()
            proc.wait()
        if task is not None:
            task.pop('_subprocess_pid', None)
            task.pop('_subprocess_pgid', None)

    stdout = b''.join(stdout_chunks).decode('utf-8', errors='replace')
    stderr = b''.join(stderr_chunks).decode('utf-8', errors='replace')

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

