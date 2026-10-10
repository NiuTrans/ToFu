"""Translate a GNU grep argv to an equivalent ripgrep argv.

Purpose: let ``run_command`` splice ``rg`` directly in place of a filesystem
``grep`` — no helper process, no Python startup — while preserving the native
stdout/stderr/exit-code contract for every construct it accepts. Anything
outside the proven-equivalent surface returns ``None`` and the caller keeps
GNU grep (with the same injected descent guards the old helper used).

Equivalence boundary (documented, deliberately accepted):

  * ripgrep honors ``.gitignore``/ignore rules and skips nothing else — this
    is the same traversal contract ``grep_search`` already sells as the 5x
    FUSE fast path, and the reason the translation is worth doing.
  * ``-c`` is NOT translated: ripgrep 14+ dropped ``--with-zero-count``, so
    zero-match counts would diverge from GNU (``0`` printed, exit 1).
  * ripgrep's default binary policy reports match-after-NUL as a
    ``binary file matches (found "\0" ...)`` notice for EXPLICIT file
    operands, which is never byte-equivalent to GNU (silent ``-I``, or
    GNU's own ``Binary file X matches`` text); explicit binary operands
    therefore stay on GNU unless ``-a``/``--text`` was given (both print
    raw lines). INSIDE a recursive walk rg silently skips binary files —
    GNU ``-I`` semantics — so GNU's ``Binary file X matches`` notice lines
    have no counterpart (and an only-binary match reports exit 1 instead
    of GNU's notice-with-exit-0). Both walk deviations match what
    ``grep_search`` has always surfaced for the same trees.
  * GNU BRE word anchors ``\\<``/``\\>`` and backreferences ``\\1``..``\\9``
    have no Rust-regex equivalent → GNU fallback.
  * Error MESSAGE text differs (``rg:`` vs ``grep:``, os-error suffixes);
    exit codes (0 match / 1 none / 2 error) are identical.

This module is standard-library only: ``grep_engine`` (script-mode helper)
and ``grep_redirect`` both import it without dragging application boot state
into a spawned helper.
"""

from __future__ import annotations

import getopt
import os
import shutil

# ── rg binary resolution (cached; PATH changes within a process are not a
#    supported runtime mutation) ─────────────────────────────────────────
_RG_BIN_CACHE: dict[str, object] = {}


def rg_executable():
    """Absolute rg path, or None. Cached per process; honours test overrides."""
    override = os.environ.get('TOFU_RG_EXECUTABLE', '').strip()
    if override:
        return override if os.path.isfile(override) else None
    if 'bin' not in _RG_BIN_CACHE:
        _RG_BIN_CACHE['bin'] = shutil.which('rg')
    return _RG_BIN_CACHE['bin']


# ── Pattern translation ────────────────────────────────────────────────
# Rust-regex allows escaping any ASCII punctuation; unknown ASCII-letter
# escapes are errors there but literals in GNU, so they fall back.
_RG_SAFE_ALPHA_ESCAPES = frozenset('bBwWsS')
# GNU BRE/ERE treats \n \t \r \f \v as the bare LETTER (no escape semantics);
# Rust regex treats them as control characters → must fall back.
_GNU_LETTER_ESCAPES = frozenset('ntrfv')
_ASCII_PUNCT = frozenset(
    '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~ ')


def _translate_bracket(pattern, i):
    """Copy a ``[...]`` expression verbatim. Returns next index or None."""
    n = len(pattern)
    j = i + 1
    if j < n and pattern[j] == '^':
        j += 1
    if j < n and pattern[j] == ']':
        j += 1
    while j < n and pattern[j] != ']':
        # POSIX collation/equivalence classes have no Rust-regex equivalent.
        if pattern[j] == '[' and j + 1 < n and pattern[j + 1] in '.=':
            return None
        j += 1
    if j >= n:
        return None
    return j + 1


