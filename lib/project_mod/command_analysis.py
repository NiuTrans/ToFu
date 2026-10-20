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
# contract asserted by tests/project_mod/test_command_analysis_extraction.py. Because it
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


def _independent_runtime_command(command):
    """Plain runtime commands need no checkout snapshot or writer exclusion.

    This is scheduling policy, not proof that an interpreter cannot write.
    Explicit write targets are checked separately before applying this policy.
    Complex shell grammar and embedded filter/Git programs keep the existing
    conservative lease path; ordinary test/build jobs run on live files.
    """
    if (re.search(r'[$`]', command)
            or re.search(r'[\r\n#<>(){}]', _mask_quoted_literals(command))):
        return False
    independent = {
        'pytest', 'py.test', 'python', 'python3', 'npm', 'npx', 'pnpm', 'yarn',
        'make', 'cmake', 'ninja', 'cargo', 'rustc', 'node', 'sleep',
        'echo', 'printf', 'true', 'false', 'pwd',
    }
    segments = _split_pipeline(command)
    for segment in segments:
        try:
            words = shlex.split(segment, comments=False, posix=True)
        except ValueError:
            return False
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words = words[1:]
        if not words:
            return False
        executable = os.path.basename(words[0])
        if executable not in independent and not re.fullmatch(r'python3\.[0-9]+', executable):
            return False
    return bool(segments)


def is_literal_sed_filter(arguments):
    """Prove only one bounded slash-delimited substitution has no write effect.

    No script files, extra programs, in-place options, escapes, addresses or
    execute/write flags qualify. Unsupported sed grammar remains opaque.
    """
    index = 0
    while index < len(arguments) and arguments[index] in {
            '-n', '-E', '-r', '--quiet', '--silent', '--posix'}:
        index += 1
    if index < len(arguments) and arguments[index] == '--':
        index += 1
    if index == len(arguments):
        return False
    program = arguments[index]
    if len(program) > 4096 or not re.fullmatch(r's/[^/\\\r\n]*/[^/\\\r\n]*/[gpIM0-9]*', program):
        return False
    return all(not operand.startswith('-') for operand in arguments[index + 1:])


def command_segment_formatter_writes(source):
    """Return (writes, literal targets, complete, owned) for Ruff, or None.

    This proves maintained argv shapes, not permission. Unknown options,
    wrappers and expansions retain writer admission without owned evidence.
    """
    try:
        words = list(command_segment_words(source))
    except ValueError:
        return None
    while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
        words.pop(0)
    unwrapped = _unwrap_command_parts(words)
    if not unwrapped or os.path.basename(unwrapped[0]) != 'ruff':
        return None
    if len(unwrapped) < 2 or unwrapped[1] not in {'format', 'check'}:
        return True, (), False, False
    verb, args = unwrapped[1], unwrapped[2:]
    flags = {'--no-cache', '--isolated', '--force-exclude', '-q', '--quiet',
             '-v', '--verbose', '--respect-gitignore', '--no-respect-gitignore'}
    flags |= ({'--check', '--diff'} if verb == 'format' else
              {'--fix', '--no-fix', '--fix-only', '--unsafe-fixes', '--no-unsafe-fixes', '--diff'})
    targets = _static_writer_operands(args, flags)
    complete = (words == unwrapped and targets is not None
                and not shell_source_has_expansion(source)
                and not _has_unquoted_shell_metachars(source))
    if not complete:
        return True, (), False, False
    options = args[:args.index('--')] if '--' in args else args
    if verb == 'format':
        writes = not any(arg in {'--check', '--diff'} for arg in options)
        owned = writes
    else:
        # fix=true in discovered Ruff configuration can make a plain check
        # write. Only explicit no-fix/report mode proves source-read-only.
        writes = '--no-fix' not in options and '--diff' not in options
        owned = writes and any(arg in {'--fix', '--fix-only'} for arg in options)
    return writes, tuple(targets), bool(targets), owned


def _command_checkout_mutation(command, cwd=''):
    """Best-effort checkout mutation signal for undo/snapshot analysis.

    Resolved write targets, in-place utilities and mutating Git count as known
    mutations. Opaque interpreters/test/build commands do not prove mutation,
    but False is not a readonly or stable-checkout guarantee. Selective writer
    admission belongs to run_command.command_write_lease_paths, whose literal
    parser also covers uncertain shell/utility shapes; it must not use this
    snapshot classifier as its sole exemption. Opaque commands receive no
    automatic reader lease. Stable cooperative reads are an explicit API.
    """
    if not command or not command.strip():
        return False
    if _extract_write_targets(command, cwd):
        return True
    if _SED_INPLACE.search(command) or _AWK_INPLACE.search(command):
        return True
    for segment in _split_pipeline(command):
        segment = segment.strip()
        if not segment:
            continue
        words = [word.text for word in _shell_words(segment)]
        index = 0
        while index < len(words) and _ENV_ASSIGNMENT_GUARD_RE.match(words[index]):
            index += 1
        words = _unwrap_command_parts(words[index:])
        if not words:
            continue
        base = os.path.basename(words[0])
        if base == 'git':
            if _classify_git_invocation(words[1:]) in ('safe_write', 'tree_destroying'):
                return True
            continue  # readonly or unknown-but-non-mutating Git
        formatter = command_segment_formatter_writes(segment)
        if formatter is not None:
            if formatter[0]:
                return True
            continue
        if base == 'sort':
            parsed = command_segment_targets(segment)
            if parsed.targets or not parsed.complete:
                return True
        if base in _READONLY_COMMANDS:
            continue
        # Opaque non-Git command (interpreter, build/test runner, …): not a
        # proven checkout mutation; no automatic reader lease is inferred.
    return False


