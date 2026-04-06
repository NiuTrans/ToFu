"""Project tool implementations — dispatch façade.

Individual tool groups have been extracted into sibling modules:
  read_tools.py   — list_dir, read_file, read_files, grep, find_files
  write_tools.py  — write_file, apply_diff, apply_diffs, _find_closest_match

This file retains:
  - Command output cleanup (_clean_command_output)
  - run_command and its helpers (_is_destructive_command, _snapshot, etc.)
  - browse_directory
  - Tool dispatch (execute_tool, project_tool_display, _resolve_base)
  - Re-exports of all symbols from read_tools / write_tools for backward compat
"""
import os
import re
import subprocess
import time
from collections import Counter

from lib.log import get_logger
from lib.project_mod.config import (
    CODE_EXTENSIONS,
    DANGEROUS_PATTERNS,
    IGNORE_DIRS,
    MAX_COMMAND_OUTPUT,
    MAX_COMMAND_TIMEOUT,
    SHELL_PREFIX,
)
from lib.project_mod.modifications import (
    _record_modification,
)
from lib.project_mod.scanner import (
    _fmt_size,
)

logger = get_logger(__name__)

# ── Re-export from read_tools (backward compat) ──
from lib.project_mod.read_tools import (  # noqa: E402,F401
    _extract_symbols,
    _merge_same_file_ranges,
    _python_grep,
    tool_find_files,
    tool_grep,
    tool_list_dir,
    tool_read_file,
    tool_read_files,
)

# ── Re-export from write_tools (backward compat) ──
from lib.project_mod.write_tools import (  # noqa: E402,F401
    _apply_one_diff,
    _find_closest_match,
    _touch_for_vscode,
    tool_apply_diff,
    tool_apply_diffs,
    tool_write_file,
)

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

# ★ Pre-compiled regex & env for run_command — avoids per-call overhead
_FS_HEAVY_RE = re.compile(r'\b(du|find|locate|tree|wc\s+-|cloc|sloccount|ncdu|fd)\b')
_DANGEROUS_RE = re.compile('|'.join(f'(?:{p})' for p in DANGEROUS_PATTERNS))
_CMD_ENV = None  # lazy-built on first use (os.environ may not be final at import time)

def _get_cmd_env():
    """Return a pre-built env dict for subprocess calls (lazy singleton).

    Sets PYTHONUNBUFFERED=1 on all platforms.
    Sets TERM=dumb on Unix to suppress terminal escape codes.
    """
    global _CMD_ENV
    if _CMD_ENV is None:
        from lib.compat import IS_WINDOWS
        _CMD_ENV = os.environ.copy()
        _CMD_ENV['PYTHONUNBUFFERED'] = '1'
        # TERM=dumb suppresses progress bars and colors in child processes.
        # On Windows, TERM is not meaningful — cmd.exe ignores it.
        if not IS_WINDOWS:
            _CMD_ENV['TERM'] = 'dumb'
    return _CMD_ENV


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


def _format_device_range(ids):
    """Format device IDs as a compact range string.

    Examples:
        [0,1,2,3,4,5,6,7] → 'cuda:0-7'
        [0,2,5] → 'cuda:0,2,5'
        [3] → 'cuda:3'
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
                    dev_range = _format_device_range(device_ids)
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

def _dummy_read_write_tools_removed():
    """Marker: tool_list_dir through tool_apply_diffs moved to submodules."""
    pass



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
    'pgdata',           # PostgreSQL internal files (WAL, base/, pg_stat, …)
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
    'find', 'fd', 'locate', 'which', 'whereis', 'type',
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
            # Prune ignored dirs in-place — exclude IGNORE_DIRS + DB engine dirs
            # Note: dot-dirs like .chatui/.project_sessions are still walked
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
                except OSError:
                    pass
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


def _format_run_output(command, stdout, stderr, exit_code, timed_out=False):
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
    if timed_out:
        result_text += '\n⏰ Command timed out.\n[exit code: -1]'
    else:
        result_text += f'\n[exit code: {exit_code}]'
    return result_text


def tool_run_command(base, command, timeout=None, stdin_callback=None):
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
    """
    if not command or not command.strip():
        return '❌ Empty command.'

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
        return '❌ Command blocked for safety: matches dangerous pattern.'

    shell_prefix = SHELL_PREFIX
    full_command = f'{shell_prefix} {command}' if shell_prefix else command

    timeout_str = f'{timeout}s' if timeout else 'unlimited'
    logger.info('run_command: $ %s  (timeout=%s, cwd=%s, interactive=%s)',
                command[:120], timeout_str, base, bool(stdin_callback))

    # ── Non-interactive fast path (no stdin_callback) ──
    if not stdin_callback:
        return _run_command_simple(command, full_command, timeout, base)

    # ── Interactive path: Popen with stdin pipe + stdin detection ──
    return _run_command_interactive(command, full_command, timeout, base, stdin_callback)


