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
import re
import subprocess
import time
from collections import Counter

from lib.log import get_logger
from lib.project_mod.config import (
    DANGEROUS_PATTERNS,
    IGNORE_DIRS,
    MAX_COMMAND_OUTPUT,
    MAX_COMMAND_TIMEOUT,
    SHELL_PREFIX,
)
from lib.project_mod.modifications import _record_modification

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  ★ Command output cleanup for LLM consumption
# ═══════════════════════════════════════════════════════

# ANSI escape codes: SGR (colors), cursor movement, OSC (window titles)
_ANSI_ESC_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-B012]')

# tqdm-style progress bar: "Label: NN%|bar_chars| X/Y [timing, rate]"
# The content inside [...] varies widely (ETA, rate, ?it/s) — match any non-]
_PROGRESS_RE = re.compile(
    r'^(.*?)\s*\d+%\|[^|]*\|\s*\d+/\d+\s*\[[^\]]*\](.*)$'
)

# ★ Pre-compiled regex for run_command
_FS_HEAVY_RE = re.compile(r'\b(du|find|locate|tree|wc\s+-|cloc|sloccount|ncdu|fd)\b')
_DANGEROUS_RE = re.compile('|'.join(f'(?:{p})' for p in DANGEROUS_PATTERNS))


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
    except Exception:
        pass

    return env


def _extract_progress_label(line):
    """Extract the label prefix from a tqdm-style progress bar line.

    Returns the stripped label string if this is a *pure* progress bar line
    (no significant content after the bar), or None otherwise.
    Lines with substantial trailing content (e.g. "[Worker 3] Starting …")
    are NOT treated as progress bars — they go through Phase 4 dedup instead.
    """
    m = _PROGRESS_RE.match(line)
    if not m:
        return None
    label = m.group(1).strip()
    trailing = m.group(2).strip()
    # If there's significant content after the progress bar, this is an
    # "announcement" line (e.g. worker startup) — don't treat as progress bar
    if len(trailing) > 20:
        return None
    return label


# ── Device / worker detection for multi-GPU annotation ──────────
_DEVICE_RE = re.compile(
    r'(?:cuda|gpu|device|rank|worker)[\s:_]*(\d+)', re.IGNORECASE
)


def _extract_progress_pct(line):
    """Extract percentage from a tqdm-style progress bar line.

    Returns an integer 0-100, or None if not a progress bar.
    """
    m = re.search(r'(\d+)%\|', line)
    return int(m.group(1)) if m else None


def _extract_device_ids(lines):
    """Extract unique device/worker IDs from a group of lines.

    Looks for patterns like cuda:0, GPU 3, Worker 5, rank 2.
    Returns sorted list of unique integer IDs, or empty list.
    """
    ids = set()
    for ln in lines:
        for m in _DEVICE_RE.finditer(ln):
            ids.add(int(m.group(1)))
    return sorted(ids)


def _format_cuda_device_range(ids):
    """Format CUDA device IDs as a compact range string with ``cuda:`` prefix.

    Examples:
        [0,1,2,3,4,5,6,7] → 'cuda:0-7'
        [0,2,5] → 'cuda:0,2,5'
        [3] → 'cuda:3'

    See ``lib/log_clean.py::_format_device_range`` for the un-prefixed
    variant used by the log-clean banner.
    """
    if not ids:
        return ''
    if len(ids) == 1:
        return f'cuda:{ids[0]}'
    # Contiguous range?
    if ids[-1] - ids[0] + 1 == len(ids):
        return f'cuda:{ids[0]}-{ids[-1]}'
    return 'cuda:' + ','.join(str(i) for i in ids)


def _line_fingerprint(line):
    """Structural fingerprint: replace digit sequences with '#'.

    Lines that differ only in numeric values (device IDs, counts, timings)
    produce the same fingerprint and can be collapsed.
    Returns None for short/empty lines to prevent false grouping.
    """
    s = line.strip()
    if len(s) < 20:
        return None
    return re.sub(r'\d+', '#', s)