def _lease_free_git_segment(command):
    """Return a hardened, fully parsed readonly Git command, else ``None``.

    Writer admission must NOT use the best-effort undo/snapshot classifier
    below: e.g. sort -o, find -delete, and sed's w program all write. Unknown
    commands, options, wrappers and shell grammar require a lease. This small
    literal grammar preserves supported Git inspections, not every
    command that might happen to be readonly. Reject expansions even inside
    quotes rather than interpreting shell syntax with a token-only parser.

    Git can run config-driven helpers or refresh its index even for these
    verbs. Disable those effects as part of the exemption. The canonical
    form is itself accepted so nested outer/worker guards remain lease-free.
    Like the lease itself, this assumes cooperating executables, not a sandbox
    against a substituted git binary or hostile inherited environment.
    """
    if (not command or shell_source_has_expansion(command)
            or re.search(r'[\r\n#;&|<>(){}*?\[\]\\]', _mask_quoted_literals(command))):
        return None
    try:
        words = list(command_segment_words(command))
    except ValueError:
        return None
    if not words or words[0] not in {'git', '/usr/bin/git', '/bin/git'}:
        return None

    safe_config = {
        'core.fsmonitor=false', 'log.showSignature=false', 'format.pretty=medium',
    }
    directory_args = []
    index = 1
    while index < len(words):
        word = words[index]
        if word in {'--no-pager', '--no-optional-locks'}:
            index += 1
        elif word == '-c' and index + 1 < len(words) and words[index + 1] in safe_config:
            index += 2
        elif word == '-C' and index + 1 < len(words):
            directory_args.extend(words[index:index + 2])
            index += 2
        else:
            break
    if index >= len(words):
        return None
    verb = words[index]
    args = words[index + 1:]
    original_args = list(args)
    if _classify_git_invocation(words[1:], require_known_options=True) != 'readonly':
        return None

    if verb == 'clean':
        # Reuse the SAME value/bundle parser: proven noninteractive preview.
        # Native clean dry-run has no write hook; disable pager/fsmonitor.
        if _classify_git_clean(args) != 'readonly':
            return None
        return shlex.join([words[0], '--no-pager', '--no-optional-locks',
                           '-c', 'core.fsmonitor=false', '-c', 'log.showSignature=false',
                           '-c', 'format.pretty=medium', *directory_args, verb, *args])
    flags = {
        'status': {'-s', '--short', '-b', '--branch', '-z', '--null', '--porcelain',
                   '--porcelain=1', '--porcelain=2', '--porcelain=v1', '--porcelain=v2',
                   '--long', '--show-stash', '--ahead-behind', '--no-ahead-behind',
                   '-u', '-uno', '-unormal', '-uall', '--untracked-files=no',
                   '--untracked-files=normal', '--untracked-files=all'},
        'branch': {'--show-current', '--list', '-a', '--all', '-r', '--remotes', '-v', '-vv'},
        'tag': {'--list', '-l', '-n'},
        'worktree': {'list', '--porcelain', '-z'},

        'check-ignore': {'-q', '--quiet', '-v', '--verbose', '-n', '--non-matching',
                         '-z', '--stdin', '--no-index'},
        'remote': {'-v', '--verbose'},
        'rev-parse': {'--show-toplevel', '--git-dir', '--git-common-dir', '--show-prefix',
                      '--is-inside-work-tree', '--is-bare-repository', '--verify', '--short',
                      '--abbrev-ref', '--symbolic', '--symbolic-full-name', '--git-path'},
        'ls-files': {'-z', '-s', '--stage', '-c', '--cached', '-d', '--deleted', '-m',
                     '--modified', '-o', '--others', '-i', '--ignored', '--exclude-standard',
                     '--eol', '--full-name', '--error-unmatch'},
        'ls-tree': {'-r', '-t', '-d', '-l', '-z', '--long', '--name-only', '--name-status',
                    '--full-name', '--full-tree'},
        'merge-base': {'-a', '--all', '--is-ancestor', '--octopus', '--independent', '--fork-point'},
        'cat-file': {'-p', '-t', '-s', '-e', '--batch', '--batch-check', '--batch-all-objects', '--unordered'},
        'for-each-ref': set(),
        'config': {'--get', '--get-all', '--get-regexp', '--get-urlmatch', '--list', '-l',
                   '--show-origin', '--show-scope', '--local', '--global', '--system',
                   '--includes', '--null', '-z', 'get', 'list'},
        'describe': {'--always', '--tags', '--all', '--long', '--dirty', '--broken'},
        'name-rev': {'--name-only', '--tags', '--all', '--no-undefined', '--always'},
        'blame': {'--no-textconv', '-n', '-s', '-p', '--porcelain', '--line-porcelain'},
        'shortlog': {'-n', '-s', '-e', '--numbered', '--summary', '--email', '--all'},
    }
    diff_flags = {'--stat', '--numstat', '--shortstat', '--name-only', '--name-status',
                  '--check', '--raw', '--summary', '--patch', '-p', '--no-patch', '-s',
                  '--no-ext-diff', '--no-textconv', '--no-color', '--color=never',
                  '--color=auto', '--color=always', '--exit-code', '--quiet',
                  '--cached', '--staged', '--no-index', '--ignore-space-change',
                  '--ignore-all-space', '--ignore-space-at-eol', '--binary', '--full-index'}
    log_flags = {'--oneline', '--graph', '--all', '--decorate', '--decorate=short',
                 '--decorate=full', '--decorate=auto', '--decorate=no', '--no-decorate',
                 '--no-show-signature', '--reverse', '--date-order', '--topo-order',
                 '--first-parent', '--no-merges', '--merges'} | diff_flags
    flags.update({'log': log_flags, 'show': log_flags, 'diff': diff_flags,
                  'rev-list': {'--count', '--left-right', '--boundary', '--objects'} | log_flags,
                  'reflog': log_flags, 'stash': log_flags})
    values = {
        'log': {'-n', '--max-count', '--format', '--pretty', '--date', '--since', '--until', '--author', '--grep'},
        'show': {'--format', '--pretty', '--date'},
        'diff': {'--diff-filter', '--relative'},
        'ls-files': {'--exclude', '--exclude-from'},
        'for-each-ref': {'--format', '--sort', '--count', '--points-at', '--contains', '--merged'},
        'branch': {'--format', '--sort', '--contains', '--merged', '--no-merged', '--points-at'},
        'tag': {'--format', '--sort', '--contains', '--merged', '--no-merged', '--points-at'},
        'describe': {'--abbrev', '--match', '--exclude', '--dirty', '--broken'},
        'blame': {'-L', '--contents'},
        'shortlog': {'--format', '--group'},
    }
    values['rev-list'] = values['log']
    values['reflog'] = values['log']
    values['stash'] = values['log']
    if verb not in flags:
        return None
    if verb == 'worktree' and args[:1] != ['list']:
        return None
    if verb == 'reflog' and args[:1] == ['show']:
        args = args[1:]
    if verb == 'stash':
        if args[:1] not in (['list'], ['show']):
            return None
        args = args[1:]
    index = 0
    while index < len(args):
        word = args[index]
        if word == '--':
            break  # Remaining tokens are literal pathspecs, not options.
        option = word.split('=', 1)[0]
        if word in flags[verb] or not word.startswith('-'):
            index += 1
        elif option in values.get(verb, set()):
            if '=' in word:
                value = word.split('=', 1)[1]
                index += 1
            elif index + 1 < len(args):
                value = args[index + 1]
                index += 2
            else:
                return None
            if option in {'--format', '--pretty'}:
                if '%G' in value or re.search(r'%\(\*?signature(?::|\))', value):
                    return None  # Verification atoms can run signature helpers.
        elif verb in {'log', 'show', 'rev-list', 'reflog', 'stash'} and re.fullmatch(
                r'(?:-[0-9]+|-n[0-9]+|-U[0-9]+)', word):
            index += 1
        elif verb == 'diff' and re.fullmatch(r'-U[0-9]+', word):
            index += 1
        else:
            return None  # Unknown/helper/output flags retain writer admission.
    # Keep parameter-sensitive listing commands from borrowing operand support
    # to create refs; classification above owns this distinction.
    hardening = ([flag for flag in ('--no-ext-diff', '--no-textconv') if flag not in original_args]
                 if verb in {'log', 'show', 'diff', 'reflog', 'stash', 'blame'} else [])
    if verb == 'blame':
        hardening = [flag for flag in hardening if flag != '--no-ext-diff']
    if verb == 'stash' or (verb == 'reflog' and original_args[:1] == ['show']):
        hardened_args = original_args[:1] + hardening + original_args[1:]
    else:
        hardened_args = hardening + original_args
    configuration = ([] if verb == 'config' else [
        '-c', 'core.fsmonitor=false', '-c', 'log.showSignature=false',
        '-c', 'format.pretty=medium'])
    return shlex.join([
        words[0], '--no-pager', '--no-optional-locks',
        *configuration, *directory_args, verb, *hardened_args,
    ])


def _lease_free_git_command(command):
    """Harden every Git inspection in a literal chain, preserving separators.

    Literal opaque Python/runtime consumers keep their existing lease-free
    scheduling policy; this grants no read authorization to embedded code.
    Writes, wrappers, redirections and unknown Git shapes never borrow the
    exemption from a neighbouring inspection.
    """
    if not command or shell_source_has_expansion(command):
        return None
    masked = _mask_quoted_literals(command)
    if re.search(r'[\r\n#<>(){}*?\[\]~\\]', masked):
        return None
    spans = _split_pipeline_spans(command)
    if not spans or command[:spans[0][0]].strip() or command[spans[-1][1]:].strip():
        return None
    replacements = []
    saw_git = False
    for index, (start, end) in enumerate(spans):
        if index and command[spans[index - 1][1]:start].strip() not in {';', '&&', '||', '|'}:
            return None
        segment = command[start:end].strip()
        try:
            words = list(command_segment_words(segment))
        except ValueError:
            return None
        if not words:
            return None
        if os.path.basename(words[0]) == 'git':
            hardened = _lease_free_git_segment(segment)
            if hardened is None:
                return None
            replacements.append((start, end, hardened))
            saw_git = True
        elif not (_independent_runtime_command(segment)
                  or (words[0] in {'head', 'tail', 'wc', 'cat', 'cut', 'tr'}
                      and not _command_checkout_mutation(segment))):
            return None
    if not saw_git:
        return None
    for start, end, hardened in reversed(replacements):
        command = command[:start] + ' ' + hardened + ' ' + command[end:]
    return command.strip()


def harden_git_inspections(command):
    """Harden literal read segments even when another segment needs a lease.

    Authorization remains bound to the original tool arguments. Recorded
    background wrappers never use this transformation: their typed binding
    must preserve exact bytes. Rewriting changes no shell separators/cwd.
    """
    if re.search(r'[(){}#]', _mask_quoted_literals(command)):
        return command  # Unknown shell grouping/comments retain ordinary admission.
    replacements = []
    for start, end in _split_pipeline_spans(command):
        hardened = _lease_free_git_segment(command[start:end].strip())
        if hardened is not None:
            replacements.append((start, end, hardened))
    for start, end, hardened in reversed(replacements):
        command = command[:start] + ' ' + hardened + ' ' + command[end:]
    return command.strip() if replacements else command