def _run_command_simple(command, full_command, timeout, base):
    """Original subprocess.run approach — no stdin interaction."""
    from lib.compat import get_shell_args
    try:
        result = subprocess.run(
            get_shell_args(full_command),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=base,
            errors='replace',
            env=_get_cmd_env(),
        )
        logger.info('run_command done: exit=%d, stdout=%dch, stderr=%dch',
                     result.returncode, len(result.stdout or ''), len(result.stderr or ''))
        return _format_run_output(command, result.stdout or '', result.stderr or '',
                                  result.returncode)
    except subprocess.TimeoutExpired:
        logger.warning('run_command timed out after %ss', timeout)
        return _format_run_output(command, '', '', -1, timed_out=True)
    except Exception as e:
        logger.error('run_command error: %s', e, exc_info=True)
        return (f'$ {command}\n\n'
                f'❌ Error executing command: {e}\n'
                f'[exit code: -1]')


def _is_any_child_reading_stdin(parent_pid, stdin_pipe_ino):
    """Check if any descendant of *parent_pid* is blocked in read(2) on our stdin pipe.

    Uses ``/proc/<pid>/syscall`` to definitively detect:
      - syscall_nr == 0 (read)
      - arg0 == 0 (fd 0 = stdin)
    combined with verifying that the child's fd 0 inode matches our stdin
    pipe inode (to avoid false positives from unrelated processes).

    Returns the (pid, comm) tuple of the blocked process, or None.

    **Linux-only**: requires /proc filesystem. Returns None on macOS/Windows.
    """
    from lib.compat import HAS_PROCFS
    if not HAS_PROCFS:
        return None  # stdin detection unavailable on this platform
    try:
        pids_to_check = _collect_descendants(parent_pid)
    except OSError:
        return None

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
                except OSError:
                    comm = '?'
                return (pid, comm)
        except (OSError, ValueError, IndexError):
            # Process may have exited between checks — harmless
            continue
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
        except (OSError, ValueError, IndexError):
            pass

    # BFS from parent_pid
    result = [parent_pid]
    queue = [parent_pid]
    while queue:
        p = queue.pop()
        for child in children_map.get(p, []):
            result.append(child)
            queue.append(child)
    return result