def _clean_command_output(output):
    """Clean command output for efficient LLM consumption.

    Phases:
      1. Strip ANSI escape codes (colors, cursor control)
      2. Resolve ``\\r`` carriage-return overwrites (keep final visible text)
      3. Compress tqdm-style progress bar groups → first + ~50% + last
      4. Collapse multi-device / repeated lines → first + count

    This drastically reduces token waste from training logs, data pipelines,
    and multi-GPU workloads without losing meaningful information.
    """
    if not output or len(output) < 200:
        return output

    original_len = len(output)

    # ── Phase 1: Strip ANSI escape codes ──────────────────────────────
    output = _ANSI_ESC_RE.sub('', output)

    # ── Phase 2: Resolve \\r carriage-return overwrites ───────────────
    # tqdm and similar tools write "\\r" to rewind the cursor and overwrite
    # the line.  In captured (non-TTY) output, all intermediate states are
    # visible — keep only the final non-empty segment per line.
    raw_lines = output.split('\n')
    lines = []
    for raw in raw_lines:
        if '\r' in raw:
            parts = raw.split('\r')
            visible = ''
            for p in parts:
                if p.strip():
                    visible = p
            lines.append(visible if visible else '')
        else:
            lines.append(raw)

    # ── Phase 3 & 4: Group and compress consecutive similar lines ─────
    # Helper: collect a run of lines matching a predicate, skipping blanks
    def _collect_group(start, match_fn):
        """Collect consecutive lines matching match_fn, skipping blank separators.

        Returns (group_of_content_lines, next_index_after_group).
        """
        grp = [lines[start]]
        j = start + 1
        while j < len(lines):
            if not lines[j].strip():
                # Blank line — peek ahead to see if next content line
                # still belongs to the group
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and match_fn(lines[k]):
                    # The blank(s) separate members of the same group —
                    # skip them and continue collecting
                    j = k
                    grp.append(lines[j])
                    j += 1
                else:
                    break
            elif match_fn(lines[j]):
                grp.append(lines[j])
                j += 1
            else:
                break
        return grp, j

    result = []
    i = 0
    total_compressed = 0

    while i < len(lines):
        line = lines[i]

        # Skip blank lines that separate groups (will be re-added as needed)
        if not line.strip():
            result.append(line)
            i += 1
            continue

        # ── Phase 3: Progress bar compression ─────────────────────────
        pb_label = _extract_progress_label(line)
        if pb_label is not None:
            group, j = _collect_group(
                i, lambda ln: _extract_progress_label(ln) == pb_label)

            n = len(group)
            if n <= 3:
                result.extend(group)
            else:
                # ── Percentage-aware sampling + device detection ──
                pcts = [(_extract_progress_pct(g), g) for g in group]
                valid = [(p, g) for p, g in pcts if p is not None]

                # Detect device parallelism: max lines sharing same %
                device_count = 1
                if valid:
                    pct_freq = Counter(p for p, _ in valid)
                    device_count = max(pct_freq.values())
                device_note = (f', ×{device_count} devices'
                               if device_count > 1 else '')

                if valid:
                    # Pick lines by percentage: start / mid / end
                    by_pct = sorted(valid, key=lambda x: x[0])
                    first_line = by_pct[0][1]
                    last_line  = by_pct[-1][1]
                    pct_mid = (by_pct[0][0] + by_pct[-1][0]) // 2
                    mid_entry = min(valid,
                                    key=lambda x: abs(x[0] - pct_mid))
                    mid_line = mid_entry[1]

                    result.append(first_line)
                    has_mid = (mid_line != first_line
                               and mid_line != last_line)
                    skipped = n - (3 if has_mid else 2)
                    result.append(
                        f'  … ({skipped} more progress updates'
                        f'{device_note}) …')
                    if has_mid:
                        result.append(mid_line)
                    result.append(last_line)
                    total_compressed += skipped
                else:
                    # Fallback: positional sampling
                    result.append(group[0])
                    result.append(
                        f'  … ({n - 2} more progress updates'
                        f'{device_note}) …')
                    result.append(group[-1])
                    total_compressed += n - 2
            i = j
            continue

        # ── Phase 4: Multi-device / repeated line collapse ────────────
        fp = _line_fingerprint(line)
        if fp is not None:
            group, j = _collect_group(
                i, lambda ln: _line_fingerprint(ln) == fp)

            n = len(group)
            if n <= 2:
                result.extend(group)
            else:
                result.append(group[0])
                device_ids = _extract_device_ids(group)
                if len(device_ids) > 1:
                    dev_range = _format_cuda_device_range(device_ids)
                    result.append(
                        f'  … (×{len(device_ids)} devices on '
                        f'{dev_range}) …')
                else:
                    result.append(
                        f'  … (and {n - 1} more similar lines) …')
                total_compressed += n - 1
            i = j
            continue

        result.append(line)
        i += 1

    cleaned = '\n'.join(result)
    if total_compressed > 5:
        logger.debug('_clean_command_output: compressed %d repetitive lines '
                     '(%d → %d chars)', total_compressed, original_len,
                     len(cleaned))
    return cleaned


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