# Best-effort undo/snapshot fast path, NOT proof of read-only execution.
# Several utilities have write-capable flags or embedded programs. Lease
# admission must use the separate fail-closed parser above, never this set.
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
    'ls-files', 'ls-tree', 'ls-remote', 'merge-base', 'cat-file', 'for-each-ref', 'name-rev',
    'check-ignore',
    'blame', 'shortlog', 'reflog',
    'config', 'stash',  # Parameter-sensitive forms are checked before this set.
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


def _shell_words(s, *, strict=False, redirects=False):
    """Split one shell segment into unquoted words with source offsets.

    Guards and rewriters share this parser so quoted patterns such as
    ``grep "not found"`` cannot be interpreted differently by a fallback
    path. Shell separators are handled by :func:`_split_pipeline_spans`;
    callers pass one resulting segment here.
    """
    out = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        if redirects and s[i] in '<>':
            while i < n and s[i] in '<>':
                i += 1
            out.append(_ShellWord(s[start:i], start, i, False, False))
            continue
        quote = None
        squote = dquote = False
        while i < n:
            char = s[i]
            if quote is None and (char.isspace() or (redirects and char in '<>')):
                break
            if char == '\\' and quote != "'":
                if quote != '"' or (i + 1 < n and s[i + 1] in '$`"\\\n'):
                    i += 2
                    continue
            if char in "'\"":
                if quote is None:
                    quote = char
                    squote = squote or char == "'"
                    dquote = dquote or char == '"'
                elif quote == char:
                    quote = None
            i += 1
        raw = s[start:i]
        try:
            decoded = shlex.split(raw, comments=False, posix=True)
            if len(decoded) != 1:
                raise ValueError('Invalid shell word')
            text = decoded[0]
        except ValueError:
            if strict:
                raise
            text = raw  # Best-effort legacy analysis; admission uses strict mode.
        out.append(_ShellWord(text, start, min(i, n), squote, dquote))
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


def _shell_expansion_prefix(source, *, pathname=True):
    """Decode the literal prefix before a real shell expansion, if any."""
    quote = None
    prefix = []
    index = 0
    while index < len(source):
        char = source[index]
        if char == '\\' and quote != "'" and index + 1 < len(source):
            following = source[index + 1]
            if quote != '"' or following in '$`"\\\n':
                prefix.append(following)
                index += 2
                continue
        if char in "'\"":
            if quote is None:
                quote = char
                index += 1
                continue
            if quote == char:
                quote = None
                index += 1
                continue
        # Tilde in HEAD~1 is literal, unlike a token/assignment prefix ~.
        # Keep assignment/colon cases conservative across supported shells.
        tilde_prefix = char == '~' and (index == 0 or source[index - 1] in ' \t\r\n=:')
        if (quote != "'" and char in '$`'
                or pathname and quote is None and (char in '*?[]{}' or tilde_prefix)):
            return ''.join(prefix), True
        prefix.append(char)
        index += 1
    return ''.join(prefix), quote is not None


def shell_source_has_expansion(source, *, pathname=True):
    """Detect expansion, not literal dollars/globs inside single quotes."""
    return _shell_expansion_prefix(source, pathname=pathname)[1]


def command_segment_words(source):
    """One shell-token authority for utility targets and admission.

    Redirection operators are separate tokens even without whitespace. Quoted
    operands remain one token; no command text or embedded program is executed.
    """
    return tuple(word.text for word in _shell_words(source, strict=True, redirects=True))


_CommandTargets = namedtuple('_CommandTargets', 'words targets complete')


def _static_writer_operands(args, flags):
    """Known non-value flags only; never guess unknown option arity."""
    operands = []
    options = True
    for word in args:
        if options and word == '--':
            if operands:
                return None
            options = False
        elif options and word.startswith('-'):
            if operands or word not in flags:
                return None
        else:
            operands.append(word)
    return operands


def _single_writer_option(args, short, long, flags):
    """Parse one value option plus literal operands; duplicates stay opaque."""
    value = None
    operands = []
    index = 0
    while index < len(args):
        word = args[index]
        if operands and word.startswith('-'):
            return None  # option permutation can depend on POSIXLY_CORRECT
        if word == '--':
            operands.extend(args[index + 1:])
            break
        if word == short or word == long:
            index += 1
            if value is not None or index >= len(args):
                return None
            value = args[index]
        elif word.startswith(long + '=') or (word.startswith(short) and len(word) > len(short)):
            if value is not None:
                return None
            value = word.split('=', 1)[1] if word.startswith(long + '=') else word[len(short):]
        elif word.startswith('-'):
            if word not in flags:
                return None
        else:
            operands.append(word)
        index += 1
    return (value, operands) if value is not None else None


def _find_parts(args):
    """One bounded parser for search roots, link policy and the expression."""
    index, follows_links = 0, False
    while index < len(args):
        arg = args[index]
        if arg in {'-H', '-L', '-P'}:
            follows_links = arg == '-L'
            index += 1
        elif re.fullmatch(r'-O[0-3]', arg):
            index += 1
        elif arg == '-D' and index + 1 < len(args):
            index += 2
        elif arg == '--':
            index += 1
            break
        else:
            break
    roots = []
    while index < len(args) and not args[index].startswith('-') and args[index] not in {'!', '('}:
        roots.append(args[index])
        index += 1
    if not roots and index < len(args) and args[index].startswith('-O'):
        return None
    return roots or ['.'], args[index:], follows_links


def _find_write_rows(expression):
    """Identify action boundaries without interpreting predicate data as flags."""
    values = {'-name', '-iname', '-path', '-ipath', '-wholename', '-iwholename',
              '-lname', '-ilname', '-regex', '-iregex', '-type', '-xtype', '-user',
              '-uid', '-group', '-gid', '-perm', '-size', '-links', '-inum',
              '-samefile', '-newer', '-anewer', '-cnewer', '-atime', '-mtime',
              '-ctime', '-amin', '-mmin', '-cmin', '-maxdepth', '-mindepth',
              '-regextype', '-fstype', '-printf', '-used', '-context'}
    rows, index, follows_links = [], 0, False
    while index < len(expression):
        arg = expression[index]
        if arg == '-follow':
            follows_links = True
            index += 1
        elif arg in {'-exec', '-execdir', '-ok', '-okdir'}:
            end = index + 1
            while end < len(expression) and expression[end] not in {';', '+'}:
                end += 1
            rows.append((index, end))
            index = end + 1
        elif arg in {'-fprint', '-fprint0', '-fls', '-fprintf'}:
            rows.append((index, min(len(expression), index + (3 if arg == '-fprintf' else 2))))
            index += 3 if arg == '-fprintf' else 2
        elif arg == '-delete':
            rows.append((index, index + 1))
            index += 1
        else:
            index += 2 if arg in values or re.fullmatch(r'-newer[a-zA-Z]{2}', arg) else 1
    return rows, follows_links


def command_segment_write_scope_refusal(source):
    """Refuse identified writes whose runtime traversal escapes literal scope.

    This is not a ban on opaque runtimes: it fences explicit find mutations
    without scanning a live tree or guessing each execdir working directory.
    """
    words = list(command_segment_words(source))
    while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
        words.pop(0)
    words = _unwrap_command_parts(words)
    if not words or os.path.basename(words[0]) != 'find':
        return None
    parsed = _find_parts(words[1:])
    if parsed is None:
        return None  # Existing uncertain-option admission remains conservative.
    _roots, expression, follows_links = parsed
    rows, expression_follows_links = _find_write_rows(expression)
    if not rows:
        return None
    if follows_links or expression_follows_links:
        return 'workspace-write-scope-unsupported: find -L/-follow mutations may cross checkout symlinks; use concrete targets or non-following traversal'
    for index, end in rows:
        if expression[index] not in {'-execdir', '-okdir'}:
            continue
        nested = command_segment_targets(shlex.join(expression[index + 1:end]))
        if any(not os.path.isabs(target) and '..' in target.split('/') for target in nested.targets):
            return 'workspace-write-scope-unsupported: relative parent traversal in find -execdir/-okdir changes the write checkout; use an absolute destination'
    return None