def translate_bre_pattern(pattern):
    """GNU BRE → Rust-regex. Returns the translated pattern, or None.

    BRE facts that make this non-trivial (each pinned by tests):
      - unescaped ``+ ? | ( ) { }`` are LITERALS in BRE, metachars in rg;
      - ``\\( \\) \\{ \\}`` are the BRE metachar forms; ``\\+ \\? \\|`` are
        GNU extensions matching rg's bare forms;
      - ``^`` anchors only at the start (of the pattern / ``\\(`` / ``\\|``),
        elsewhere it is a literal caret; ``$`` anchors only at the end;
      - a leading ``*`` (or one right after ``\\(``/``\\|``) is a literal;
      - ``\\1``..``\\9`` backrefs and ``\\<``/``\\>`` word anchors are
        unsupported → fallback; ``\\'``/``\\``` map to ``\\z``/``\\A``.
    """
    out = []
    i = 0
    n = len(pattern)
    group_open = True          # start of pattern / \( / \| just seen
    while i < n:
        c = pattern[i]
        nxt_open = False
        if c == '[':
            end = _translate_bracket(pattern, i)
            if end is None:
                return None
            out.append(pattern[i:end])
            i = end
            group_open = False
            continue
        if c == '\\':
            if i + 1 >= n:
                return None    # lone trailing backslash — GNU errors too
            d = pattern[i + 1]
            if d in '123456789' or d in '<>':
                return None
            if d == "'":
                out.append('\\z')
            elif d == '`':
                out.append('\\A')
            elif d in '(){}+?|':
                out.append(d)
                nxt_open = d in '(|'
            elif d in _RG_SAFE_ALPHA_ESCAPES:
                out.append('\\' + d)
            elif d in _GNU_LETTER_ESCAPES:
                return None
            elif d in _ASCII_PUNCT:
                out.append('\\' + d)
            else:
                return None    # unknown alpha escape: GNU literal ≠ rg error
            i += 2
            group_open = nxt_open
            continue
        if c in '+?|(){}':
            out.append('\\' + c)
        elif c == '^':
            out.append('^' if group_open else '\\^')
        elif c == '$':
            if i == n - 1:
                out.append('$')
            elif pattern[i + 1] == '\\' and i + 2 < n \
                    and pattern[i + 2] in ')|':
                out.append('$')
            else:
                out.append('\\$')
        elif c == '*':
            out.append('\\*' if group_open else '*')
        else:
            out.append(c)
        i += 1
        group_open = False
    return ''.join(out)


def translate_ere_pattern(pattern):
    """GNU ERE → Rust-regex. Mostly identical; only GNU-only escapes differ."""
    out = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == '[':
            end = _translate_bracket(pattern, i)
            if end is None:
                return None
            out.append(pattern[i:end])
            i = end
            continue
        if c == '\\':
            if i + 1 >= n:
                return None
            d = pattern[i + 1]
            if d in '123456789' or d in '<>':
                return None
            if d == "'":
                out.append('\\z')
            elif d == '`':
                out.append('\\A')
            elif d in _RG_SAFE_ALPHA_ESCAPES:
                out.append('\\' + d)
            elif d in _GNU_LETTER_ESCAPES:
                return None
            elif d in _ASCII_PUNCT:
                out.append('\\' + d)
            else:
                return None
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


# ── Operand probing ────────────────────────────────────────────────────

def _default_probe(path):
    """Classify one operand: dir / text file / binary file / missing / other."""
    try:
        if os.path.isdir(path):
            return 'dir'
        if not os.path.isfile(path):
            return 'missing' if not os.path.lexists(path) else 'other'
        with open(path, 'rb') as fh:
            chunk = fh.read(4096)
        return 'binary' if b'\0' in chunk else 'text'
    except OSError:
        return 'other'


# ── argv translation ───────────────────────────────────────────────────
_GREP_SHORT_OPTS = 'EFGivwnxHhclLoqrsRIayUue:f:m:A:B:C:d:D:TPzbZ'
_GREP_LONG_OPTS = [
    'extended-regexp', 'basic-regexp', 'fixed-strings', 'ignore-case',
    'invert-match', 'word-regexp', 'line-regexp', 'line-number',
    'with-filename', 'no-filename', 'count', 'files-with-matches',
    'files-without-match', 'only-matching', 'quiet', 'silent',
    'recursive', 'dereference-recursive', 'no-messages', 'text',
    'regexp=', 'file=', 'max-count=', 'after-context=',
    'before-context=', 'context=', 'include=', 'exclude=',
    'exclude-dir=', 'directories=', 'binary-files=', 'color=',
    'colour=', 'line-buffered', 'unix-byte-offsets', 'null',
    'null-data', 'byte-offset', 'initial-tab', 'perl-regexp',
    'label=', 'exclude-from=', 'group-separator=', 'no-group-separator',
    'devices=', 'binary', 'mmap', 'no-mmap', 'help', 'version',
    'ignore-file=',
]