# ── Command destructiveness analysis ──────────────────────────────────
# Provably read-only shell utilities that NEVER modify the filesystem.
# Only commands whose behaviour is fully determined by the binary name
# belong here — NOT interpreters/runtimes (python, node, …) whose
# behaviour depends on the script/code they execute.
#
# Design principle: the snapshot (with runtime dirs excluded) is cheap
# (~5 ms for a few hundred source files), so we only skip it for
# commands we can PROVE are harmless.  Everything else → snapshot.
import re as _re

_READONLY_COMMANDS = frozenset({
    # ── Search / match ──
    'grep', 'egrep', 'fgrep', 'rg', 'ag', 'ack',
    # ── View / page ──
    'cat', 'head', 'tail', 'less', 'more', 'bat',
    # ── List / stat ──
    'ls', 'dir', 'tree', 'stat', 'file', 'du', 'df',
    # ── Find / locate ──
    'find', 'fd', 'fdfind', 'locate', 'which', 'whereis', 'type',
    # ── Text processing (pure filters — no in-place flag) ──
    # Note: sed is here because plain sed is a stdout filter; sed -i is
    # caught separately by _SED_INPLACE before the whitelist check.
    'wc', 'sort', 'uniq', 'cut', 'tr', 'sed', 'awk', 'column',
    # ── Compare / hash ──
    'diff', 'cmp', 'comm', 'md5sum', 'sha256sum', 'sha1sum',
    # ── Shell builtins / info ──
    'echo', 'printf', 'true', 'false', 'test', '[',
    'env', 'printenv', 'whoami', 'id', 'hostname',
    'date', 'cal', 'uptime', 'uname',
    'pwd', 'basename', 'dirname', 'realpath', 'readlink',
    # ── Process / resource inspection ──
    'ps', 'top', 'htop', 'free', 'vmstat', 'lsof', 'pgrep',
    # ── Network (query-only forms) ──
    'ping', 'dig', 'nslookup', 'host', 'traceroute',
    # ── Data format ──
    'jq', 'yq',
    # ── Git read-only sub-commands are handled specially below ──
})

# Git sub-commands that are purely read-only.
_GIT_READONLY_SUBCOMMANDS = frozenset({
    'status', 'log', 'diff', 'show', 'branch', 'tag',
    'remote', 'describe', 'rev-parse', 'rev-list',
    'ls-files', 'ls-tree', 'ls-remote',
    'blame', 'shortlog', 'reflog',
    'config',   # reads config; --global writes, but never touches project files
    'stash',    # 'git stash list/show' is read-only; 'stash pop/drop' handled below
})

_GIT_DESTRUCTIVE_SUBCOMMANDS = frozenset({
    'checkout', 'switch', 'reset', 'clean', 'rm', 'mv',
    'stash', 'rebase', 'merge', 'cherry-pick', 'revert',
    'apply', 'am', 'pull', 'fetch', 'push', 'clone', 'init',
    'add', 'commit', 'restore',
})