def command_segment_output_targets(source):
    """Shell output effects independent of the invoked utility's own write set."""
    try:
        tokens = _shell_words(source, strict=True, redirects=True)
    except ValueError:
        return _CommandTargets((), (), False)
    words, redirects = [], []
    complete, index = True, 0
    while index < len(tokens):
        word = tokens[index]
        token = word.text
        raw = source[word.start:word.end]
        if raw == token and token in {'>', '>>', '<', '<<', '<<<', '<>'}:
            if (index and words and words[-1].isdigit()
                    and tokens[index - 1].end == word.start
                    and source[tokens[index - 1].start:word.start].isdigit()):
                words.pop()  # Only an adjacent, unquoted number is an IO descriptor.
            if index + 1 >= len(tokens):
                return _CommandTargets(tuple(words), tuple(redirects), False)
            value_word = tokens[index + 1]
            value = value_word.text
            value_source = source[value_word.start:value_word.end]
            if token in {'>', '>>', '<>'}:
                if value_source.startswith('&'):
                    # Known stdout/stderr duplication creates no named file.
                    # Other inherited descriptors or legacy >&file are opaque.
                    if token != '>' or value_source not in {'&1', '&2', '&-'}:
                        complete = False
                elif value != '/dev/null':
                    redirects.append(value)
                    if shell_source_has_expansion(value_source):
                        complete = False
            index += 2
        elif raw == token and token.startswith(('>', '<')):
            return _CommandTargets(tuple(words), tuple(redirects), False)
        else:
            words.append(token)
            index += 1
    return _CommandTargets(tuple(words), tuple(redirects), complete)


def command_segment_targets(source):
    """Bounded literal utility writes; incomplete parses retain candidate roots.

    `complete` is snapshot precision, not authorization or lease exemption.
    Unknown mutating options retain every literal candidate rather than guessing
    an option's arity. Opaque runtimes remain the caller's scheduling policy.
    """
    outputs = command_segment_output_targets(source)
    words, redirects = list(outputs.words), list(outputs.targets)
    while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
        words.pop(0)
    words = _unwrap_command_parts(words)
    if not words:
        return _CommandTargets((), tuple(redirects), True)
    base, args = os.path.basename(words[0]), words[1:]
    targets = list(redirects)
    complete = outputs.complete and not shell_source_has_expansion(source)

    def uncertain():
        # Long option values may themselves select an external destination.
        candidates = [arg.split('=', 1)[1] if arg.startswith('--') and '=' in arg
                      else arg for arg in args if not arg.startswith('-')
                      or (arg.startswith('--') and '=' in arg)]
        return _CommandTargets(tuple(words), tuple(dict.fromkeys(targets + candidates)), False)

    if base in {'touch', 'tee', 'mkdir'}:
        flags = ({'-a', '-c', '-m', '--no-create'} if base == 'touch' else
                 {'-a', '-i', '--append', '--ignore-interrupts'} if base == 'tee' else
                 {'-p', '--parents', '-v', '--verbose'})
        operands = _static_writer_operands(args, flags)
        if operands is None:
            return uncertain()
        targets.extend(operands)
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base == 'truncate':
        option = _single_writer_option(args, '-s', '--size', {'-c', '--no-create'})
        if option is None:
            return uncertain()
        targets.extend(option[1])
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base == 'sort':
        short_values = set('okStT')
        short_flags = set('bdfgiMhnRrsuVzcmC')
        long_values = {'--output', '--key', '--field-separator', '--buffer-size',
                       '--temporary-directory', '--batch-size', '--parallel',
                       '--compress-program', '--random-source', '--files0-from', '--sort'}
        long_flags = {'--reverse', '--numeric-sort', '--general-numeric-sort',
                      '--human-numeric-sort', '--month-sort', '--version-sort',
                      '--random-sort', '--ignore-leading-blanks', '--dictionary-order',
                      '--ignore-case', '--ignore-nonprinting', '--stable', '--unique',
                      '--zero-terminated', '--merge', '--check', '--help', '--version', '--debug'}
        index = 0
        while index < len(args):
            arg = args[index]
            index += 1
            if arg == '--':
                break
            if arg.startswith('--'):
                option, separator, value = arg.partition('=')
                if option in long_values:
                    if not separator:
                        if index >= len(args):
                            return uncertain()
                        value = args[index]
                        index += 1
                    if option in {'--output', '--temporary-directory'}:
                        targets.append(value)
                    # Helper programs cannot borrow a harmless read exemption.
                    if option in {'--compress-program', '--random-source', '--files0-from'}:
                        return uncertain()
                elif option not in long_flags or separator:
                    return uncertain()
            elif arg.startswith('-') and arg != '-':
                position = 1
                while position < len(arg):
                    flag = arg[position]
                    position += 1
                    if flag in short_values:
                        value = arg[position:]
                        if not value:
                            if index >= len(args):
                                return uncertain()
                            value = args[index]
                            index += 1
                        if flag in {'o', 'T'}:
                            targets.append(value)
                        break
                    if flag not in short_flags:
                        return uncertain()
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base in {'cp', 'mv'}:
        operands, destination = [], None
        index, options = 0, True
        flags = (set('abdfHilLnprRsTuvxP') if base == 'cp' else set('bfinTuv'))
        long_flags = {'--force', '--interactive', '--no-clobber', '--verbose',
                      '--no-target-directory', '--backup', '--update', '--help', '--version'}
        if base == 'cp':
            long_flags |= {'--archive', '--recursive', '--dereference', '--no-dereference',
                           '--preserve', '--parents', '--link', '--symbolic-link',
                           '--one-file-system', '--remove-destination', '--copy-contents'}
        while index < len(args):
            arg = args[index]
            index += 1
            if options and arg == '--':
                options = False
            elif options and arg.startswith('--'):
                option, separator, value = arg.partition('=')
                if option in {'--target-directory', '--suffix'}:
                    if not separator:
                        if index >= len(args):
                            return uncertain()
                        value = args[index]
                        index += 1
                    if option == '--target-directory':
                        destination = value
                elif option not in long_flags and option not in {'--reflink', '--sparse', '--no-preserve'}:
                    return uncertain()
            elif options and arg.startswith('-') and arg != '-':
                position = 1
                while position < len(arg):
                    flag = arg[position]
                    position += 1
                    if flag in {'t', 'S'}:
                        value = arg[position:]
                        if not value:
                            if index >= len(args):
                                return uncertain()
                            value = args[index]
                            index += 1
                        if flag == 't':
                            destination = value
                        break
                    if flag not in flags:
                        return uncertain()
            else:
                operands.append(arg)
        if destination is None:
            if len(operands) < 2:
                return uncertain()
            destination = operands.pop()
        targets.append(destination)
        if base == 'mv':
            targets.extend(operands)
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base == 'sed':
        inplace, program_option, operands = False, False, []
        index, options = 0, True
        while index < len(args):
            arg = args[index]
            index += 1
            if options and arg == '--':
                options = False
            elif options and arg in {'-e', '-f', '--expression', '--file'}:
                if index >= len(args):
                    return uncertain()
                index += 1
                program_option = True
            elif options and (arg.startswith(('--expression=', '--file='))
                              or (arg.startswith(('-e', '-f')) and len(arg) > 2)):
                program_option = True
            elif options and (arg.startswith('--in-place')
                              or (arg.startswith('-') and not arg.startswith('--') and 'i' in arg[1:])):
                inplace = True
            elif options and arg.startswith('-'):
                if arg not in {'-n', '-r', '-E', '-s', '-u', '-z', '--quiet', '--silent',
                               '--regexp-extended', '--separate', '--unbuffered', '--null-data'}:
                    return uncertain()
            else:
                operands.append(arg)
        if inplace:
            targets.extend(operands if program_option else operands[1:])
        elif not is_literal_sed_filter(args):
            return _CommandTargets(tuple(words), tuple(targets), False)
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base.endswith('awk'):
        files = _awk_inplace_operands(args)
        if files is not None:
            targets.extend(files)
            return _CommandTargets(tuple(words), tuple(targets), complete)
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base == 'env' and args:
        return _CommandTargets(tuple(words), tuple(targets), False)
    if base == 'find':
        parsed = _find_parts(args)
        if parsed is None:
            return uncertain()  # Unknown optimizer grammar cannot erase peer operands.
        roots, expression, _follows_links = parsed
        rows, _expression_follows_links = _find_write_rows(expression)
        if rows:
            targets.extend(roots)
            for index, end in rows:
                arg = expression[index]
                if arg in {'-fprint', '-fprint0', '-fprintf', '-fls'} and index + 1 < len(expression):
                    targets.append(expression[index + 1])
                if arg in {'-exec', '-execdir', '-ok', '-okdir'}:
                    nested = command_segment_targets(shlex.join(expression[index + 1:end]))
                    targets.extend(path for path in nested.targets if '{}' not in path)
                    if not nested.complete:
                        targets.extend(path for path in expression[index + 2:end]
                                       if not path.startswith('-') and '{}' not in path)
            return _CommandTargets(tuple(words), tuple(dict.fromkeys(targets)), complete)
        return _CommandTargets(tuple(words), tuple(targets), complete)
    mode = _WRITE_TARGET_COMMANDS.get(base)
    if mode == 'all_args':
        targets.extend(arg for arg in args if not arg.startswith('-'))
    elif mode == 'last_arg':
        operands = [arg for arg in args if not arg.startswith('-')]
        targets.extend(operands[-1:])
    elif mode == 'opaque' or base not in _READONLY_COMMANDS:
        return _CommandTargets(tuple(words), tuple(targets), False)
    return _CommandTargets(tuple(words), tuple(targets), complete)


