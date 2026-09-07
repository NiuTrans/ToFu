"""Pure command-string / command-output analysis.

Extracted from ``lib/project_mod/run_command.py`` (2026-07-11) to isolate the
side-effect-free analysis layer from the subprocess-execution machinery. Every
function here is PURE: it inspects a command string or captured output text and
returns a verdict (dangerous? destructive? which files does it write? which
top-level delete is catastrophic?) or a cleaned/compressed copy of output. None
of them spawn a process, read process state, or mutate the filesystem — the one
filesystem touch is the read-only ``os.path.realpath`` workspace-containment
check inside :func:`_is_catastrophic_delete`.

``run_command.py`` re-imports every public symbol from here, so both existing
import paths keep working unchanged::

    from lib.project_mod.run_command import _is_destructive_command  # still works
    from lib.project_mod.tools import _is_catastrophic_delete        # still works

There is no dependency back on ``run_command.py`` — the execution layer depends
on this module, not the reverse.
"""

import os
import re
import shlex
from collections import Counter, namedtuple

from lib.log import get_logger
from lib.project_mod.config import DANGEROUS_PATTERNS

logger = get_logger(__name__)

# Alias kept for parity with run_command.py's historical ``import re as _re``
# usage inside the analysis functions moved here.
_re = re


# ═══════════════════════════════════════════════════════
#  Command output cleanup for LLM consumption
# ═══════════════════════════════════════════════════════

# ANSI escape codes: SGR (colors), cursor movement, OSC (window titles)
_ANSI_ESC_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-B012]')

# tqdm-style progress bar: "Label: NN%|bar_chars| X/Y [timing, rate]"
# The content inside [...] varies widely (ETA, rate, ?it/s) — match any non-]
_PROGRESS_RE = re.compile(
    r'^(.*?)\s*\d+%\|[^|]*\|\s*\d+/\d+\s*\[[^\]]*\](.*)$'
)

# Pre-compiled regex for run_command
_FS_HEAVY_RE = re.compile(r'\b(du|find|locate|tree|wc\s+-|cloc|sloccount|ncdu|fd)\b')
_DANGEROUS_RE = re.compile('|'.join(f'(?:{p})' for p in DANGEROUS_PATTERNS))


def _mask_quoted_literals(command):
    """Return *command* with the CONTENTS of quoted spans blanked to spaces.

    The dangerous-pattern guard is meant to catch command STRUCTURE — an
    actual ``shutdown`` command word, ``rm -rf /``, a ``> /dev/sd`` redirect —
    not a word that merely appears inside a quoted argument. Several patterns
    in ``DANGEROUS_PATTERNS`` are bare words (``\\bshutdown\\b``, ``\\breboot\\b``,
    ``\\bmkfs\\b``, ``\\bdiskpart\\b``), so a benign diagnostic like
    ``grep -E "graceful shutdown|shutting down" app.log`` false-positives and is
    blocked — breaking log debugging. Blanking quoted contents (delimiters
    included, so token boundaries survive) neutralises those in-string words
    while leaving unquoted command structure — and thus every structural
    pattern — intact.

    Known limitation: a dangerous word passed UNQUOTED as a search argument
    (``grep shutdown file``) is still matched; only quoted literals are masked.
    Real command isolation is enforced by the catastrophic-delete guard and the
    portable sandbox — this guard is a best-effort net over the raw string.
    """
    out = []
    in_single = in_double = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == '\\' and not in_single and i + 1 < n:
            # Escape: keep both chars when unquoted (preserve structure),
            # blank them when inside a double-quoted span.
            out.append('  ' if in_double else command[i:i + 2])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(' ')
        elif ch == '"' and not in_single:
            in_double = not in_double
            out.append(' ')
        elif in_single or in_double:
            out.append(' ')
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


def _is_dangerous_command(command):
    """True when *command* trips a hard-blocked dangerous pattern.

    Scans the command with quoted-literal contents masked (see
    :func:`_mask_quoted_literals`) so words inside a search pattern or echo
    string cannot false-positive.
    """
    return bool(_DANGEROUS_RE.search(_mask_quoted_literals(command)))


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
# ⚠️ NO PRODUCTION CALLER. This permissive union (accelerator words AND plain
# numbered workers in one class) is retained only for the exported-symbol
# contract asserted by tests/test_command_analysis_extraction.py. Because it
# cannot distinguish "cuda:0" from "io worker 0", using it to label output is
# exactly how the folder came to report three postgres processes as three CUDA
# devices. NEW CODE MUST USE _extract_accelerator_ids (hardware claims) or
# _extract_ordinal_ids (index-only claims) — never this regex.
_DEVICE_RE = re.compile(
    r'(?:cuda|gpu|device|rank|worker)[\s:_]*(\d+)', re.IGNORECASE
)