# Shell output redirection operators that write to files.
# Excludes harmless >/dev/null and 2>/dev/null (stderr suppression).
_REDIRECT_TO_DEV_NULL = _re.compile(r'[12]?>+\s*/dev/null\b')
_REDIRECT_PATTERN = _re.compile(r'[12]?>>?(?!&)')

# sed with in-place flag
_SED_INPLACE = _re.compile(r'\bsed\b.*\s-i')


def _split_pipeline(cmd):
    """Split a shell command into pipeline/chain segments, respecting quotes.

    Splits on |, ;, &&, || but NOT inside single or double quotes.
    This prevents splitting patterns like ``grep -i "foo|bar"`` on the pipe.
    """
    segments = []
    current = []
    in_single = False
    in_double = False
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        # Track quote state
        if c == "'" and not in_double:
            in_single = not in_single
            current.append(c)
            i += 1
        elif c == '"' and not in_single:
            in_double = not in_double
            current.append(c)
            i += 1
        elif c == '\\' and i + 1 < n and (in_double or not in_single):
            # Escaped character — consume both
            current.append(c)
            current.append(cmd[i + 1])
            i += 2
        elif not in_single and not in_double:
            # Check for ;, &&, ||, | (pipeline/chain separators)
            if c == ';':
                segments.append(''.join(current).strip())
                current = []
                i += 1
            elif c == '&' and i + 1 < n and cmd[i + 1] == '&':
                segments.append(''.join(current).strip())
                current = []
                i += 2
            elif c == '|' and i + 1 < n and cmd[i + 1] == '|':
                segments.append(''.join(current).strip())
                current = []
                i += 2
            elif c == '|':
                segments.append(''.join(current).strip())
                current = []
                i += 1
            else:
                current.append(c)
                i += 1
        else:
            current.append(c)
            i += 1
    tail = ''.join(current).strip()
    if tail:
        segments.append(tail)
    return segments

# ── Commands whose file arguments are WRITE targets ──────────────────
# Maps command name → set of arg-index semantics for extracting write targets.
# 'all_args'       — every non-flag argument is a write target (rm, touch, chmod)
# 'last_arg'       — last non-flag argument is the write target (cp, install)
# 'all_args_files' — like all_args but only existing files (mkdir targets are dirs)
_WRITE_TARGET_COMMANDS = {
    # Delete
    'rm':     'all_args',
    'rmdir':  'all_args',
    'unlink': 'all_args',
    # Create / modify metadata
    'touch':  'all_args',
    'chmod':  'all_args',
    'chown':  'all_args',
    'chgrp':  'all_args',
    # Copy / move — destination is the write target; source is read-only
    'cp':     'last_arg',
    'mv':     'last_arg',
    'install':'last_arg',
    # In-place editors
    'patch':  'all_args',
    # Archive extraction
    'tar':    'opaque',   # too complex to parse; fall back to full snapshot
    'unzip':  'opaque',
    'gunzip': 'all_args',
    # In-place edit (sed -i handled specially in _extract_write_targets)
    'sed':    'sed_special',
}