def command_segment_literal_targets(source):
    """Derive literal targets or bounded expansion parents from source words.

    A dynamic source operand must not erase a literal destination in another
    checkout. Single-quoted/escaped dollar signs are filenames, not expansion.
    Fully dynamic names remain covered by the caller's conservative cwd claim;
    this cooperative parser is not a sandbox for arbitrary runtime expansion.
    """
    parsed = command_segment_targets(source)
    try:
        words = _shell_words(source, strict=True, redirects=True)
    except ValueError:
        return ()
    result = set()
    for target in parsed.targets:
        origins = [word for word in words if word.text == target
                   or (word.text.startswith('-') and word.text.endswith(target))]
        if not origins:
            if target == '.':
                result.add(target)  # find's implicit search root
            continue
        if any(not shell_source_has_expansion(source[word.start:word.end]) for word in origins):
            result.add(target)
            continue
        # Use source provenance, not the decoded target's first dollar: a
        # quoted checkout name may itself contain literal expansion markers.
        for word in origins:
            prefix, expanded = _shell_expansion_prefix(source[word.start:word.end])
            if expanded:
                target_prefix = prefix[len(word.text) - len(target):]
                parent = os.path.dirname(target_prefix)
                if parent:
                    result.add(parent)
    return tuple(sorted(result))


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
    (or all files if None) are reported as changed. None does not mean that
    known writes are absent: admission must use command_segment_literal_targets
    to retain explicit destinations even when other effects are opaque.
    """
    if not command or not command.strip():
        return set()

    targets = set()
    for segment in _split_pipeline(command):
        parsed = command_segment_targets(segment)
        if parsed.words and os.path.basename(parsed.words[0]) == 'git':
            if (_classify_git_invocation(list(parsed.words[1:])) != 'readonly'
                    or shell_source_has_expansion(segment)):
                return None
        elif not parsed.complete:
            return None
        targets.update(parsed.targets)
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

    This is a best-effort snapshot heuristic, not a safety/admission proof.
    Common inspection/filter binaries skip tracking even though some options
    or embedded programs can write. Unknown binaries, interpreters and build
    tools are assumed destructive. Writer exclusion uses the separate
    fail-closed lease parser, never this heuristic.
    """
    if not command or not command.strip():
        return False

    cmd = command.strip()

    # Shell expansion can execute writes before any nominally readonly verb.
    # Conservatively reject even quoted expansion markers; token-only parsing
    # is not evidence that a command substitution or variable is harmless.
    if shell_source_has_expansion(cmd, pathname=False):
        return True

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
        try:
            parts = command_segment_words(seg)
        except ValueError:
            return True
        if not parts:
            continue
        base = parts[0].split('/')[-1]  # handle /usr/bin/rm → rm

        # env with arguments may execute a writer (env git stash apply).
        if base == 'env' and len(parts) > 1:
            return True

        # ── Special-case: git ──
        if base == 'git':
            words = [word.text for word in _shell_words(seg)]
            if (_classify_git_invocation(words[1:]) == 'readonly'
                    and not _re.search(r'[$`]', seg)):
                continue
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


# ── Git workspace guard classification ─────────────────────────────
# Contract: docs/runbooks/GIT_WORKSPACE_GUARD.md.
# Pure risk classification of a shell command for the shared-checkout
# Git guard. Returns one of:
#   'readonly'        — every segment is provably read-only
#   'safe_write'      — ordinary writes (including preflighted atomic apply)
#                       that need writer exclusion, not a presence veto
#   'tree_destroying' — a Git segment that may rewrite working-tree bytes or
#                       move HEAD under other conversations
#   None              — no Git segment, an unknown Git sub-command, an
#                       unknown global flag, or an opaque non-Git segment;
#                       callers must fall back to the existing attended gate.
# This module stays PURE: verdicts only. The nonce / presence / refusal
# policy lives in lib/project_mod/git_guard.py.

# Ordinary additive writes; destructive ref/history options are checked first.
# This class is not writer exclusion and never bypasses readonly authorization.
_GIT_GUARD_SAFE_WRITE = frozenset({
    'add', 'commit', 'fetch', 'push',
})

# Short-flag bundles that imply a FORCED branch operation: -D is exactly -df
# and -M is -mf, so a bundled 'f' is equally forceful. For ``tag`` the
# destructive short flags stay d/D/c/C/f (tag has no lowercase force aliases).
_GIT_GUARD_FORCE_SHORT_FLAGS = {
    'branch': 'dDf',
    'tag': 'dDcCf',
}

# Sub-commands that may rewrite working-tree bytes or move HEAD. ``stash``
# is handled separately because list/show are read-only.
_GIT_GUARD_TREE_DESTROYING = frozenset({
    'checkout', 'switch', 'reset', 'restore', 'clean', 'rm', 'mv',
    'rebase', 'merge', 'cherry-pick', 'revert', 'am', 'pull',
})

# git global options (BEFORE the sub-command) that consume the NEXT token
# as their value: git -C <path> -c k=v --git-dir <p> ...
_GIT_GUARD_GLOBAL_VALUE_OPTS = frozenset({
    '-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env',
})

# Bare global flags that are safe to skip while hunting the sub-command.
_GIT_GUARD_GLOBAL_BARE_FLAGS = frozenset({
    '-p', '-P', '--paginate', '--no-pager', '--bare', '--no-replace-objects',
    '--literal-pathspecs', '--glob-pathspecs', '--noglob-pathspecs',
    '--icase-pathspecs', '--no-optional-locks', '--version', '--help',
})

_ENV_ASSIGNMENT_GUARD_RE = _re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def git_command_words(words):
    """Bounded Git wrapper projection; no env option/command interpretation."""
    words = list(words)
    while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
        words.pop(0)
    words = _unwrap_command_parts(words)
    if words and os.path.basename(words[0]) == 'env':
        words = words[1:]
        if words[:1] == ['--']:
            words = words[1:]
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words.pop(0)
        if not words or words[0].startswith('-'):
            return []  # -S/-u/-i and unknown forms never gain Git receipts.
    return words


def git_invocation_parts(args):
    """Stable public syntax-only pair; authority belongs to details parsing."""
    details = _git_invocation_details(args)
    return None if details is None else (details[0], details[1])


def _git_invocation_details(args):
    """Parse argv into (subcommand, rest, unproved_config, autostash) or None.

    One pure owner for validated global-option consumption. Unknown options,
    missing values and invalid config assignments return None; attached -C/-c
    and known long equals forms are supported. Values are argv data, including
    option-looking paths. The returned rest is a fresh list; input is unchanged.
    An empty subcommand denotes bare Git/global help or an exec-path query.
    This is syntax evidence, not shell-expansion or configured-helper authority.
    """
    unproved = False
    autostash = False
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if not isinstance(token, str) or '\x00' in token:
            return None
        if token in _GIT_GUARD_GLOBAL_BARE_FLAGS:
            unproved |= token in {'--paginate', '-p'}
            continue
        if token == '--exec-path':
            return '', [], unproved, autostash  # Query exits without invoking a verb.
        option, value = token, None
        if token in _GIT_GUARD_GLOBAL_VALUE_OPTS:
            if index >= len(args):
                return None
            value = args[index]
            index += 1
        elif token.startswith(('-C', '-c')) and len(token) > 2:
            option, value = token[:2], token[2:]
        elif token.startswith('--') and '=' in token:
            option, value = token.split('=', 1)
            if option not in _GIT_GUARD_GLOBAL_VALUE_OPTS | {'--exec-path'}:
                return None
        elif token.startswith('-') or not token:
            return None
        else:
            return token, list(args[index:]), unproved, autostash
        if not isinstance(value, str) or '\x00' in value:
            return None
        if option in ('-c', '--config-env'):
            key, separator, setting = value.partition('=')
            if not re.fullmatch(
                    r'[A-Za-z][A-Za-z0-9-]*(?:\.[^=\s]+)*\.[A-Za-z][A-Za-z0-9-]*', key):
                return None
            if option == '--config-env' and (not separator or not setting):
                return None

            # Only bounded presentation settings prove absence of helpers.
            harmless = (option == '-c' and separator and (
                (key.lower() in {'color.ui', 'color.diff', 'color.status', 'color.branch'}
                 and setting.lower() in {'false', 'true', 'never', 'auto', 'always'})
                or value in {'core.fsmonitor=false', 'log.showSignature=false',
                             'format.pretty=medium'}))
            unproved |= not harmless
            autostash |= (option == '-c' and key.lower() == 'merge.autostash'
                          and setting.lower() in {'true', 'yes', 'on', '1', ''})
        elif option != '-C' and not value:
            return None
        if option == '--exec-path':
            unproved = True
    return '', [], unproved, autostash