# Flags that make an explicit binary policy visible in output shape; when
# present, untranslated binary-notice differences cannot surprise anyone.
_BINARY_POLICY_SHORT = frozenset('aI')


def translate_grep_to_rg(program, argv, cwd, safe_excludes, *,
                         probe=None, max_probed_files=64):
    """Return an rg argv (binary included) equivalent to the GNU grep call.

    ``program`` is the invoked grep word (grep/egrep/fgrep, any path form);
    ``argv`` the words after it with shell redirections already removed by
    the caller. ``cwd`` resolves relative operands. ``safe_excludes`` are
    directory basenames excluded on both paths (the IGNORE_DIRS contract).
    Returns None when any construct falls outside proven equivalence.
    """
    if os.environ.get('TOFU_GREP_EXECUTABLE') or \
            os.environ.get('TOFU_GREP_FORCE_GNU') == '1':
        return None
    rg = rg_executable()
    if rg is None:
        return None
    probe = probe or _default_probe

    try:
        options, positionals = getopt.gnu_getopt(
            list(argv), _GREP_SHORT_OPTS, _GREP_LONG_OPTS)
    except getopt.GetoptError:
        return None

    invoked = os.path.basename(program)
    mode = 'fixed' if invoked == 'fgrep' else (
        'ere' if invoked == 'egrep' else 'bre')
    patterns = []
    pattern_files = []
    includes = []
    excludes = []
    exclude_dirs = list(safe_excludes)
    recursive = False
    follow = False
    rg_flags = []
    binary_text_mode = False
    for opt, value in options:
        if opt in ('-E', '--extended-regexp'):
            mode = 'ere'
        elif opt in ('-G', '--basic-regexp'):
            mode = 'bre'
        elif opt in ('-F', '--fixed-strings'):
            mode = 'fixed'
        elif opt in ('-e', '--regexp'):
            patterns.append(value)
        elif opt in ('-f', '--file'):
            pattern_files.append(value)
        elif opt in ('-c', '--count'):
            return None            # rg ≥14 has no --with-zero-count
        elif opt in ('-r', '--recursive'):
            recursive = True
        elif opt in ('-R', '--dereference-recursive'):
            recursive = True
            follow = True
        elif opt in ('-i', '-y', '--ignore-case'):
            rg_flags.append('-i')
        elif opt in ('-v', '--invert-match'):
            rg_flags.append('-v')
        elif opt in ('-w', '--word-regexp'):
            rg_flags.append('-w')
        elif opt in ('-x', '--line-regexp'):
            rg_flags.append('-x')
        elif opt in ('-n', '--line-number'):
            rg_flags.append('-n')
        elif opt in ('-H', '--with-filename'):
            rg_flags.append('--with-filename')
        elif opt in ('-h', '--no-filename'):
            rg_flags.append('--no-filename')
        elif opt in ('-l', '--files-with-matches'):
            rg_flags.append('--files-with-matches')
        elif opt in ('-L', '--files-without-match'):
            rg_flags.append('--files-without-match')
        elif opt in ('-o', '--only-matching'):
            rg_flags.append('--only-matching')
        elif opt in ('-q', '--quiet', '--silent'):
            rg_flags.append('--quiet')
        elif opt in ('-s', '--no-messages'):
            rg_flags.append('--no-messages')
        elif opt in ('-a', '--text'):
            rg_flags.append('--text')
            binary_text_mode = True
        elif opt == '-I':
            pass                   # silent-skip is GNU-only; see operand gate
        elif opt in ('-U', '-u', '--unix-byte-offsets'):
            pass                   # no-ops / obsolete on both sides
        elif opt == '--line-buffered':
            rg_flags.append('--line-buffered')
        elif opt in ('-m', '--max-count'):
            rg_flags.extend(('--max-count', value))
        elif opt in ('-A', '--after-context'):
            rg_flags.extend(('--after-context', value))
        elif opt in ('-B', '--before-context'):
            rg_flags.extend(('--before-context', value))
        elif opt in ('-C', '--context'):
            rg_flags.extend(('--context', value))
        elif opt == '--include':
            if '/' in value:
                return None        # gitignore anchoring ≠ GNU path fnmatch
            includes.append(value)
        elif opt == '--exclude':
            if '/' in value:
                return None
            excludes.append(value)
        elif opt == '--exclude-dir':
            exclude_dirs.append(value)
        elif opt in ('-d', '--directories'):
            if value != 'recurse':
                return None        # read/skip policies differ under rg
            recursive = True
        elif opt in ('-D', '--devices', '--label', '--exclude-from',
                     '--group-separator', '--no-group-separator',
                     '-T', '--initial-tab', '-P', '--perl-regexp',
                     '-z', '--null-data', '--ignore-file'):
            return None
        elif opt == '--binary-files':
            if value == 'text':
                rg_flags.append('--text')
                binary_text_mode = True
            elif value == 'without-match':
                pass
            else:
                return None
        elif opt in ('--binary',):
            pass                   # rg's default IS --binary
        elif opt in ('--mmap', '--no-mmap'):
            pass                   # performance hints, no output effect
        elif opt in ('-Z', '--null'):
            rg_flags.append('--null')
        elif opt in ('-b', '--byte-offset'):
            rg_flags.append('--byte-offset')
        elif opt in ('--color', '--colour'):
            if value in ('auto', 'never'):
                pass               # captured pipe: auto == never on both
            else:
                return None        # ANSI sequences differ between engines
        elif opt in ('--help', '--version'):
            return None
        else:
            return None

    if patterns:
        operands = positionals
    elif pattern_files:
        # rg -f reads one pattern per line like GNU, but the file's pattern
        # flavor must survive translation: only fixed-strings is provably
        # identical without reading and rewriting the file.
        if mode != 'fixed':
            return None
        operands = positionals
    elif positionals:
        patterns = [positionals[0]]
        operands = positionals[1:]
    else:
        return None                # stdin filter — caller never sends these

    # Pattern flavor translation.
    translated_patterns = []
    if mode == 'fixed':
        translated_patterns = list(patterns)
    else:
        translate = translate_ere_pattern if mode == 'ere' \
            else translate_bre_pattern
        for pat in patterns:
            rendered = translate(pat)
            if rendered is None:
                return None
            translated_patterns.append(rendered)

    # Operand shape: every operand must exist and be classifiable. Directory
    # operands without a recursive flag are a GNU error ("Is a directory")
    # but a search under rg → keep GNU for that observable contract.
    if not operands and not recursive:
        return None
    probed_files = 0
    saw_dir = False
    for operand in operands:
        if operand == '-':
            return None            # stdin mixing — stream filter territory
        resolved = (operand if os.path.isabs(operand)
                    else os.path.join(cwd, operand))
        kind = probe(resolved)
        if kind == 'missing' or kind == 'other':
            return None            # GNU diagnostics / fifos stay native
        if kind == 'dir':
            if not recursive:
                return None        # "Is a directory" is a GNU-only contract
            saw_dir = True
            continue
        probed_files += 1
        if probed_files > max_probed_files:
            return None            # probe cost bound — huge argv stays GNU
        if kind == 'binary':
            if binary_text_mode:
                continue           # -a/--text: both engines print raw lines
            # rg reports match-after-NUL as a "binary file matches" notice
            # where GNU -I stays silent (exit 1) and plain GNU prints its
            # own notice text — neither is spliceable byte-exactly.
            return None

    command = [rg, '--no-heading', '--color=never']
    if len(operands) > 1 and not saw_dir:
        # ripgrep's default parallel walker emits multi-file results out of
        # argv order; an explicit file list carries user-intended order, so
        # pin it. Recursive directory walks keep full parallelism (their
        # order is filesystem-arbitrary under GNU too).
        command.append('--threads=1')
    command.extend(rg_flags)
    if follow:
        command.append('--follow')
    if mode == 'fixed':
        command.append('--fixed-strings')
    for glob in includes:
        command.extend(('--glob', glob))
    for glob in excludes:
        command.extend(('--glob', '!' + glob))
    for dirname in exclude_dirs:
        command.extend(('--glob', f'!{dirname}/'))
    for path in pattern_files:
        command.extend(('--file', path))
    for pattern in translated_patterns:
        command.extend(('--regexp', pattern))
    if operands:
        command.append('--')
        command.extend(operands)
    return tuple(command)


__all__ = [
    'rg_executable',
    'translate_bre_pattern',
    'translate_ere_pattern',
    'translate_grep_to_rg',
]