def _extract_write_targets(command, cwd=''):
    """Parse a shell command and return the set of file paths it WRITES to.

    Returns:
        set[str] | None
        - set of relative paths that the command writes to (may be empty
          if the command is read-only)
        - None if the command is opaque (interpreter, build tool, etc.)
          and we cannot determine specific targets — meaning ANY file
          in the project could be modified.

    This is used to filter snapshot diffs: only files in the returned set
    (or all files if None) are reported as changed.
    """
    if not command or not command.strip():
        return set()

    cmd = command.strip()
    targets = set()
    has_opaque = False

    # ── Redirect targets ──
    # Extract the file path from redirections like: > file.txt, >> log.txt, 2> err.log
    # First remove /dev/null redirects, then find remaining redirect targets.
    cmd_no_devnull = _REDIRECT_TO_DEV_NULL.sub('', cmd)
    for m in _re.finditer(r'[12]?>>?\s*(\S+)', cmd_no_devnull):
        target = m.group(1)
        if target and not target.startswith('&'):
            targets.add(target)

    # ── sed -i targets ──
    if _SED_INPLACE.search(cmd):
        # sed -i[suffix] 's/.../.../g' file1 file2 ...
        # File arguments come after the sed expression (last args that aren't flags)
        for seg in _split_pipeline(cmd):
            seg = seg.strip()
            if not seg:
                continue
            parts = seg.split()
            base_cmd = parts[0].split('/')[-1] if parts else ''
            if base_cmd != 'sed':
                continue
            # Skip flags and the expression; remaining non-flag args are files
            skip_next = False
            past_expr = False
            for arg in parts[1:]:
                if skip_next:
                    skip_next = False
                    continue
                if arg.startswith('-') and not past_expr:
                    # -i, -e, -f may take a following argument
                    if arg in ('-e', '-f'):
                        skip_next = True
                    continue
                if not past_expr:
                    past_expr = True  # first non-flag is the expression
                    continue
                # Everything after the expression is a file target
                targets.add(arg)

    # ── Per-segment analysis ──
    segments = _split_pipeline(cmd)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # Strip env var assignments
        while _re.match(r'^\w+=\S*\s', seg):
            seg = _re.sub(r'^\w+=\S*\s+', '', seg, count=1)
        # Strip any redirect suffixes from this segment for command parsing
        seg_clean = _re.sub(r'[12]?>>?\s*\S+', '', seg).strip()
        parts = seg_clean.split()
        if not parts:
            continue
        base_cmd = parts[0].split('/')[-1]

        # Read-only commands → no targets from this segment
        if base_cmd in _READONLY_COMMANDS:
            continue
        if base_cmd == 'git':
            sub = parts[1] if len(parts) > 1 else ''
            if sub in _GIT_READONLY_SUBCOMMANDS:
                continue
            # git checkout/reset/etc affect the whole worktree → opaque
            has_opaque = True
            continue

        # Known write commands → extract specific targets
        write_mode = _WRITE_TARGET_COMMANDS.get(base_cmd)
        if write_mode == 'all_args':
            for arg in parts[1:]:
                if not arg.startswith('-'):
                    targets.add(arg)
            continue
        elif write_mode == 'last_arg':
            # Last non-flag argument is the destination
            non_flag = [a for a in parts[1:] if not a.startswith('-')]
            if non_flag:
                targets.add(non_flag[-1])
                # For mv, source files are also "written" (deleted)
                if base_cmd == 'mv':
                    for a in non_flag[:-1]:
                        targets.add(a)
            continue
        elif write_mode == 'sed_special':
            # sed without -i is a pure filter (read-only);
            # sed -i targets are already extracted above.
            if _SED_INPLACE.search(seg):
                continue  # targets already collected
            else:
                continue  # plain sed is read-only
        elif write_mode == 'opaque':
            has_opaque = True
            continue

        # Unknown/opaque command (interpreters, build tools, etc.)
        if base_cmd not in _READONLY_COMMANDS:
            has_opaque = True

    # If any segment is opaque, we can't guarantee specific targets
    if has_opaque:
        return None

    return targets