def _classify_checkout_like(sub, args):
    """Classify ``git checkout|switch`` for the workspace guard.

    Branch creation (``checkout -b name`` / ``switch -c name``) moves HEAD to
    a new ref pointing at the SAME commit and rewrites no working-tree byte
    → ``safe_write``. Anything else — switching refs, ``--detach``, path
    checkouts (``checkout -- f``), or a start-point after the new branch name
    (``checkout -b new main~2``, which rewrites the tree to the start point)
    → ``tree_destroying``.
    """
    # Accept only the narrow additive form. Force/reset/orphan and unknown
    # options can move existing refs or discard index/worktree state.
    create_flags = ({'-b'} if sub == 'checkout' else {'-c', '--create'})
    if len(args) == 2 and args[0] in create_flags and not args[1].startswith('-'):
        return 'safe_write'
    if sub == 'switch' and len(args) == 1 and args[0].startswith('--create='):
        return 'safe_write' if args[0][9:] else 'tree_destroying'
    return 'tree_destroying'


def _commit_rewrites_history(args):
    """Find amend options, not option values or pathspecs.

    Git accepts unambiguous long-option abbreviations. Conservatively flag
    amend prefixes rather than granting an ordinary-commit receipt to them.
    """
    value_options = {
        '-m', '--message', '-F', '--file', '-C', '--reuse-message',
        '-c', '--reedit-message', '--author', '--date', '--cleanup',
        '--fixup', '--squash', '--pathspec-from-file', '--trailer',
    }
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == '--':
            break
        if token in value_options:
            index += 1
            continue
        option = token.split('=', 1)[0]
        if option.startswith('--') and len(option) > 2 and '--amend'.startswith(option):
            return True
    return False


def _classify_git_apply(args, *, require_known_options=False):
    """Parameter-sensitive apply policy; unknown shapes retain approval.

    Default apply preflights the patch and fails without partial application.
    It does not move HEAD or rewrite history, but still needs writer admission.
    Conflict/partial/outside-root modes are not in that ordinary-write class.
    Report/check modes are read-only unless --apply requests application too.
    Parse option values and -- before deciding: a pathname is not an option.
    """
    report_flags = {'--check', '--stat', '--numstat', '--summary'}
    ordinary_flags = {
        '--apply', '--index', '--cached', '--intent-to-add', '-N',
        '--reverse', '-R', '--no-add', '--binary', '--unidiff-zero',
        '--recount', '--inaccurate-eof', '--ignore-space-change',
        '--ignore-whitespace', '--allow-empty', '--verbose', '-v',
        '--quiet', '-q', '-z',
    }
    risky_flags = {'--3way', '-3', '--reject', '--unsafe-paths', '--allow-overlap'}
    value_flags = {'--exclude', '--include', '--directory', '--whitespace', '-p', '-C'}
    report = apply = risky = unknown = False
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == '--':
            break
        if token in report_flags:
            report = True
        elif token in ordinary_flags:
            apply = apply or token == '--apply'
        elif token in risky_flags:
            risky = True
        elif token in value_flags:
            if index >= len(args):
                unknown = True
                continue
            value = args[index]
            index += 1
            if token in ('-p', '-C') and not value.isdigit():
                unknown = True
            elif token == '--whitespace' and value not in (
                    'nowarn', 'warn', 'fix', 'error', 'error-all'):
                unknown = True
        elif any(token.startswith(flag + '=') for flag in value_flags
                 if flag.startswith('--')):
            flag, value = token.split('=', 1)
            if flag == '--whitespace' and value not in (
                    'nowarn', 'warn', 'fix', 'error', 'error-all'):
                unknown = True
        elif token.startswith(('-p', '-C')) and token[2:].isdigit():
            continue
        elif token.startswith('-') and token != '-':
            # Includes long-option abbreviations and --build-fake-ancestor,
            # which writes an index even in otherwise report-only invocations.
            unknown = True
    if require_known_options and unknown:
        return None
    if risky:
        return 'tree_destroying'
    if unknown:
        return None
    return 'readonly' if report and not apply else 'safe_write'


def _git_worktree_add_destination(args):
    """Literal additive worktree shape; never force/reset an existing ref."""
    if not args or args[0] != 'add':
        return None
    rest = args[1:]
    if len(rest) >= 3 and rest[0] == '-b' and not rest[1].startswith('-'):
        rest = rest[2:]
    elif rest and rest[0] == '--detach':
        rest = rest[1:]
    else:
        return None
    if len(rest) not in (1, 2) or any(not x or x.startswith('-') for x in rest):
        return None
    return rest[0]


GitWorktreeLifecycle = namedtuple('GitWorktreeLifecycle', 'operation paths forced')


def git_worktree_lifecycle(args):
    """Parse native remove/move once for classification, targets and review.

    This is syntax evidence only. The target resolver validates relative
    operands against the invoking checkout; force never proves ownership or
    that discarded files, commits and worktree locks are expendable.
    """
    if not args or args[0] not in ('remove', 'move'):
        return None
    operands = []
    options_done = False
    forced = False
    for value in args[1:]:
        if value == '--' and not options_done:
            options_done = True
        elif not options_done and value.startswith('-'):
            if value not in ('-f', '--force'):
                return None
            forced = True
        else:
            operands.append(value)
    required = 1 if args[0] == 'remove' else 2
    if len(operands) != required:
        return None
    if any(not value or value.startswith('~') for value in operands):
        return None
    return GitWorktreeLifecycle(args[0], tuple(operands), forced)


def _git_worktree_lifecycle_targets(args):
    """Classify only literal absolute targets; relative paths need cwd evidence."""
    lifecycle = git_worktree_lifecycle(args)
    if lifecycle and all(os.path.isabs(path) for path in lifecycle.paths):
        return lifecycle.paths
    return None


GitIntegrationEvidence = namedtuple(
    'GitIntegrationEvidence', 'risky_options unknown_options external_options')