# Only these words are EVIDENCE of a real compute accelerator, and only
# they may licence a ``cuda:`` prefix in the fold marker. ``worker``/``rank``
# deliberately excluded: real ``ps aux`` output contains lines like
# "postgres: io worker 0/1/2", which the old union regex turned into
# "×3 devices on cuda:0-2" — three database processes reported as three
# GPUs. A fold marker that invents hardware is worse than no marker at all,
# so accelerator attribution now requires an accelerator word.
_ACCEL_RE = re.compile(r'(?:cuda|gpu|nvidia|hip|rocm|xpu)[\s:_]*(\d+)',
                       re.IGNORECASE)

# Numbered-variant words: enough to say "these lines differ by an index",
# NOT enough to name the hardware.
_ORDINAL_RE = re.compile(r'(?:device|rank|worker|shard|replica)[\s:_]*(\d+)',
                         re.IGNORECASE)


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

    ⚠️ NO PRODUCTION CALLER — kept for the exported-symbol contract only.
    This is the permissive union (accelerators AND plain numbered workers), so
    it does NOT establish that a GPU is involved. New code must use
    :func:`_extract_accelerator_ids` for anything that renders a ``cuda:``
    label, or :func:`_extract_ordinal_ids` for index-only claims; reusing this
    one is how the fold marker started claiming hardware that isn't there.
    """
    ids = set()
    for ln in lines:
        for m in _DEVICE_RE.finditer(ln):
            ids.add(int(m.group(1)))
    return sorted(ids)


def _extract_accelerator_ids(lines):
    """Extract IDs backed by EXPLICIT accelerator evidence (cuda/gpu/…).

    Returns sorted unique ints, or [] when the lines carry no accelerator
    word — in which case the caller must not emit a ``cuda:`` label.
    """
    ids = set()
    for ln in lines:
        for m in _ACCEL_RE.finditer(ln):
            ids.add(int(m.group(1)))
    return sorted(ids)


def _extract_ordinal_ids(lines):
    """Extract IDs from plain numbered-variant words (worker/rank/shard/…).

    Used to say "N numbered variants" WITHOUT naming any hardware.
    """
    ids = set()
    for ln in lines:
        for m in _ORDINAL_RE.finditer(ln):
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
                # ── Percentage-aware sampling + concurrency detection ──
                pcts = [(_extract_progress_pct(g), g) for g in group]
                valid = [(p, g) for p, g in pcts if p is not None]

                # How many lines share a percentage tells us the output is
                # CONCURRENT — it does NOT tell us what the workers are. The
                # old code called every such group "×N devices", so four tqdm
                # bars from a single-process data loader were reported as four
                # devices. Same rule as the Phase 4 marker: name hardware only
                # with an explicit accelerator word, otherwise describe the
                # shape ('parallel streams') and claim nothing about what runs
                # them.
                stream_count = 1
                if valid:
                    pct_freq = Counter(p for p, _ in valid)
                    stream_count = max(pct_freq.values())
                accel_ids = _extract_accelerator_ids(group)
                if len(accel_ids) > 1:
                    device_note = (f', ×{len(accel_ids)} devices on '
                                   f'{_format_cuda_device_range(accel_ids)}')
                elif stream_count > 1:
                    device_note = f', ×{stream_count} parallel streams'
                else:
                    device_note = ''

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
                # Attribution is gated on EVIDENCE, not on "some word had a
                # number after it". Three tiers, narrowest first:
                #   1. explicit accelerator word → may say cuda:N-M
                #   2. plain numbered variant (worker/rank/…) → count only
                #   3. neither → state the grouping rule, claim nothing
                # Tier 1 vs 2 matters: real `ps aux` has "postgres: io
                # worker 0/1/2", and calling that three CUDA devices is an
                # invented fact, not a lossy summary.
                accel_ids = _extract_accelerator_ids(group)
                ord_ids = _extract_ordinal_ids(group)
                if len(accel_ids) > 1:
                    dev_range = _format_cuda_device_range(accel_ids)
                    result.append(
                        f'  … (+{n - 1} lines folded, '
                        f'×{len(accel_ids)} devices on {dev_range}) …')
                elif len(ord_ids) > 1:
                    result.append(
                        f'  … (+{n - 1} lines folded, '
                        f'{len(ord_ids)} numbered variants) …')
                else:
                    # NOT "similar lines": these lines share a structural
                    # fingerprint (digit runs normalised to #) and their
                    # non-numeric text may differ completely.
                    result.append(
                        f'  … (+{n - 1} lines folded: same structure, '
                        f'differing values) …')
                total_compressed += n - 1
            i = j
            continue

        result.append(line)
        i += 1

    cleaned = '\n'.join(result)
    # The fold total must reach the MODEL, not just the log. Without it a
    # reader cannot tell whether they are looking at the whole output or a
    # fraction of it — each marker states its own group, but nothing states
    # the aggregate. Only emitted when folding actually happened, so
    # untouched output stays byte-identical.
    if total_compressed > 0:
        kept = cleaned.count('\n') + 1
        cleaned += (
            f'\n[output folded: {total_compressed} of '
            f'{len(lines)} lines omitted, {kept} shown — consecutive lines '
            f'sharing a structural fingerprint were grouped]'
        )
    if total_compressed > 5:
        logger.debug('_clean_command_output: compressed %d repetitive lines '
                     '(%d → %d chars)', total_compressed, original_len,
                     len(cleaned))
    return cleaned


# ═══════════════════════════════════════════════════════
#  Command destructiveness / write-target analysis
# ═══════════════════════════════════════════════════════

# Provably read-only shell utilities that NEVER modify the filesystem.
# Only commands whose behaviour is fully determined by the binary name
# belong here — NOT interpreters/runtimes (python, node, …) whose
# behaviour depends on the script/code they execute.
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
    # Note: sed/awk are here because plain invocations are stdout filters;
    # sed -i and gawk's `-i inplace` extension are caught separately by
    # _SED_INPLACE / _AWK_INPLACE before the whitelist check.  The awk
    # variants share awk's semantics — including its pre-existing blind
    # spot for writes from inside the program text (print > "file" is
    # caught by the redirect probe; system("...") is not, same as awk).
    'wc', 'sort', 'uniq', 'cut', 'tr', 'sed', 'column',
    'awk', 'gawk', 'mawk', 'nawk', 'goawk',
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

# awk-family in-place editing via gawk's bundled ``inplace`` extension:
# ``gawk -i inplace '{...}' file`` (also ``-iinplace``, ``--include inplace``
# and ``--include=inplace``; goawk accepts the same flags).  Like
# _SED_INPLACE this is a loose whole-command probe: it may cross pipeline
# separators and match ``-i inplace`` inside a quoted program — a false
# positive only costs one extra snapshot (~5 ms), while a false negative
# silently loses undo coverage.  Exact operand parsing happens per segment
# in _extract_write_targets via _awk_inplace_operands.
_AWK_INPLACE = _re.compile(r'\b\w*awk\b.*?\s(?:-i|--include)[\s=]*\S*inplace')


_ShellWord = namedtuple(
    '_ShellWord', ['text', 'start', 'end', 'squote', 'dquote'])


def _shell_words(s):
    """Split one shell segment into unquoted words with source offsets.

    Guards and rewriters share this parser so quoted patterns such as
    ``grep "not found"`` cannot be interpreted differently by a fallback
    path. Shell separators are handled by :func:`_split_pipeline_spans`;
    callers pass one resulting segment here.
    """
    out = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in ' \t':
            i += 1
        if i >= n:
            break
        start = i
        buf = []
        squote = dquote = False
        while i < n and s[i] not in ' \t':
            char = s[i]
            if char == '\\' and not squote:
                if i + 1 < n:
                    buf.append(s[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            if char == "'" and not dquote:
                squote = True
                i += 1
                while i < n and s[i] != "'":
                    buf.append(s[i])
                    i += 1
                i += 1
                continue
            if char == '"' and not squote:
                dquote = True
                i += 1
                while i < n and s[i] != '"':
                    if (s[i] == '\\' and i + 1 < n
                            and s[i + 1] in '$`"\\\n'):
                        buf.append(s[i + 1])
                        i += 2
                        continue
                    buf.append(s[i])
                    i += 1
                i += 1
                continue
            buf.append(char)
            i += 1
        out.append(_ShellWord(
            ''.join(buf), start, i, squote, dquote))
    return out


def _split_pipeline(cmd):
    """Split a shell command into pipeline/chain segments, respecting quotes.

    Splits on |, ;, &&, ||, newlines, and subshell parentheses but NOT
    inside single or double quotes. This prevents splitting patterns like
    ``grep -i "foo|bar"`` on the pipe.

    Newlines and bare ``(`` / ``)`` are ALWAYS command structure in shell
    grammar (a newline is a command separator; an unquoted paren is a
    subshell / function / case / expansion delimiter, never part of a
    word — escaped ``\\(`` stays glued via the backslash branch). Leaving
    them attached glues phantom tokens onto segments: the subshell closer
    in ``( producer | grep -E 'pat' )`` tokenizes as a fake grep OPERAND
    (a stream filter misread as a filesystem read), and the opener in
    ``( grep -rn x lib/ )`` masks the command word entirely (evasion).
    """
    return [cmd[s:e].strip() for s, e in _split_pipeline_spans(cmd)]


def _split_pipeline_spans(cmd):
    """Span-returning twin of :func:`_split_pipeline`: list of (start, end)
    offsets into *cmd* for each non-empty segment (separators excluded,
    surrounding whitespace included). The grep-redirect splicer rewrites
    segments in place through these spans."""
    spans = []
    start = 0
    in_single = False
    in_double = False
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c == "'" and not in_double:
            in_single = not in_single
            i += 1
        elif c == '"' and not in_single:
            in_double = not in_double
            i += 1
        elif c == '\\' and i + 1 < n and (in_double or not in_single):
            i += 2
        elif not in_single and not in_double:
            if c in ';\n()':
                spans.append((start, i))
                i += 1
                start = i
            elif c == '&' and i + 1 < n and cmd[i + 1] == '&':
                spans.append((start, i))
                i += 2
                start = i
            elif c == '|' and i + 1 < n and cmd[i + 1] == '|':
                spans.append((start, i))
                i += 2
                start = i
            elif c == '|':
                spans.append((start, i))
                i += 1
                start = i
            else:
                i += 1
        else:
            i += 1
    spans.append((start, n))
    return [(s, e) for s, e in spans if cmd[s:e].strip()]


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


# awk option letters that consume a value (glued or separate): -F sep,
# -v var=val, -f file, -i ext, -e text, -E file, -l ext, -W compat.
# NOTE: gawk's -o is deliberately absent — modern gawk treats it as
# argless, so consuming a following word here would misalign operands.
_AWK_VALUE_OPTS = frozenset('FvfieElW')

# awk options whose presence means the PROGRAM came from an option, so
# every operand is a VAR=val assignment or a file (never program text).
_AWK_PROGRAM_OPTS = frozenset('feE')


def _awk_inplace_operands(args):
    """File operands of an awk argv that loads the ``inplace`` extension.

    ``args`` are the words after the awk command word (naive whitespace
    split, mirroring the sed -i block's trade-off: a quoted multi-token
    program leaves junk tokens in the operand list — the safe
    over-inclusion direction, since a junk target only widens the diff
    filter).  Returns a list of file targets when ``-i inplace`` /
    ``--include[=] inplace`` is present, else None.  Under-inclusion is
    not possible: without ``-e``/``-f``/``-E`` the FIRST operand is always
    the program text, and real file operands can never precede it.
    """
    inplace = False
    program_via_option = False
    operands = []
    i, n = 0, len(args)
    while i < n:
        word = args[i]
        if word == '--':
            operands.extend(args[i + 1:])
            break
        if word.startswith('--'):
            name, eq, val = word[2:].partition('=')
            if name in ('include', 'load', 'assign', 'field-separator',
                        'file', 'exec', 'source'):
                if not eq:
                    i += 1
                    val = args[i] if i < n else ''
                if name == 'include' and 'inplace' in val.split('/')[-1]:
                    inplace = True
                if name in ('file', 'exec', 'source'):
                    program_via_option = True
            i += 1
            continue
        if word.startswith('-') and word != '-':
            letter, val = word[1:2], word[2:]
            if letter in _AWK_VALUE_OPTS:
                if not val:
                    i += 1
                    val = args[i] if i < n else ''
                if letter == 'i' and 'inplace' in val.split('/')[-1]:
                    inplace = True
                if letter in _AWK_PROGRAM_OPTS:
                    program_via_option = True
            i += 1
            continue
        operands.append(word)
        i += 1
    if not inplace:
        return None
    if operands and not program_via_option:
        operands = operands[1:]  # the first operand is the program text
    # VAR=val operands are assignments, not files.  A real file whose name
    # contains '=' must be addressed as ./name (awk's own rule too), which
    # does not match this pattern.
    return [w for w in operands if not _re.match(r'^\w+=', w)]


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

    # ── awk -i inplace targets ──
    # gawk/goawk in-place editing:  awk -i inplace 'prog' file1 file2 ...
    # Mirrors the sed -i block above, with operand parsing factored into
    # _awk_inplace_operands.
    if _AWK_INPLACE.search(cmd):
        for seg in _split_pipeline(cmd):
            seg = seg.strip()
            if not seg:
                continue
            parts = seg.split()
            base_cmd = parts[0].split('/')[-1] if parts else ''
            if not base_cmd.endswith('awk'):
                continue
            files = _awk_inplace_operands(parts[1:])
            if files:
                targets.update(files)

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

    # gawk/goawk -i inplace → destructive for the same reason as sed -i
    if _AWK_INPLACE.search(cmd):
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

# Privilege wrappers that can precede the real command word. A delete behind
# one (``sudo rm -rf /``) must be judged by the ``rm``, not by the ``sudo`` —
# otherwise a one-word prefix smuggles a catastrophic delete past every
# argument-level guard. (The retired substring guard ``\brm\s+-rf\s+/``
# caught this shape precisely because it never parsed; when the regex was
# removed in favour of this parser, seeing through sudo/doas is what keeps
# coverage neutral-or-better.)
_PRIV_WRAPPERS = frozenset({'sudo', 'doas'})
# Wrapper flags that consume the NEXT token as their argument
# (``sudo -u root rm …``) — skip them together or the argument is mistaken
# for the command word.
_PRIV_WRAPPER_ARG_FLAGS = frozenset(
    {'-u', '-g', '-h', '-p', '-C', '-U', '-r', '-t'})


def _unwrap_command_parts(parts):
    """Strip a leading privilege wrapper (``sudo``/``doas`` + its flags).

    Returns the token list of the wrapped command, or *parts* unchanged when
    no wrapper is present. Only ONE wrapper level is unwrapped; anything more
    exotic (``xargs rm``, ``find -exec rm``, ``timeout 5 rm``) is out of
    scope for this best-effort net — the same blind spots the retired
    substring guard also had.
    """
    if not parts or parts[0].split('/')[-1] not in _PRIV_WRAPPERS:
        return parts
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok in _PRIV_WRAPPER_ARG_FLAGS:
            i += 2
            continue
        if tok.startswith('-'):
            i += 1
            continue
        return parts[i:]
    return []


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

    A leading privilege wrapper (``sudo rm -rf /``) is seen through via
    :func:`_unwrap_command_parts` before the delete-command check.

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
    except Exception as e:
        logger.debug('[RunCommand] workspace containment resolve failed: %s', e)
        ws_real = None
    for seg in _split_pipeline(command):
        seg = seg.strip()
        if not seg:
            continue
        while _re.match(r'^\w+=\S*\s', seg):
            seg = _re.sub(r'^\w+=\S*\s+', '', seg, count=1)
        parts = _unwrap_command_parts(seg.split())
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
            # Rule 2: restricted callers may only delete inside the workspace.
            if ws_real:
                tgt_real = os.path.realpath(expanded)
                if not (tgt_real == ws_real
                        or tgt_real.startswith(ws_real + os.sep)):
                    return arg
    return None


# ── Unbounded recursive-scan guard ( B2) ─────────────────
# Measured incident (2026-07-31, task 96c56840): the model ended a long
# verification script with ``grep -rn "mcp>=1.0.0" ../ --include=pyproject.toml``
# — a recursive scan of the workspace PARENT, a FUSE mount holding 8GB+ of
# dumps, a PG data dir, 7392 swarm dirs and every sibling repo. The pipeline
# ran 2.5h with zero output; the task wedged; no timeout existed to end it.
#
# The rule (owner ruling 2026-07-31): a recursive scanner whose target is
#   (a) an ANCESTOR of the workspace cwd (scanning it includes the whole
#       workspace + its data trees), or
#   (b) a shallow FUSE-mount path (≤3 components under /mnt),
# is refused when the run_command call carries NO explicit timeout and the
# scan segment is not itself wrapped in coreutils ``timeout``. In-workspace
# scans (``.``, subdirs), sibling dirs (``../other-repo`` — not an ancestor,
# not a mount root), deep specific paths, and bounded scans all stay legal.

# grep-family flags that consume the NEXT token as their argument (so the
# pattern/target positionals are not miscounted).
_GREP_ARG_FLAGS = frozenset({
    '-e', '-f', '-m', '-A', '-B', '-C',
    '--max-count', '--after-context', '--before-context', '--context',
    '--include', '--exclude', '--exclude-dir', '--binary-files',
    '--directories', '--label', '--threads', '--regexp', '--file',
})

# Scanners that recurse only behind a flag.
_FLAGGED_RECURSIVE = frozenset({'grep', 'egrep', 'fgrep'})
# Scanners recursive-by-nature over a target dir (locate excluded: it reads a
# prebuilt index, it does not crawl).
_INHERENT_RECURSIVE = frozenset({
    'find', 'rg', 'ag', 'ack', 'fd', 'fdfind', 'tree', 'du', 'ncdu', 'cloc',
})

# Commands that remain useful in run_command pipelines but can wedge forever
# on a hostile FUSE/NFS traversal. grep has its stronger transparent engine;
# direct find segments get a native streaming timeout wrapper.
_BOUNDED_SCAN_BINARIES = frozenset({'find'})
_SCAN_PREFIX_KEYWORDS = frozenset({'if', 'while', 'until', '!', 'time'})
_SCAN_WRAPPERS = frozenset({'command', 'builtin', 'exec'})
_ENV_ASSIGNMENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

_TIMEOUT_WRAPPER_RE = re.compile(r'^timeout\b')


def _strip_quotes(tok):
    if len(tok) >= 2 and tok[0] in '"\'' and tok[-1] == tok[0]:
        return tok[1:-1]
    return tok


def _scan_targets_for_segment(parts, recursive_flagged):
    """Extract scan-target candidates from one command segment's tokens.

    Returns (is_scanner, targets): targets are the positional args that may
    be scan roots (pattern positional already dropped for grep-family when
    no -e/-f is present). Best-effort parsing — a missed exotic shape fails
    OPEN (allowed), the guard only refuses shapes it can prove.
    """
    positionals = []
    saw_pattern_flag = False
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok.startswith('--'):
            name = tok.split('=', 1)[0]
            if name in _GREP_ARG_FLAGS and '=' not in tok:
                if name in ('--regexp', '--file'):
                    saw_pattern_flag = True
                i += 2
                continue
            i += 1
            continue
        if tok.startswith('-') and tok != '-':
            if tok in _GREP_ARG_FLAGS:
                if tok in ('-e', '-f'):
                    saw_pattern_flag = True
                i += 2
                continue
            i += 1
            continue
        positionals.append(tok)
        i += 1
    if recursive_flagged and not saw_pattern_flag and positionals:
        # First positional is the PATTERN, not a target.
        positionals = positionals[1:]
    return True, positionals


def _scan_binary_word_index(words):
    """Return the command-word index after benign shell prefixes."""
    i = 0
    while i < len(words):
        word = words[i]
        if (not word.squote and not word.dquote
                and (_ENV_ASSIGNMENT_RE.match(word.text)
                     or word.text in _SCAN_PREFIX_KEYWORDS)):
            i += 1
            continue
        break
    if i < len(words) and words[i].text.split('/')[-1] in _SCAN_WRAPPERS:
        i += 1
        while i < len(words) and words[i].text.startswith('-'):
            i += 1
    return i


def _bound_scan_segments(command, timeout_binary, seconds, timeout_args=None):
    """Wrap direct recursive scanner segments with a native timeout.

    The rewrite is span-based and leaves the rest of the shell program
    untouched, so pipelines remain streaming and downstream backpressure
    still works. Returns ``(rewritten, count)``. Unknown/wrapped shapes fail
    open; the ancestor/FUSE-root refusal remains the outer safety boundary.
    """
    try:
        seconds = max(1, int(seconds))
    except (TypeError, ValueError) as exc:
        logger.debug('[CommandAnalysis] invalid scanner timeout %r: %s',
                     seconds, exc)
        return command, 0
    if not command or not timeout_binary:
        return command, 0
    if timeout_args is None:
        timeout_args = ('--signal=TERM', '--kill-after=2s')
    quoted_prefix = ' '.join([
        shlex.quote(str(timeout_binary)),
        *(shlex.quote(str(arg)) for arg in timeout_args),
        shlex.quote(f'{seconds}s'),
    ])
    insertions = []
    for start, end in _split_pipeline_spans(command):
        segment = command[start:end]
        words = _shell_words(segment)
        index = _scan_binary_word_index(words)
        if index >= len(words):
            continue
        base = words[index].text.split('/')[-1]
        if base not in _BOUNDED_SCAN_BINARIES:
            continue
        insertion_at = start + words[index].start
        timeout_prefix = f'{quoted_prefix} '
        insertions.append((insertion_at, timeout_prefix))
    rewritten = command
    for at, text in sorted(insertions, reverse=True):
        rewritten = rewritten[:at] + text + rewritten[at:]
    return rewritten, len(insertions)


def _unbounded_recursive_scan_target(command, cwd=None):
    """Return the offending target when *command* is an unbounded recursive
    scan of a workspace ancestor or a FUSE-mount root — else None.

    "Unbounded" is judged by the CALLER (this function only answers "is this
    target pathological"); a scan wrapped in coreutils ``timeout`` is
    reported safe here because the segment is self-bounding.
    """
    if not command or not command.strip():
        return None
    cwd_real = os.path.realpath(cwd) if cwd else None
    for seg in _split_pipeline(command):
        seg = seg.strip()
        if not seg:
            continue
        while _re.match(r'^\w+=\S*\s', seg):
            seg = _re.sub(r'^\w+=\S*\s+', '', seg, count=1)
        parts = _unwrap_command_parts([w.text for w in _shell_words(seg)])
        if not parts:
            continue
        base_cmd = parts[0].split('/')[-1]
        if _TIMEOUT_WRAPPER_RE.match(base_cmd):
            # coreutils timeout wraps this segment's scan — self-bounding.
            continue
        if base_cmd in _FLAGGED_RECURSIVE:
            # Recursive only behind -r/-R/--recursive (clusters included).
            flags = [t for t in parts[1:] if t.startswith('-') and t != '-']
            recursive = any(
                f == '--recursive'
                or (not f.startswith('--') and ('r' in f[1:] or 'R' in f[1:]))
                for f in flags)
            if not recursive:
                continue
            _, targets = _scan_targets_for_segment(parts, recursive_flagged=True)
        elif base_cmd in _INHERENT_RECURSIVE:
            _, targets = _scan_targets_for_segment(parts, recursive_flagged=False)
        else:
            continue
        if not targets:
            # No target → the tool scans its cwd (inside the workspace).
            continue
        for raw in targets:
            tok = _strip_quotes(raw)
            if not tok or tok == '-':
                continue
            if not (tok.startswith('/') or tok.startswith('..')
                    or tok.startswith('~') or tok.startswith('.')
                    or '/' in tok):
                # A bare word ('src') — relative, inside the workspace.
                continue
            expanded = os.path.expanduser(os.path.expandvars(tok))
            if not expanded:
                continue
            if not expanded.startswith('/'):
                if not cwd_real:
                    continue
                resolved = os.path.realpath(os.path.join(cwd_real, expanded))
            else:
                resolved = os.path.realpath(expanded)
            if cwd_real:
                if resolved == cwd_real or resolved.startswith(cwd_real + os.sep):
                    continue  # inside the workspace — the normal case
                if (cwd_real + os.sep).startswith(resolved + os.sep) \
                        or cwd_real == resolved:
                    return raw  # ancestor of the workspace — the incident shape
            depth = len([c for c in resolved.split('/') if c])
            if (resolved == '/mnt' or resolved.startswith('/mnt/')) \
                    and depth <= 3:
                return raw  # FUSE mount root zone
    return None


# ── Filesystem-grep redirect guard (2026-08-04) ─────────────────────
# The run_command tool description has advised grep_search-over-grep for
# months, yet grep-via-run_command kept recurring — measured 2026-08-04:
# ``grep -n 'cream\|…' static/styles.css | head -30; echo ---; grep -rn
# 'cream' static/*.css 2>/dev/null | head -20`` sat RUNNING for 17m04s in a
# FUSE bad window with zero output and no timeout to end it. Advice is not
# enforcement; this guard is the enforcement.
#
# The rule: a grep-family segment (grep / egrep / fgrep, sudo seen through)
# that reads the FILESYSTEM — explicit file/dir operands, or a recursive
# flag (an implicit walk of the cwd) — is refused with a message that
# translates the call to grep_search. A grep that filters a STREAM (no
# operands, stdin fed by a pipe: ``pytest 2>&1 | grep PASS``, ``ps aux |
# grep python``) stays legal — grep_search cannot replace it. ``rg`` and
# ``git grep`` are already the fast path and stay legal; a coreutils
# ``timeout`` wrapper is not unwrapped (bounded scans stay legal, matching
# the scan guard's philosophy above). Subshells, command substitution and
# multi-line scripts are seen THROUGH (the splitter treats bare parens and
# newlines as command boundaries). Best-effort: exotic shapes (heredocs,
# xargs) fail OPEN, consistent with every other guard in this module.
# Kill switch: TOFU_RUN_GREP_GUARD=0.

_GREP_REDIRECT_BINARIES = frozenset({'grep', 'egrep', 'fgrep'})

# Short grep flags that consume an argument (rest-of-cluster, or the next
# token when last in the cluster): -e -f -m -A -B -C -d -D.
_GREP_SHORT_ARG_CHARS = frozenset('efmABCdD')

# Shell redirections: fused (``2>/dev/null``, ``>out``, ``2>&1``, ``<in``)
# vs bare operator whose target is the NEXT token (``> out``, ``2> err``).
_REDIR_FUSED_RE = re.compile(r'^\d*(?:>>?|<|&>|>&)\S')
_REDIR_BARE_RE = re.compile(r'^\d*(?:>>?|<|&>|>&)$')


def _strip_redirection_tokens(tokens):
    """Drop shell redirection tokens (operator + its target) from *tokens*.

    Without this, ``ps aux | grep -v x 2>/dev/null`` would look like it has
    a file operand (``2>/dev/null``) and be misrefused — it is a pure stream
    filter and must stay legal.
    """
    out = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _REDIR_BARE_RE.match(tok):
            i += 2  # bare operator consumes the next token as its target
            continue
        if _REDIR_FUSED_RE.match(tok):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _grep_segment_reads_filesystem(parts):
    """True when a grep-family segment reads files: explicit file/dir
    operands, or a recursive walk (``-r``/``-R``, target optional — grep
    then scans the cwd). *parts* may contain strings or :class:`_ShellWord`
    values; argv boundaries are authoritative. A grep with only a pattern
    (stdin filter) returns False.
    """
    parts = [part.text if isinstance(part, _ShellWord) else part
             for part in parts]
    positionals = []
    saw_pattern_flag = False
    recursive = False
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok == '--':
            positionals.extend(parts[i + 1:])
            break
        if tok.startswith('--'):
            name = tok.split('=', 1)[0]
            if name in ('--recursive', '--dereference-recursive'):
                recursive = True
            elif name in ('--regexp', '--file'):
                saw_pattern_flag = True
            if name in _GREP_ARG_FLAGS and '=' not in tok:
                i += 2      # long flag consumes the next token as its arg
                continue
            i += 1
            continue
        if tok.startswith('-') and tok != '-':
            cluster = tok[1:]
            takes_next = False
            for j, ch in enumerate(cluster):
                if ch in 'rR':
                    recursive = True
                elif ch in _GREP_SHORT_ARG_CHARS:
                    if ch in 'ef':
                        saw_pattern_flag = True
                    # Rest of the cluster is this flag's argument; when the
                    # flag is the last char, the NEXT token is the argument.
                    takes_next = (j == len(cluster) - 1)
                    break
            i += 2 if takes_next else 1
            continue
        positionals.append(tok)
        i += 1
    if recursive:
        return True
    if not saw_pattern_flag and positionals:
        positionals = positionals[1:]   # first positional is the PATTERN
    return any(t != '-' for t in positionals)   # '-' alone means stdin


def _grep_filesystem_segment(command):
    """Return the offending segment when *command* aims a grep-family binary
    at the FILESYSTEM (file/dir operands or a recursive walk) — else None.

    See the block comment above for the rule. Stream filters, ``rg`` /
    ``git grep`` and ``timeout``-wrapped shapes all return None.
    """
    if not command or not command.strip():
        return None
    for seg in _split_pipeline(command):
        seg = seg.strip()
        if not seg:
            continue
        while _re.match(r'^\w+=\S*\s', seg):
            seg = _re.sub(r'^\w+=\S*\s+', '', seg, count=1)
        parts = _unwrap_command_parts([w.text for w in _shell_words(seg)])
        if not parts:
            continue
        if any(t.startswith('<<') for t in parts):
            # Heredoc / herestring — operand-looking tokens are DATA, not
            # paths; fail open rather than misparse a stream filter.
            continue
        if parts[0].split('/')[-1] not in _GREP_REDIRECT_BINARIES:
            continue
        stripped = [parts[0]] + _strip_redirection_tokens(parts[1:])
        if _grep_segment_reads_filesystem(stripped):
            return seg
    return None


# ── grep-hardening shell-metachar detection ─────────────────────────
# ── grep-hardening shell-metachar detection ─────────────────────────
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