def _filter_changes_by_targets(changes, write_targets, cwd):
    """Filter snapshot-diff changes to only include plausible write targets.

    Args:
        changes: list of {rel_path, change_type} from _diff_snapshots
        write_targets: set of paths from _extract_write_targets, or None
            (None = opaque command, keep all changes)

    Returns:
        Filtered list of changes.
    """
    if write_targets is None:
        # Opaque command — keep all changes (can't filter)
        return changes

    if not write_targets:
        # Command is purely read-only but was snapshot'd anyway (edge case)
        return []

    # Normalize targets: resolve to relative paths from project root,
    # expand directories (a target of 'src/' should match 'src/foo.py')
    normalized = set()
    dir_prefixes = []
    for t in write_targets:
        # Strip quotes that might be in the command
        t = t.strip('"\'')
        # Resolve relative to cwd within the project
        if os.path.isabs(t):
            normalized.add(t)
        else:
            normalized.add(os.path.normpath(t))
        # If target looks like a directory (ends with / or is a known dir),
        # also match children
        if t.endswith('/'):
            dir_prefixes.append(os.path.normpath(t) + '/')
        # Also try it as a dir prefix (for 'rm -rf dir' where dir has no /)
        dir_prefixes.append(os.path.normpath(t) + '/')

    filtered = []
    for ch in changes:
        rel = ch['rel_path']
        norm_rel = os.path.normpath(rel)
        # Direct match
        if norm_rel in normalized:
            filtered.append(ch)
            continue
        # Directory prefix match (target is a parent dir)
        if any(norm_rel.startswith(dp) for dp in dir_prefixes):
            filtered.append(ch)
            continue
        # Glob match for patterns like *.pyc
        if any('*' in t and __import__('fnmatch').fnmatch(norm_rel, t)
               for t in write_targets):
            filtered.append(ch)
            continue
    return filtered


def _is_destructive_command(command):
    """Analyze whether a shell command could modify the filesystem.

    Returns True if the command is potentially destructive and file
    change tracking (snapshot/diff) should be performed.

    Design: we maintain a small, curated whitelist of commands that are
    PROVABLY read-only.  Everything not on the list — including all
    interpreters (python, node, ruby, …), build tools (make, cargo, …),
    package managers (npm, pip, …), and unknown binaries — is assumed
    destructive.  This is intentionally conservative: a false-positive
    (unnecessary snapshot) costs ~5 ms; a false-negative (missed file
    change) breaks undo.
    """
    if not command or not command.strip():
        return False

    cmd = command.strip()

    # Output redirection → always destructive (writes to file).
    # First strip harmless redirects to /dev/null (e.g. 2>/dev/null),
    # then check if any real file-writing redirects remain.
    cmd_no_devnull = _REDIRECT_TO_DEV_NULL.sub('', cmd)
    if _REDIRECT_PATTERN.search(cmd_no_devnull):
        return True

    # sed -i (in-place edit) → destructive even though sed itself is a filter
    if _SED_INPLACE.search(cmd):
        return True

    # Split pipeline into individual commands and check each segment
    # e.g. "grep foo | sort | wc -l" → ['grep', 'sort', 'wc']
    segments = _split_pipeline(cmd)
    for seg in segments:
        if not seg:
            continue
        # Strip leading env vars (FOO=bar cmd ...)
        while _re.match(r'^\w+=\S*\s', seg):
            seg = _re.sub(r'^\w+=\S*\s+', '', seg, count=1)
        # Get base command name
        parts = seg.split()
        if not parts:
            continue
        base = parts[0].split('/')[-1]  # handle /usr/bin/rm → rm

        # ── Special-case: git ──
        if base == 'git':
            sub = parts[1] if len(parts) > 1 else ''
            if sub in _GIT_READONLY_SUBCOMMANDS:
                continue  # this segment is safe
            # Any other git sub-command (including destructive ones
            # and unknown future ones) → destructive
            return True

        # ── Check readonly whitelist ──
        if base in _READONLY_COMMANDS:
            continue  # this segment is safe

        # ── Everything else → destructive ──
        # This includes: interpreters (python, node, ruby, perl, bash),
        # build tools (make, cmake, cargo, go), package managers (npm, pip),
        # file ops (rm, mv, cp, touch, chmod, tar, …), and any unknown binary.
        return True

# ── Catastrophic top-level deletion guard ───────────────────────────
# Deleting a first-level directory (``/mnt``, ``/home``, ``/data`` …) or
# the filesystem root itself is never a legitimate agent action — it is
# the failure mode that wiped other teams' paths during a benchmark run.
# We refuse it OUTRIGHT (no trash rewrite, no approval bypass), parsing
# the actual ``rm``/``rmdir`` arguments so flag order can't smuggle it
# past us (``rm -fr /mnt`` == ``rm /mnt -r -f``).
_DELETE_COMMANDS = frozenset({'rm', 'rmdir', 'unlink'})