def git_integration_option_evidence(subcommand, args):
    """Pure per-verb option evidence from the SAME parser used for risk.

    Tuple fields contain option spellings, never consumed message/key values.
    Recovery/autostash/strategy is risky; unknown or invalid options require
    attended fallback; signing invokes external machinery and is not discard.
    ``--`` ends options. A value looking like --abort is still only data.
    """
    if subcommand not in {'merge', 'cherry-pick', 'revert'}:
        return GitIntegrationEvidence((), ('unsupported integration verb',), ())
    flags = {'--no-commit', '--commit', '-n', '--no-edit', '--edit', '-e',
             '--signoff', '--no-signoff', '--continue', '--no-gpg-sign',
             '--rerere-autoupdate', '--no-rerere-autoupdate'}
    values = {'--cleanup': 'cleanup', '--strategy': 'strategy',
              '--strategy-option': 'strategy', '-X': 'strategy'}
    risky_flags = {'--abort', '--quit', '--skip', '--autostash'}
    if subcommand == 'merge':
        flags |= {'--ff', '--no-ff', '--ff-only', '--squash', '--no-squash',
                  '--quiet', '-q', '--verbose', '-v', '--stat', '--no-stat',
                  '--summary', '--no-summary', '--compact-summary',
                  '--no-compact-summary', '--no-autostash', '--log', '--no-log',
                  '--progress', '--no-progress', '--allow-unrelated-histories',
                  '--overwrite-ignore', '--no-overwrite-ignore',
                  '--verify', '--no-verify'}
        values.update({'-m': 'message', '--message': 'message',
                       '-F': 'file', '--file': 'file', '--into-name': 'name',
                       '-s': 'strategy'})
    else:
        flags |= {'-s'}
        values.update({'-m': 'mainline', '--mainline': 'mainline'})
        if subcommand == 'cherry-pick':
            flags |= {'-x', '--ff', '--no-ff', '--allow-empty', '--no-allow-empty',
                      '--allow-empty-message', '--no-allow-empty-message',
                      '--keep-redundant-commits', '--no-keep-redundant-commits'}
            values['--empty'] = 'empty'
        else:
            flags |= {'--reference', '--no-reference'}
    risky, unknown, external = [], [], []
    if not args:
        unknown.append('missing integration arguments')
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == '--':
            break
        if token in flags or not token.startswith('-'):
            continue
        if token in risky_flags:
            risky.append(token)
            continue
        if (token in {'-S', '--gpg-sign', '--verify-signatures', '--no-verify-signatures'}
                or token.startswith(('-S', '--gpg-sign='))):
            external.append(token.split('=', 1)[0] if token.startswith('--') else '-S')
            continue
        option, value = token, None
        if token in values:
            if index < len(args):
                value = args[index]
                index += 1
        elif token.startswith('--') and '=' in token:
            option, value = token.split('=', 1)
            if option == '--log' and subcommand == 'merge':
                if not value.isdigit():
                    unknown.append(option)
                continue
        elif token[:2] in values and len(token) > 2:
            option, value = token[:2], token[2:]
        kind = values.get(option)
        valid = kind is not None and value is not None
        if kind == 'mainline':
            valid = valid and re.fullmatch(r'[1-9][0-9]*', value) is not None
        elif kind == 'cleanup':
            valid = valid and value in {'strip', 'whitespace', 'verbatim', 'scissors', 'default'}
        elif kind == 'empty':
            valid = valid and value in {'drop', 'keep', 'stop'}
        elif kind in {'strategy', 'file', 'name'}:
            valid = valid and bool(value) and not value.startswith('-')
        if not valid:
            unknown.append(option)
        elif kind == 'strategy':
            risky.append(option)
    return GitIntegrationEvidence(tuple(risky), tuple(unknown), tuple(external))


def _classify_git_integration(subcommand, args, *, require_known_options=False):
    evidence = git_integration_option_evidence(subcommand, args)
    unproved = evidence.unknown_options or evidence.external_options
    if require_known_options and unproved:
        return None
    if evidence.risky_options:
        return 'tree_destroying'
    return None if unproved else 'safe_write'


def _classify_git_clean(args):
    """Only proven non-interactive dry-run syntax earns read authority."""
    preview = False
    unknown = False
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == '--':
            break  # All remaining tokens are pathspec data.
        if token in {'--dry-run', '--no-dry-run'}:
            preview = token == '--dry-run'
        elif token in {'--force', '--no-force', '--quiet', '--no-quiet'}:
            continue
        elif token in {'--interactive', '--no-interactive'}:
            unknown = True
        elif token in {'-e', '--exclude'}:
            if index >= len(args):
                unknown = True
            else:
                index += 1  # Even --dry-run is a pattern, not a preview flag.
        elif token.startswith('--exclude='):
            continue
        elif token.startswith('--'):
            unknown = True
        elif token.startswith('-') and token != '-':
            for offset, flag in enumerate(token[1:], 1):
                if flag == 'e':
                    if offset == len(token) - 1:
                        if index >= len(args):
                            unknown = True
                        else:
                            index += 1
                    break  # Rest of bundle is the exclude value.
                if flag == 'n':
                    preview = True
                elif flag not in 'dfqxX':
                    unknown = True
    if unknown:
        return None
    return 'readonly' if preview else 'tree_destroying'


def _classify_git_invocation(args, *, require_known_options=False):
    details = _git_invocation_details(args)
    if details is None:
        return None
    sub, rest, unproved, autostash = details
    native = _classify_git_native_invocation(
        sub, rest, require_known_options=require_known_options)
    if autostash and sub == 'merge' and not require_known_options:
        return 'tree_destroying'
    if unproved:
        return 'tree_destroying' if native == 'tree_destroying' and not require_known_options else None
    return native