def _run_command_interactive(command, full_command, timeout, base, stdin_callback):
    """Popen-based execution with stdin detection and interactive input.

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
            env=_get_cmd_env(),
            text=False,  # binary mode for non-blocking I/O
        )
    except Exception as e:
        logger.error('run_command Popen error: %s', e, exc_info=True)
        return (f'$ {command}\n\n'
                f'❌ Error starting command: {e}\n'
                f'[exit code: -1]')

    # Set stdout/stderr to non-blocking (no-op on Windows, uses threading there).
    # If this fails on an unusual platform, interactive I/O will still work via
    # polling with small timeouts — it just won't be as responsive.
    nonblocking_ok = all(
        set_pipe_nonblocking(fd) for fd in (proc.stdout, proc.stderr)
    )
    if not nonblocking_ok:
        logger.warning('run_command: non-blocking pipe setup failed — falling back to polling I/O')

    # Get the inode of our stdin pipe so we can match it in /proc
    try:
        stdin_pipe_ino = os.fstat(proc.stdin.fileno()).st_ino
    except OSError:
        stdin_pipe_ino = None

    stdout_chunks = []
    stderr_chunks = []
    start_time = time.monotonic()
    stdin_closed = False
    timed_out = False

    try:
        while True:
            # Check timeout
            elapsed = time.monotonic() - start_time
            if timeout and elapsed > timeout:
                logger.warning('run_command timed out after %ss (interactive)', timeout)
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
            except (ValueError, OSError):
                # fd already closed
                readable = []

            for fd in readable:
                try:
                    chunk = fd.read(65536)
                    if chunk:
                        got_output = True
                        if fd is proc.stdout:
                            stdout_chunks.append(chunk)
                        else:
                            stderr_chunks.append(chunk)
                except (BlockingIOError, OSError):
                    pass

            if retcode is not None and not got_output:
                # Process exited and no more data — drain remaining
                try:
                    rest_out = proc.stdout.read()
                    if rest_out:
                        stdout_chunks.append(rest_out)
                except (BlockingIOError, OSError):
                    pass
                try:
                    rest_err = proc.stderr.read()
                    if rest_err:
                        stderr_chunks.append(rest_err)
                except (BlockingIOError, OSError):
                    pass
                break

            # ★ Stdin detection: check /proc/pid/syscall for read(0, ...) on our pipe
            if (retcode is None and not stdin_closed
                    and stdin_pipe_ino is not None):
                reader = _is_any_child_reading_stdin(proc.pid, stdin_pipe_ino)
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
                        except OSError:
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
        except OSError:
            pass
        return (f'$ {command}\n\n'
                f'❌ Error during interactive execution: {e}\n'
                f'[exit code: -1]')
    finally:
        # Clean up
        for fd in (proc.stdin, proc.stdout, proc.stderr):
            try:
                fd.close()
            except (OSError, AttributeError):
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    stdout = b''.join(stdout_chunks).decode('utf-8', errors='replace')
    stderr = b''.join(stderr_chunks).decode('utf-8', errors='replace')
    exit_code = proc.returncode if not timed_out else -1

    logger.info('run_command done (interactive): exit=%d, stdout=%dch, stderr=%dch',
                exit_code, len(stdout), len(stderr))
    return _format_run_output(command, stdout, stderr, exit_code, timed_out=timed_out)


# ═══════════════════════════════════════════════════════
#  ★ Directory Browser — NEW
# ═══════════════════════════════════════════════════════

def browse_directory(path_str=None, show_hidden=False):
    """List subdirectories at a given path for folder browser UI."""
    if not path_str or path_str == '~':
        path_str = os.path.expanduser('~')
    abs_path = os.path.abspath(os.path.expanduser(path_str))

    if not os.path.isdir(abs_path):
        return {'error': f'Not a directory: {abs_path}', 'path': abs_path}

    parent = os.path.dirname(abs_path)
    dirs = []
    files_count = 0
    try:
        for entry in sorted(os.scandir(abs_path), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not show_hidden and entry.name.startswith('.'):
                        continue
                    # Check if it looks like a project (has code files)
                    has_code = False
                    item_count = 0
                    try:
                        for sub in os.scandir(entry.path):
                            item_count += 1
                            if item_count > 100:
                                break
                            ext = os.path.splitext(sub.name)[1].lower()
                            if ext in CODE_EXTENSIONS:
                                has_code = True
                    except (PermissionError, OSError) as e:
                        logger.debug('[Tools] dir scan failed for %s: %s', entry.name, e, exc_info=True)
                    dirs.append({
                        'name': entry.name,
                        'path': entry.path,
                        'itemCount': item_count,
                        'hasCode': has_code,
                        'hidden': entry.name.startswith('.'),
                    })
                elif entry.is_file(follow_symlinks=False):
                    files_count += 1
            except (PermissionError, OSError) as e:
                logger.debug('[Tools] entry processing failed for entry: %s', e, exc_info=True)
                continue
    except PermissionError:
        logger.debug('[Tools] permission denied scanning %s', abs_path, exc_info=True)
        return {'error': f'Permission denied: {abs_path}', 'path': abs_path}

    return {
        'path': abs_path,
        'parent': parent if parent != abs_path else None,
        'dirs': dirs,
        'filesCount': files_count,
        'showHidden': show_hidden,
    }


# ═══════════════════════════════════════════════════════
#  Tool Dispatch
# ═══════════════════════════════════════════════════════

def _resolve_base(base_path, rel_path):
    """Resolve base_path + rel_path, supporting multi-root 'name:path' syntax.

    If rel_path contains ':', treat the part before ':' as a root name.
    Otherwise fall back to the provided base_path.

    ★ Cross-root safety: when multiple roots are configured, checks if
    the requested relative path exists under the primary root.  If it
    does NOT exist there but DOES exist under exactly one other root,
    auto-routes to that root and logs a warning.  This prevents the
    common model mistake of writing files intended for root B into root A.

    Returns (effective_base, effective_rel).
    """
    if rel_path and ':' in rel_path and not os.path.isabs(rel_path):
        # Check it's not a Windows drive letter like C:\...
        colon_idx = rel_path.index(':')
        if colon_idx > 0 and colon_idx < 40:  # reasonable name length
            from lib.project_mod.config import resolve_namespaced_path
            try:
                return resolve_namespaced_path(rel_path)
            except ValueError:
                logger.debug('[Tools] namespaced path %s not a known root, treating as plain path', rel_path[:40], exc_info=True)

    # ── Multi-root cross-check for path-misrouting ──
    # When the model forgets the 'rootname:' prefix in a multi-root
    # workspace, the path silently resolves under the primary root.
    # If the file/dir does NOT exist under primary but DOES exist under
    # exactly one other root, auto-route there.  This is a safety net,
    # not a substitute for proper 'rootname:' prefix usage.
    if base_path and rel_path and rel_path not in ('.', '', '/'):
        from lib.project_mod.config import _lock as _cfg_lock
        from lib.project_mod.config import _roots
        with _cfg_lock:
            if len(_roots) > 1:
                primary_target = os.path.join(base_path, rel_path)
                if not os.path.exists(primary_target):
                    # File doesn't exist under primary — check other roots
                    candidate_roots = []
                    for rn, rs in _roots.items():
                        if rs['path'] == base_path:
                            continue
                        other_target = os.path.join(rs['path'], rel_path)
                        if os.path.exists(other_target):
                            candidate_roots.append((rn, rs['path']))
                    if len(candidate_roots) == 1:
                        rn, rp = candidate_roots[0]
                        logger.warning(
                            '[Tools] ★ Cross-root auto-route: %s not found under primary %s '
                            'but exists under [%s] %s — routing there. '
                            'Model should use \'%s:%s\' prefix to be explicit.',
                            rel_path, base_path, rn, rp, rn, rel_path)
                        return rp, rel_path
                    elif len(candidate_roots) > 1:
                        names = ', '.join(f'{rn}' for rn, _ in candidate_roots)
                        logger.warning(
                            '[Tools] ★ Ambiguous multi-root path: %s not found under primary '
                            'but exists in multiple roots (%s). Using primary as fallback. '
                            'Model should use explicit root prefix.',
                            rel_path, names)

    return base_path, rel_path


def execute_tool(fn_name, fn_args, base_path, conv_id=None, task_id=None, **kwargs):
    # ★ Multi-root: resolve 'rootname:relative/path' for path-based tools
    if fn_name == 'list_dir':
        bp, rp = _resolve_base(base_path, fn_args.get('path', '.'))
        return tool_list_dir(bp, rp)
    elif fn_name == 'read_files':
        reads = fn_args.get('reads', [])
        # Resolve base for each read spec
        resolved = []
        for spec in reads:
            if isinstance(spec, dict) and 'path' in spec:
                bp2, rp2 = _resolve_base(base_path, spec['path'])
                resolved.append({'path': rp2, 'start_line': spec.get('start_line'), 'end_line': spec.get('end_line'), '_base': bp2})
            elif isinstance(spec, str) and spec.strip():
                # Model sent a bare string path instead of {path: ...} — normalise it
                bp2, rp2 = _resolve_base(base_path, spec.strip())
                resolved.append({'path': rp2, '_base': bp2})
                logger.debug('[Tools] read_files: normalised bare string spec %r → dict', spec[:80])
            else:
                logger.warning('[Tools] read_files: skipping invalid spec type=%s val=%r',
                               type(spec).__name__, str(spec)[:120])
                # Do NOT append raw spec — downstream code expects dicts with .get()
                continue
        # Use first resolved base (all should be same in single-root projects)
        bp_main = base_path
        for r in resolved:
            if isinstance(r, dict) and '_base' in r:
                bp_main = r['_base']
                break
        # For multi-root, we call tool_read_file individually per spec
        parts = []
        total_chars = 0
        BATCH_CHAR_BUDGET = 200_000
        MAX_BATCH = 20
        for i, spec in enumerate(resolved[:MAX_BATCH]):
            if not isinstance(spec, dict):
                parts.append(f'[{i+1}] Error: invalid entry')
                continue
            bp2 = spec.pop('_base', bp_main)
            result = tool_read_file(bp2, spec.get('path', ''), spec.get('start_line'), spec.get('end_line'))
            if total_chars + len(result) > BATCH_CHAR_BUDGET:
                remaining = BATCH_CHAR_BUDGET - total_chars
                if remaining > 200:
                    result = result[:remaining] + '\n… [truncated — batch budget exceeded]'
                else:
                    parts.append(f'… [{len(resolved) - i} more files skipped — batch budget exceeded]')
                    break
            total_chars += len(result)
            parts.append(result)
        return '\n\n'.join(parts)
    elif fn_name == 'grep_search':
        search_path = fn_args.get('path')
        bp = base_path
        if search_path:
            bp, search_path = _resolve_base(base_path, search_path)
        return tool_grep(bp, fn_args.get('pattern', ''),
                         search_path, fn_args.get('include'),
                         fn_args.get('context_lines'),
                         max_results=fn_args.get('max_results'),
                         count_only=bool(fn_args.get('count_only', False)))
    elif fn_name == 'find_files':
        search_path = fn_args.get('path')
        bp = base_path
        if search_path:
            bp, search_path = _resolve_base(base_path, search_path)
        return tool_find_files(bp, fn_args.get('pattern', ''),
                               search_path,
                               max_results=fn_args.get('max_results'))
    # ★ Write tools — pass conv_id + task_id for per-round undo
    elif fn_name == 'write_file':
        bp, rp = _resolve_base(base_path, fn_args.get('path', ''))
        result = tool_write_file(bp, rp,
                                 fn_args.get('content', ''),
                                 fn_args.get('description', ''),
                                 conv_id=conv_id, task_id=task_id)
        if result['ok']:
            return (f"✅ File {'created' if result.get('created') else 'updated'}: {result['path']} "
                    f"({result['lines']} lines, {_fmt_size(result['bytesWritten'])})")
        else:
            return f"❌ Write failed: {result['error']}"
    elif fn_name == 'apply_diff':
        # ★ Batch mode: if 'edits' array is present, apply all edits in sequence
        edits = fn_args.get('edits')
        if edits and isinstance(edits, list):
            return tool_apply_diffs(base_path, edits, conv_id=conv_id, task_id=task_id)
        # ★ Single-edit mode (backward compatible)
        bp, rp = _resolve_base(base_path, fn_args.get('path', ''))
        result = tool_apply_diff(bp, rp,
                                 fn_args.get('search', ''),
                                 fn_args.get('replace', ''),
                                 fn_args.get('description', ''),
                                 conv_id=conv_id, task_id=task_id,
                                 replace_all=bool(fn_args.get('replace_all', False)))
        if result['ok']:
            msg = (f"✅ Applied diff to {result['path']}: "
                   f"{result['linesChanged']} lines changed "
                   f"({result['oldLines']}L → {result['newLines']}L)")
            if result.get('replacedCount'):
                msg += f" [{result['replacedCount']} occurrences replaced]"
            return msg
        else:
            return f"❌ Diff failed: {result['error']}"
    elif fn_name == 'read_local_file':
        from lib.file_reader import read_local_file as _read_local
        file_path = fn_args.get('path', '')
        return _read_local(file_path)
    elif fn_name == 'run_command':
        # ★ Multi-root: resolve working_dir if model specifies one
        cwd = base_path
        working_dir = fn_args.get('working_dir', '')
        if working_dir:
            cwd_bp, _ = _resolve_base(base_path, working_dir)
            cwd = os.path.join(cwd_bp, _) if _ and _ != '.' else cwd_bp

        command_str = fn_args.get('command', '')
        destructive = _is_destructive_command(command_str)

        # ★ Pre-compute write targets to decide if snapshotting is useful.
        # If we can't determine specific targets (opaque commands like
        # python3, make, npm …), DON'T snapshot — the diff would include
        # every file that changed autonomously (log files, DB WAL, etc.)
        # and we'd report false positives.  Only snapshot when we know
        # exactly which files the command WRITES to.
        write_targets = _extract_write_targets(command_str, cwd) if destructive else set()
        # write_targets: set = specific files;  None = opaque;  empty set = read-only
        can_track = destructive and write_targets is not None and len(write_targets) > 0

        snap_before = None
        _saved_contents = {}
        if can_track:
            # ★ Take filesystem snapshot before command to detect changes
            snap_before = _snapshot_project_files(cwd)
            # Save content of existing files so we can undo deletions/modifications
            # Cap per-file at 100KB, total at 20MB to avoid memory explosion
            _total_saved = 0
            _MAX_FILE_SAVE = 100 * 1024
            _MAX_TOTAL_SAVE = 20 * 1024 * 1024
            for rel, mtime in snap_before.items():
                if _total_saved >= _MAX_TOTAL_SAVE:
                    break
                abs_p = os.path.join(cwd, rel)
                try:
                    fsize = os.path.getsize(abs_p)
                    if fsize > _MAX_FILE_SAVE:
                        continue
                    with open(abs_p, 'rb') as f:
                        raw = f.read(_MAX_FILE_SAVE)
                    _saved_contents[rel] = raw
                    _total_saved += len(raw)
                except OSError:
                    pass
            logger.debug('[run_command] Snapshot taken (%d files), write_targets=%s: %.200s',
                         len(snap_before), write_targets, command_str)
        elif destructive:
            logger.debug('[run_command] Opaque command, skipping snapshot (no deterministic write targets): %.200s',
                         command_str)
        else:
            logger.debug('[run_command] Read-only command, skipping snapshot: %.200s', command_str)

        result = tool_run_command(cwd,
                                  command_str,
                                  fn_args.get('timeout', None),
                                  stdin_callback=kwargs.get('stdin_callback'))

        # ★ Diff snapshot after command (only if we took one)
        if snap_before is not None:
            snap_after = _snapshot_project_files(cwd)
            changes = _diff_snapshots(cwd, snap_before, snap_after)
            if changes:
                # ★ Filter changes to only include files the command
                # could plausibly write to.  write_targets was already
                # computed above and is guaranteed to be a non-empty set
                # (not None) since we only snapshot when can_track=True.
                changes = _filter_changes_by_targets(changes, write_targets, cwd)
                if changes:
                    logger.debug('[run_command] Write targets=%s, filtered to %d change(s)',
                                 write_targets, len(changes))
                # Enrich deleted/modified entries with original content for undo
                for ch in changes:
                    rel = ch['rel_path']
                    if ch['change_type'] in ('deleted', 'modified'):
                        raw = _saved_contents.get(rel)
                        if raw is not None:
                            # Try to decode as text; keep as bytes if binary
                            try:
                                ch['original_content'] = raw.decode('utf-8')
                            except (UnicodeDecodeError, ValueError):
                                ch['original_content'] = raw
                recorded = _record_run_command_changes(
                    cwd, changes, conv_id=conv_id, task_id=task_id)
                if recorded:
                    logger.info('[run_command] Detected %d file change(s): %s',
                                len(recorded),
                                ', '.join(f"{r['path']}({r['action']})" for r in recorded[:10]))
        return result
    return f'Unknown project tool: {fn_name}'


def execute_standalone_command(fn_name, fn_args, working_dir=None, stdin_callback=None):
    """Execute run_command without requiring a project path."""
    if fn_name == 'run_command':
        return tool_run_command(working_dir,
                                fn_args.get('command', ''),
                                fn_args.get('timeout', None),
                                stdin_callback=stdin_callback)
    return f'Unknown tool: {fn_name}'


def project_tool_display(fn_name, fn_args):
    """Return a concise display string for a project tool call (no emoji prefix — added by frontend)."""
    if not isinstance(fn_args, dict):
        return f'{fn_name}({fn_args})'
    if fn_name == 'read_files':
        reads = fn_args.get('reads', [])
        if not reads:
            return 'Read files (empty)'
        # Group by unique path, collect line ranges per file
        from collections import OrderedDict
        grouped = OrderedDict()
        for r in reads:
            # LLM sometimes produces ["path1", "path2"] instead of [{path: "path1"}, ...]
            if isinstance(r, str):
                grouped.setdefault(r, [])
                continue
            if not isinstance(r, dict):
                continue
            p = r.get('path', '?')
            sl, el = r.get('start_line'), r.get('end_line')
            grouped.setdefault(p, [])
            if sl is not None and el is not None:
                grouped[p].append(f'L{sl}-{el}')
            elif sl is not None:
                grouped[p].append(f'L{sl}+')
        n_files = len(grouped)
        # Disambiguate duplicate basenames
        basenames = [p.rsplit('/', 1)[-1] for p in grouped]
        from collections import Counter
        dup = {b for b, c in Counter(basenames).items() if c > 1}
        parts = []
        for p, ranges in list(grouped.items())[:4]:
            base = p.rsplit('/', 1)[-1]
            name = '/'.join(p.rsplit('/', 2)[-2:]) if base in dup else base
            if ranges:
                parts.append(f'{name} {", ".join(ranges)}')
            else:
                parts.append(name)
        suffix = f' +{n_files - 4} more' if n_files > 4 else ''
        return f'Read {n_files} file{"s" if n_files != 1 else ""}: {"; ".join(parts)}{suffix}'
    elif fn_name == 'grep_search':
        pat = fn_args.get('pattern', '?')[:40]
        inc = fn_args.get('include', '')
        search_path = fn_args.get('path', '')
        suffix = ''
        if inc and search_path:
            suffix = f' in {inc} ({search_path})'
        elif inc:
            suffix = f' in {inc}'
        elif search_path:
            suffix = f' in {search_path}'
        return f'grep /{pat}/' + suffix
    elif fn_name == 'list_dir':
        return f'List {fn_args.get("path", ".")}'
    elif fn_name == 'find_files':
        search_path = fn_args.get('path', '')
        return f'Find {fn_args.get("pattern", "?")}' + (f' in {search_path}' if search_path else '')
    elif fn_name == 'write_file':
        p = fn_args.get('path', '?')
        desc = fn_args.get('description', '')
        return f'Write {p}' + (f' — {desc}' if desc else '')
    elif fn_name == 'apply_diff':
        edits = fn_args.get('edits')
        if edits and isinstance(edits, list):
            paths = list(dict.fromkeys(e.get('path', '?') for e in edits if isinstance(e, dict)))
            n = len(edits)
            desc = fn_args.get('description', '')
            if len(paths) == 1:
                label = f'Patch {paths[0]} ({n} edits)'
            elif len(paths) <= 3:
                label = f'Patch {", ".join(paths)} ({n} edits)'
            else:
                label = f'Patch {len(paths)} files ({n} edits)'
            return label + (f' — {desc}' if desc else '')
        p = fn_args.get('path', '?')
        desc = fn_args.get('description', '')
        return f'Patch {p}' + (f' — {desc}' if desc else '')
    elif fn_name == 'run_command':
        cmd = fn_args.get('command', '?')
        return cmd  # Full command without $ prefix — frontend adds it
    elif fn_name == 'read_local_file':
        p = fn_args.get('path', '?')
        return f'Read {os.path.basename(p)}'
    return fn_name