# Number of leading path components a delete target must have to be
# considered "deep enough" to allow. ``/`` → 0, ``/mnt`` → 1 (blocked),
# ``/mnt/foo`` → 2 (allowed). Tunable via env for stricter sites.
_MIN_DELETE_DEPTH = max(1, int(os.environ.get('TOFU_MIN_DELETE_DEPTH', '2')))


def _is_catastrophic_delete(command, cwd=None):
    """Return the offending path if *command* deletes a forbidden abs target.

    Two independent rules; either one rejects:

      1. **Depth rule (always on).** A delete (``rm``/``rmdir``/``unlink``)
         whose resolved absolute target has fewer than ``_MIN_DELETE_DEPTH``
         path components (``/``, ``/mnt``, ``/home``, ``~`` → home root) is
         catastrophic and refused for every caller — this is never legitimate.

      2. **Workspace-containment rule (restricted callers only).** When the
         current task runs in a *restricted* context — i.e. a remote /
         ``agent/run`` principal, the same flag ``abs_path_guard`` uses to
         sandbox absolute read/write — any absolute delete whose realpath
         falls OUTSIDE the active workspace ``cwd`` is refused, even if it is
         "deep enough" to pass rule 1. This is what stops an agent from
         deleting another user's path (``/mnt/.../INS/<other>/…``) or a home
         dir (``/home/<someone>``) or escaping via ``..``. Local desktop / CLI
         callers are NOT restricted, so their ability to delete outside the
         project (e.g. ``rm -rf ~/old_build``) is preserved — no product
         regression.

    Only absolute / ``~`` / env-var-expanded targets are evaluated: a
    relative ``rm -rf build`` stays inside ``cwd`` and is always safe.
    """
    if not command or not command.strip():
        return None
    # Workspace containment only engages for restricted principals AND when a
    # workspace cwd is known. Import locally to avoid a module import cycle.
    ws_real = None
    try:
        from lib.project_mod.abs_path_guard import is_restricted
        if cwd and is_restricted():
            ws_real = os.path.realpath(cwd)
    except Exception:
        ws_real = None
    for seg in _split_pipeline(command):
        seg = seg.strip()
        if not seg:
            continue
        while _re.match(r'^\w+=\S*\s', seg):
            seg = _re.sub(r'^\w+=\S*\s+', '', seg, count=1)
        parts = seg.split()
        if not parts:
            continue
        base_cmd = parts[0].split('/')[-1]
        if base_cmd not in _DELETE_COMMANDS:
            continue
        for arg in parts[1:]:
            if arg.startswith('-'):
                continue
            # Only absolute / home / env-expanded targets can escape the
            # workspace. A bare relative path stays inside cwd.
            if not (arg.startswith('/') or arg.startswith('~')
                    or arg.startswith('$')):
                continue
            # Strip a wildcard tail (``/mnt/*`` is as bad as ``/mnt``).
            cleaned = arg.rstrip('/*')
            expanded = os.path.expanduser(os.path.expandvars(cleaned))
            # Unresolved env var (``$FOO`` with FOO unset) → treat the bare
            # prefix as root-ish and block, since we can't prove it's safe.
            if expanded.startswith('$') or not expanded:
                return arg
            if not expanded.startswith('/'):
                continue
            depth = len([c for c in expanded.split('/') if c])
            if depth < _MIN_DELETE_DEPTH:
                return arg
            # ★ Rule 2: restricted callers may only delete inside the workspace.
            if ws_real:
                tgt_real = os.path.realpath(expanded)
                if not (tgt_real == ws_real
                        or tgt_real.startswith(ws_real + os.sep)):
                    return arg
    return None


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
    shim = prune + (
        'rm() { '
        '_td="{trash}/$(date +%Y%m%d_%H%M%S_%N)"; mkdir -p "$_td"; '
        '[ -f "{trash}/.gitignore" ] || printf "*\\n" > "{trash}/.gitignore" 2>/dev/null; '
        'for _a in "$@"; do '
        'case "$_a" in -*) continue;; esac; '
        '[ -e "$_a" ] || [ -L "$_a" ] || continue; '
        'mkdir -p "$_td/$(dirname "$_a")" 2>/dev/null; '
        'mv "$_a" "$_td/$_a" 2>/dev/null || cp -a "$_a" "$_td/$_a" 2>/dev/null; '
        'done; return 0; }; '
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


def _format_run_output(command, stdout, stderr, exit_code, timed_out=False, aborted=False):
    """Format command output into the standard result text."""
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
    if aborted:
        result_text += '\n[Command aborted by user]\n[exit code: -1]'
    elif timed_out:
        result_text += '\n[Command timed out]\n[exit code: -1]'
    else:
        result_text += f'\n[exit code: {exit_code}]'
    return result_text


def tool_run_command(base, command, timeout=None, stdin_callback=None, task=None,
                     on_chunk=None):
    """Execute a shell command with optional interactive stdin support.

    Args:
        base: Working directory for the command.
        command: Shell command string to execute.
        timeout: Timeout in seconds (0 = unlimited, None = auto-detect).
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
    """
    if not command or not command.strip():
        return 'Error: Empty command.'

    if not base:
        base = os.path.expanduser('~')

    # ★ Resolve timeout
    if timeout is None:
        timeout = 60 if _FS_HEAVY_RE.search(command) else 300
    if not isinstance(timeout, (int, float)):
        timeout = 300
    elif int(timeout) == 0:
        timeout = None
    elif MAX_COMMAND_TIMEOUT is not None:
        timeout = max(1, min(int(timeout), MAX_COMMAND_TIMEOUT))
    else:
        timeout = max(1, int(timeout)) if timeout > 0 else 300

    if _DANGEROUS_RE.search(command):
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

    # ── Non-interactive fast path (no stdin_callback) ──
    if not stdin_callback:
        return _run_command_simple(command, full_command, timeout, base, task=task,
                                   on_chunk=on_chunk)

    # ── Interactive path: Popen with stdin pipe + stdin detection ──
    return _run_command_interactive(command, full_command, timeout, base, stdin_callback,
                                    on_chunk=on_chunk, task=task)


def _safe_on_chunk(on_chunk, stream, text):
    """Invoke an on_chunk callback, swallowing any exception.

    The callback is user-supplied (comes from the SSE layer).  A bug in the
    frontend-event emission MUST NOT abort the subprocess.
    """
    if not on_chunk or not text:
        return
    try:
        on_chunk(stream, text)
    except Exception as e:
        logger.debug('[run_command] on_chunk callback raised: %s', e)


def _run_command_simple(command, full_command, timeout, base, task=None, on_chunk=None):
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
# Detect unquoted shell metachars that indicate a pipeline / redirection.
# A simple scan over single/double quote state suffices — we don't need to
# fully parse the shell.
def _has_unquoted_shell_metachars(cmd):
    in_single = False
    in_double = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if ch == '\\' and not in_single and i + 1 < len(cmd):
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in '|;&<>`':
                return True
            if ch == '$' and i + 1 < len(cmd) and cmd[i + 1] == '(':
                return True
        i += 1
    return False


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
                              on_chunk=None, task=None):
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

    try:
        while True:
            # ── abort: user clicked Stop (cooperative, ~0.2s granularity) ──
            if task and task.get('aborted'):
                logger.info('[run_command] Task aborted — killing interactive subprocess '
                            'PID %d: %s', proc.pid, command[:80])
                _kill_process_tree(proc)
                aborted = True
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

    if aborted:
        return _format_run_output(command, stdout, stderr, -1,
                                  timed_out=False, aborted=True)
    exit_code = proc.returncode if not timed_out else -1
    logger.info('run_command done (interactive): exit=%d, stdout=%dch, stderr=%dch',
                exit_code, len(stdout), len(stderr))
    return _format_run_output(command, stdout, stderr, exit_code, timed_out=timed_out)