def _classify_git_native_invocation(sub, rest, *, require_known_options=False):
    """Classify one ``git ...`` argv (words AFTER the ``git`` word).

    Returns 'readonly' | 'safe_write' | 'tree_destroying' | None (unknown
    sub-command or global-flag shape → caller falls back to the human gate).
    """
    if not sub:
        return 'readonly'  # Bare Git/global help prints without mutation.

    if sub == 'worktree':
        if rest in (['list'], ['list', '--porcelain'], ['list', '--porcelain', '-z']):
            return 'readonly'
        if _git_worktree_add_destination(rest) is not None:
            return 'safe_write'
        if _git_worktree_lifecycle_targets(rest) is not None:
            return 'tree_destroying'
        return None
    if sub in {'merge', 'cherry-pick', 'revert'}:
        return _classify_git_integration(
            sub, rest, require_known_options=require_known_options)
    if sub == 'clean':
        return _classify_git_clean(rest)
    if sub == 'apply':
        return _classify_git_apply(rest, require_known_options=require_known_options)
    if sub == 'stash':
        if any(tok.startswith(('--output', '--ext-diff', '--textconv')) for tok in rest):
            return None
        # list/show are read-only; every other form (incl. bare `git stash`
        # == push) mutates the working tree or the shared stash stack.
        if rest[:1] not in (['list'], ['show']):
            return 'tree_destroying'
        flags = {'-p', '--patch', '--stat', '--numstat', '--name-only',
                 '--name-status', '--oneline', '--no-patch', '-s', '--raw',
                 '--no-ext-diff', '--no-textconv', '--no-color', '--all',
                 '--include-untracked', '--only-untracked', '-u'}
        values = {'--format', '--pretty', '--date', '--max-count', '-n',
                  '--grep', '--author', '--since', '--until'}
        index = 1
        while index < len(rest):
            token = rest[index]
            index += 1
            if token == '--':
                break
            if token in values:
                if index == len(rest):
                    return None
                value = rest[index]
                index += 1
            elif token.split('=', 1)[0] in values and '=' in token:
                value = token.split('=', 1)[1]
            elif (not token.startswith('-') or token in flags
                  or re.fullmatch(r'-[0-9]+|-n[0-9]+|-U[0-9]+', token)):
                continue
            else:
                return None
            if token.split('=', 1)[0] in {'--format', '--pretty'} and '%G' in value:
                return None
        return 'readonly'
    if sub in {'branch', 'tag', 'push'}:
        # Consume maintained value options before risk scans. Values are data.
        values = ({'--push-option', '-o'} if sub == 'push' else
                  {'--format', '--sort', '--contains', '--no-contains',
                   '--merged', '--no-merged', '--points-at'})
        filtered = []
        index = 0
        while index < len(rest):
            token = rest[index]
            index += 1
            if token == '--':
                filtered.extend(rest[index:])
                break
            if token in values:
                if index == len(rest):
                    return None
                index += 1
                if sub != 'push':
                    filtered.append(token + '=value')
            elif any(token.startswith(option + '=') for option in values if option.startswith('--')):
                if sub != 'push':
                    filtered.append(token.split('=', 1)[0] + '=value')
            else:
                filtered.append(token)
        rest = filtered

        known_flags = ({'--verbose', '--quiet', '--dry-run', '--set-upstream',
                        '--all', '--tags', '--atomic', '--follow-tags', '--porcelain',
                        '--no-verify', '--verify', '--ipv4', '--ipv6', '--force',
                        '--force-with-lease', '--delete', '--mirror'} if sub == 'push' else
                       {'-d', '-D', '--delete', '-m', '-M', '--move', '-c', '-C',
                        '--copy', '-f', '--force', '--edit-description',
                        '--set-upstream-to', '--unset-upstream', '--list', '-l',
                        '-a', '--all', '-r', '--remotes', '-v', '-vv', '--verbose',
                        '--show-current'})
        if require_known_options and any(
                token.startswith('-') and token not in known_flags
                and token.split('=', 1)[0] not in values
                and not (sub == 'push' and re.fullmatch(r'-[vqnu46fd]+|-o.+', token))
                and not (sub != 'push' and re.fullmatch(r'-[avrdDfFmMcC]+', token))
                for token in rest):
            return None
    if sub == 'branch':
        # Plain -d/--delete is self-guarding: Git refuses to delete a branch
        # that is unmerged (against HEAD or its upstream) or checked out in
        # any worktree, so a successful delete can neither orphan commits nor
        # break a sibling's worktree. Only the narrow shape — one delete flag
        # plus plain branch names — earns safe_write; force (-D is -df, so a
        # bundled 'f' too), unknown options, or mixed delete/rename shapes
        # fall through to the conservative destructive-flag scan below.
        delete_flag = next((tok for tok in rest
                            if tok in ('-d', '--delete')), None)
        if (delete_flag is not None
                and rest.count(delete_flag) == 1
                and not any(tok in ('-d', '--delete') and tok != delete_flag
                            for tok in rest)):
            others = [tok for tok in rest if tok != delete_flag]
            if others and all(not tok.startswith('-') for tok in others):
                return 'safe_write'
    if sub in ('branch', 'tag'):
        destructive_flags = {'-d', '-D', '--delete', '-m', '-M', '--move',
                             '-c', '-C', '--copy', '-f', '--force',
                             '--edit-description', '--set-upstream-to',
                             '--unset-upstream'}
        if any(tok.split('=')[0] in destructive_flags
               or (tok.startswith('-') and not tok.startswith('--')
                   and any(flag in tok[1:]
                           for flag in 'Mm' + _GIT_GUARD_FORCE_SHORT_FLAGS[sub]))
               for tok in rest):
            return 'tree_destroying'
        if not rest or all(tok in ('-a', '--all', '-r', '--remotes',
                                  '-v', '-vv', '--verbose', '--show-current')
                           for tok in rest):
            return 'readonly'
        if any(tok in ('--list', '-l', '--contains', '--no-contains',
                       '--merged', '--no-merged', '--points-at', '--format', '--sort')
               or tok.startswith(('--format=', '--sort=')) for tok in rest):
            # Unknown options are not proved read-only.
            allowed = ('--list', '--format', '--sort', '--contains', '--no-contains', '--merged',
                       '--no-merged', '--points-at', '--format=', '--sort=')
            if all(not tok.startswith('-') or tok in ('-l', '-a', '-r', '-v', '-vv')
                   or any(tok == flag or tok.startswith(flag + '=')
                          if not flag.endswith('=') else tok.startswith(flag)
                          for flag in allowed) for tok in rest):
                return 'readonly'
        return 'safe_write' if all(not tok.startswith('-') for tok in rest) else None
    if sub == 'config':
        reads = {'--get', '--get-all', '--get-regexp', '--get-urlmatch',
                 '--list', '-l', '--show-origin', '--show-scope',
                 '--local', '--global', '--system', '--includes', '--null', '-z'}
        if rest and rest[0] in ('get', 'list'):
            return 'readonly' if not any(tok.startswith('--output') for tok in rest) else None
        if any(tok in ('--get', '--get-all', '--get-regexp', '--get-urlmatch',
                       '--list', '-l') for tok in rest):
            return 'readonly' if all(not tok.startswith('-') or tok in reads for tok in rest) else None
        return None  # Ambiguous getter/setter forms keep the existing approval gate.
    if sub == 'remote':
        return 'readonly' if not rest or rest == ['-v'] else None
    if sub == 'reflog':
        return 'readonly' if not rest or rest[0] in ('show', 'exists') else 'tree_destroying'
    if sub in ('update-ref', 'symbolic-ref', 'replace'):
        if sub == 'symbolic-ref' and rest in (['HEAD'], ['-q', 'HEAD'], ['--quiet', 'HEAD']):
            return 'readonly'
        return 'tree_destroying'
    if sub == 'commit' and _commit_rewrites_history(rest):
        return 'tree_destroying'
    # Git accepts bundled short options (-vf, -vd, -pf). Scan only the
    # known bare-option prefix: -o consumes the remaining text as its value,
    # and fetch's -j consumes a job count, so neither may expose fake flags.
    if sub == 'fetch' and any(
            tok.startswith(('--force', '--prune', '--update-head-ok', '+'))
            or _re.match(r'^-[vqamtnk46]*[fpPu]', tok) for tok in rest):
        return 'tree_destroying'
    if sub == 'push' and any(
            tok.startswith(('--force', '--delete', '--mirror', '+', ':'))
            or _re.match(r'^-[vqnu46]*[fd]', tok) for tok in rest):
        return 'tree_destroying'

    if sub == 'push':
        flags = {'--verbose', '--quiet', '--dry-run', '--set-upstream',
                 '--all', '--tags', '--atomic', '--follow-tags', '--porcelain',
                 '--no-verify', '--verify', '--ipv4', '--ipv6'}
        if any(token.startswith('-') and token not in flags
               and not re.fullmatch(r'-[vqnu46]+|-o.+', token) for token in rest):
            return None
    if sub in _GIT_READONLY_SUBCOMMANDS:
        if any(tok.startswith(('--output', '--ext-diff', '--textconv')) for tok in rest):
            return None
        return 'readonly'
    if sub in ('checkout', 'switch'):
        return _classify_checkout_like(sub, rest)
    if sub in _GIT_GUARD_SAFE_WRITE:
        return 'safe_write'
    if sub in _GIT_GUARD_TREE_DESTROYING:
        return 'tree_destroying'
    return None


def _git_guard_classify(command, *, require_known_segments=False):
    """Classify *command* for the shared-checkout Git workspace guard.

    Returns 'readonly' | 'safe_write' | 'tree_destroying' | None. None means
    the guard does not engage (no Git segment, unknown Git shape, or an
    opaque non-Git segment without a recognized high-risk Git mutation) and
    the caller keeps the existing attended-gate behavior. High-risk mutations
    take precedence over unknown segments regardless of ordering. Unknown
    segments take precedence over ordinary writes. Risk classification does
    not authorize opaque segments: receipt callers require
    ``require_known_segments=True`` in addition to the risk verdict. A non-Git
    segment is tolerated for automatic ordinary-write receipts only when it
    is provably read-only (``git add -A && git commit -m wip`` is safe_write;
    ``git add x && rm -rf build`` is None → human gate).

    Quoting/pipelines follow the same parser as the destructive classifier
    (``_split_pipeline`` / ``_shell_words``), so words inside quoted literals
    cannot masquerade as sub-commands. Absolute-path git (``/usr/bin/git``)
    IS seen through via basename matching. Best-effort guard-rail on par
    with ``_is_catastrophic_delete`` — not a security boundary.
    """
    if not command or 'git' not in command:
        return None
    saw_git = False
    saw_safe_write = False
    saw_tree_destroying = False
    saw_unknown = False
    for seg in _split_pipeline(command):
        seg = seg.strip()
        if not seg:
            continue
        words = [w.text for w in _shell_words(seg)]
        i = 0
        while i < len(words) and _ENV_ASSIGNMENT_GUARD_RE.match(words[i]):
            i += 1
        words = git_command_words(words[i:])

        if re.search(r'\bGIT_CONFIG_(?:COUNT|KEY_[0-9]+|VALUE_[0-9]+|PARAMETERS)=', seg):
            saw_unknown = True
        if not words:
            saw_unknown = True
            continue
        # Applies to filter/helper segments too, not only the Git argv.
        if shell_source_has_expansion(seg, pathname=False):
            saw_unknown = True
        base = words[0].split('/')[-1]
        if base != 'git':
            if base in _READONLY_COMMANDS and not _is_destructive_command(seg):
                continue
            saw_unknown = True  # opaque non-git segment → existing gate
            continue
        saw_git = True
        cls = _classify_git_invocation(
            words[1:], require_known_options=require_known_segments)

        if cls == 'readonly' and _REDIRECT_PATTERN.search(
                _REDIRECT_TO_DEV_NULL.sub('', _mask_quoted_literals(seg))):
            saw_unknown = True  # e.g. git status > tracked-file is not read-only
        if cls == 'tree_destroying':
            saw_tree_destroying = True
        elif cls is None:
            saw_unknown = True
        elif cls == 'safe_write':
            saw_safe_write = True
    # Inspect the entire chain: opaque prefixes must not hide a later reset,
    # but opaque suffixes must not borrow an additive Git receipt either.
    if require_known_segments and saw_unknown:
        return None
    if saw_tree_destroying:
        return 'tree_destroying'
    if not saw_git or saw_unknown:
        return None
    return 'safe_write' if saw_safe_write else 'readonly'
