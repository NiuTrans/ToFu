"""grep_redirect — transparent filesystem-grep redirect for run_command.

Owner ruling 2026-08-06: refuse+teach is the LAST resort, not the product.
When a run_command segment aims grep at the FILESYSTEM (the shapes the
2026-08-04 guard used to refuse outright), the grep is instead EXECUTED
in-process by a GNU-faithful engine (BRE/ERE/POSIX-class translation,
IGNORE_DIRS descent pruning identical to the hardening layer, internal
deadline), its stdout/stderr persisted to temp files, and the shell
command is spliced so the rest of the pipeline continues from those
files — the model believes it ran run_command::

    grep -n pat f | wc -l      →  ( cat $T.out ; exit 0 ) | wc -l
    grep -q pat f && echo y    →  ( : ; exit 0 ) && echo y
    grep pat f 2>/dev/null     →  ( cat $T.out ; cat $T.err >&2 ; exit 2 ) 2>/dev/null

Exit-code semantics, filename-prefix rules, context separators, binary
notices and error lines are byte-level GNU-compatible — the ground truth
was probed from GNU grep on 2026-08-06 and is pinned in
tests/test_grep_redirect_engine.py (incl. a differential parity suite
against the host's GNU grep where one exists).

CORRECTNESS CONTRACT — plan-time execution: the redirected grep runs
BEFORE the shell command. That is only valid when no earlier segment of
the same command can change the bytes the grep reads, so a segment is
redirected only when every PRECEDING segment is provably side-effect-free
(read-only command whitelist, no output redirection, literal ``cd`` folded
into the engine cwd). Anything else (``make > log; grep FAIL log``) falls
back to refuse+teach, as do untranslatable shapes (-P, -z, backtick
command substitution in arguments, sudo-wrapped grep, unreadable pattern
files, deadline exceeded on a hostile filesystem window).

Kill switches: TOFU_RUN_GREP_GUARD=0 disables the whole interception
layer; TOFU_GREP_REDIRECT=0 reverts to refuse-always.
"""

from __future__ import annotations

import atexit
import fnmatch
import glob as _glob
import os
import re
import shlex
import tempfile
import threading
import time
from collections import namedtuple
from dataclasses import dataclass, field

from lib.log import get_logger
from lib.project_mod.command_analysis import (
    _GIT_READONLY_SUBCOMMANDS,
    _READONLY_COMMANDS,
    _REDIR_BARE_RE,
    _REDIR_FUSED_RE,
    _REDIRECT_PATTERN,
    _grep_segment_reads_filesystem,
    _mask_quoted_literals,
    _split_pipeline_spans,
)
from lib.project_mod.config import IGNORE_DIRS

logger = get_logger(__name__)

# Internal wall-clock budget for ALL redirected greps of one command. On a
# hostile tree (FUSE bad window) the engine must fail honestly — the caller
# falls back to refuse+teach — rather than recreate the 17m04s wedge the
# guard exists to prevent.
_DEADLINE_S = float(os.environ.get('TOFU_GREP_REDIRECT_DEADLINE_S', '40'))

# GNU's binary heuristic: a NUL in the first 32 KB marks the file binary.
_BINARY_SNIFF_BYTES = 32768

_GREP_BINARIES = frozenset({'grep', 'egrep', 'fgrep'})

# ── Temp-file lifecycle ─────────────────────────────────────────────
# Splice targets are plain files under the system temp dir. They are swept
# (a) at the start of the NEXT plan, (b) at process exit, (c) by the OS tmp
# reaper as a last resort — never by appending `rm` to the model's command
# (the rm→trash shim would intercept it and the write-target tracker would
# record junk paths).
_TEMP_REGISTRY = set()
_TEMP_LOCK = threading.Lock()


def _register_temp(path):
    with _TEMP_LOCK:
        _TEMP_REGISTRY.add(path)


def _sweep_temps():
    with _TEMP_LOCK:
        stale = list(_TEMP_REGISTRY)
        _TEMP_REGISTRY.clear()
    for p in stale:
        try:
            os.unlink(p)
        except OSError as e:
            logger.debug('[grep_redirect] temp sweep of %s failed: %s', p, e)


atexit.register(_sweep_temps)


class _Refusal(Exception):
    """A segment cannot be transparently redirected; carries the reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Deadline(Exception):
    pass


# ═════════════════════════════════════════════════════════════════════
#  Shell word tokenizer (quote/escape aware, offset-preserving)
# ═════════════════════════════════════════════════════════════════════

_Word = namedtuple('_Word', ['text', 'start', 'end', 'squote', 'dquote'])


def _shell_words(s):
    """Split *s* into shell words. Each _Word carries the unquoted value,
    its raw [start,end) offsets in *s*, and whether ANY part was single-
    or double-quoted (the gates for glob/tilde/variable expansion)."""
    out = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in ' \t':
            i += 1
        if i >= n:
            break
        st = i
        buf = []
        sq = dq = False
        while i < n and s[i] not in ' \t':
            c = s[i]
            if c == '\\' and not sq:
                if i + 1 < n:
                    buf.append(s[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            if c == "'" and not dq:
                sq = True
                i += 1
                while i < n and s[i] != "'":
                    buf.append(s[i])
                    i += 1
                i += 1  # closing quote (or unterminated end)
                continue
            if c == '"' and not sq:
                dq = True
                i += 1
                while i < n and s[i] != '"':
                    if s[i] == '\\' and i + 1 < n and s[i + 1] in '$`"\\\n':
                        buf.append(s[i + 1])
                        i += 2
                        continue
                    buf.append(s[i])
                    i += 1
                i += 1
                continue
            buf.append(c)
            i += 1
        out.append(_Word(''.join(buf), st, i, sq, dq))
    return out


# ═════════════════════════════════════════════════════════════════════
#  grep argument parser
# ═════════════════════════════════════════════════════════════════════

@dataclass
class _Spec:
    patterns: list = field(default_factory=list)
    mode: str = 'bre'                 # bre | ere | fixed
    ignore_case: bool = False
    invert: bool = False
    word_regexp: bool = False
    line_regexp: bool = False
    line_number: bool = False
    with_filename: object = None      # True (-H) / False (-h) / None (auto)
    count_only: bool = False
    list_mode: str = ''               # '' | 'with' (-l) | 'without' (-L)
    only_matching: bool = False
    quiet: bool = False
    recursive: bool = False
    no_messages: bool = False         # -s
    binary_mode: str = 'binary'       # binary | without-match (-I) | text (-a)
    max_count: object = None          # -m N (per file)
    before: int = 0
    after: int = 0
    includes: list = field(default_factory=list)
    excludes: list = field(default_factory=list)
    exclude_dirs: list = field(default_factory=list)
    dir_action: str = 'read'          # -d read | skip | recurse
    operands: list = field(default_factory=list)   # list[_Word]
    implicit_dot: bool = False        # bare `grep -r x` — display without ./


# Short flags consuming an argument (cluster-rest or next token).
_ARG_SHORT = frozenset('efmABCdD')
# Evaluated flag-only shorts; -y is legacy -i, -U/-u are no-ops on Linux.
_SUPPORTED_SHORT = frozenset('EFGivwnxhHclLoqrsIayUu')

_LONG_SIMPLE = {
    '--extended-regexp': ('mode', 'ere'), '--basic-regexp': ('mode', 'bre'),
    '--fixed-strings': ('mode', 'fixed'), '--ignore-case': ('ignore_case', True),
    '--invert-match': ('invert', True), '--word-regexp': ('word_regexp', True),
    '--line-regexp': ('line_regexp', True), '--line-number': ('line_number', True),
    '--with-filename': ('with_filename', True), '--no-filename': ('with_filename', False),
    '--count': ('count_only', True), '--files-with-matches': ('list_mode', 'with'),
    '--files-without-match': ('list_mode', 'without'),
    '--only-matching': ('only_matching', True),
    '--quiet': ('quiet', True), '--silent': ('quiet', True),
    '--recursive': ('recursive', True), '--dereference-recursive': ('recursive', True),
    '--no-messages': ('no_messages', True), '--text': ('binary_mode', 'text'),
    '--unix-byte-offsets': (None, None),
}
_LONG_ARG = {
    '--regexp': 'pattern', '--file': 'pattern_file', '--max-count': 'max_count',
    '--after-context': 'after', '--before-context': 'before', '--context': 'context',
    '--include': 'includes', '--exclude': 'excludes', '--exclude-dir': 'exclude_dirs',
    '--exclude-from': 'exclude_from', '--directories': 'dir_action',
    '--binary-files': 'binary_files',
}
_LONG_IGNORE = frozenset({'--color', '--colour', '--line-buffered', '--mmap'})
_LONG_REFUSE = frozenset({
    '--perl-regexp', '--null-data', '--null', '--byte-offset', '--initial-tab',
    '--label', '--help', '--version', '--devices',
})


def _nonneg(ch, arg):
    try:
        v = int(arg)
    except ValueError:
        raise _Refusal(f'invalid -{ch} value {arg!r}') from None
    return max(0, v)


def _read_pattern_file(path, cwd):
    p = path if os.path.isabs(path) else os.path.join(cwd, path)
    try:
        with open(p, encoding='utf-8', errors='replace') as f:
            lines = f.read().split('\n')
    except OSError as e:
        raise _Refusal(
            f'cannot read pattern file {path!r}: {e.strerror or e}') from None
    # GNU -f: one pattern per line; a trailing newline adds no empty pattern.
    if lines and lines[-1] == '':
        lines.pop()
    return lines


def _apply_arg_flag(spec, ch, arg, cwd):
    if ch == 'e':
        spec.patterns.append(arg)
    elif ch == 'f':
        spec.patterns.extend(_read_pattern_file(arg, cwd))
    elif ch == 'm':
        try:
            spec.max_count = max(0, int(arg))
        except ValueError:
            raise _Refusal(f'invalid -m count {arg!r}') from None
    elif ch == 'A':
        spec.after = _nonneg('A', arg)
    elif ch == 'B':
        spec.before = _nonneg('B', arg)
    elif ch == 'C':
        spec.after = spec.before = _nonneg('C', arg)
    elif ch == 'd':
        if arg not in ('read', 'skip', 'recurse'):
            raise _Refusal(f'invalid -d action {arg!r}')
        spec.dir_action = arg
    elif ch == 'D':
        if arg != 'read':
            raise _Refusal(f'unsupported -D action {arg!r}')


def _apply_long_arg(spec, kind, val, cwd):
    if kind == 'pattern':
        spec.patterns.append(val)
    elif kind == 'pattern_file':
        spec.patterns.extend(_read_pattern_file(val, cwd))
    elif kind == 'max_count':
        try:
            spec.max_count = max(0, int(val))
        except ValueError:
            raise _Refusal(f'invalid --max-count {val!r}') from None
    elif kind == 'after':
        spec.after = _nonneg('A', val)
    elif kind == 'before':
        spec.before = _nonneg('B', val)
    elif kind == 'context':
        spec.after = spec.before = _nonneg('C', val)
    elif kind == 'includes':
        spec.includes.append(val)
    elif kind == 'excludes':
        spec.excludes.append(val)
    elif kind == 'exclude_dirs':
        spec.exclude_dirs.append(val)
    elif kind == 'exclude_from':
        spec.excludes.extend(_read_pattern_file(val, cwd))
    elif kind == 'dir_action':
        if val not in ('read', 'skip', 'recurse'):
            raise _Refusal(f'invalid --directories action {val!r}')
        spec.dir_action = val
    elif kind == 'binary_files':
        if val not in ('binary', 'without-match', 'text'):
            raise _Refusal(f'invalid --binary-files type {val!r}')
        spec.binary_mode = val


def _apply_short(spec, ch, argv, i, cl_rest, cwd):
    """Apply one short-flag char. Returns (next_i, cluster_done)."""
    if ch in _ARG_SHORT:
        arg = cl_rest if cl_rest else (argv[i + 1].text if i + 1 < len(argv) else None)
        if arg is None:
            raise _Refusal(f'-{ch} requires an argument')
        consumed_next = not cl_rest
        _apply_arg_flag(spec, ch, arg, cwd)
        return (i + 1 if consumed_next else i), True
    if ch not in _SUPPORTED_SHORT:
        raise _Refusal(f'unsupported grep flag -{ch}')
    if ch == 'E':
        spec.mode = 'ere'
    elif ch == 'F':
        spec.mode = 'fixed'
    elif ch == 'G':
        spec.mode = 'bre'
    elif ch in 'iy':
        spec.ignore_case = True
    elif ch == 'v':
        spec.invert = True
    elif ch == 'w':
        spec.word_regexp = True
    elif ch == 'x':
        spec.line_regexp = True
    elif ch == 'n':
        spec.line_number = True
    elif ch == 'H':
        spec.with_filename = True
    elif ch == 'h':
        spec.with_filename = False
    elif ch == 'c':
        spec.count_only = True
    elif ch == 'l':
        spec.list_mode = 'with'
    elif ch == 'L':
        spec.list_mode = 'without'
    elif ch == 'o':
        spec.only_matching = True
    elif ch == 'q':
        spec.quiet = True
    elif ch == 's':
        spec.no_messages = True
    elif ch == 'r':
        spec.recursive = True
    elif ch == 'I':
        spec.binary_mode = 'without-match'
    elif ch == 'a':
        spec.binary_mode = 'text'
    # -U / -u: no-ops on Linux
    return i, False


def _parse_grep_args(words, cwd):
    """Parse grep argv words (binary at words[0]) into a _Spec.
    Raises _Refusal for anything untranslatable."""
    spec = _Spec()
    if words[0].text.endswith('egrep'):
        spec.mode = 'ere'
    elif words[0].text.endswith('fgrep'):
        spec.mode = 'fixed'
    argv = words[1:]
    positionals = []
    i = 0
    end_of_flags = False
    while i < len(argv):
        w = argv[i]
        t = w.text
        if end_of_flags or not t.startswith('-') or t == '-':
            positionals.append(w)
            i += 1
            continue
        if t == '--':
            end_of_flags = True
            i += 1
            continue
        if t.startswith('--'):
            name, eq, val = t.partition('=')
            if name in _LONG_REFUSE:
                raise _Refusal(f'unsupported grep flag {name}')
            if name in _LONG_IGNORE:
                i += 1
                continue
            if name in _LONG_SIMPLE:
                attr, v = _LONG_SIMPLE[name]
                if attr:
                    setattr(spec, attr, v)
                i += 1
                continue
            if name in _LONG_ARG:
                kind = _LONG_ARG[name]
                if not eq:
                    if i + 1 >= len(argv):
                        raise _Refusal(f'{name} requires an argument')
                    val = argv[i + 1].text
                    i += 1
                _apply_long_arg(spec, kind, val, cwd)
                i += 1
                continue
            raise _Refusal(f'unsupported grep flag {name}')
        cluster = t[1:]
        j = 0
        while j < len(cluster):
            nxt, done = _apply_short(spec, cluster[j], argv, i,
                                     cluster[j + 1:], cwd)
            if done:
                i = nxt
                break
            j += 1
        i += 1
    if spec.patterns:
        spec.operands = positionals            # -e/-f given: all positional
    elif positionals:
        spec.patterns = [positionals[0].text]
        spec.operands = positionals[1:]
    if not spec.patterns:
        raise _Refusal('grep without a pattern')
    if spec.only_matching and spec.invert:
        raise _Refusal('unsupported combination -o with -v')
    if any(w.text == '-' for w in spec.operands):
        raise _Refusal('stdin operand `-` mixed with files')
    if any('`' in w.text and not w.squote for w in spec.operands):
        raise _Refusal('command substitution in grep arguments')
    if spec.dir_action == 'recurse':
        spec.recursive = True
    if not spec.operands and spec.recursive:
        spec.implicit_dot = True
    return spec


# ═════════════════════════════════════════════════════════════════════
#  Pattern translation: BRE / ERE / fixed → Python re
# ═════════════════════════════════════════════════════════════════════

_POSIX_CLASSES = {
    'alnum': 'A-Za-z0-9', 'alpha': 'A-Za-z', 'blank': ' \\t',
    'cntrl': '\\x00-\\x1f\\x7f', 'digit': '0-9', 'graph': '\\x21-\\x7e',
    'lower': 'a-z', 'print': '\\x20-\\x7e', 'punct': '!-/:-@\\[-`{-~',
    'space': ' \\t\\r\\n\\v\\f', 'upper': 'A-Z', 'xdigit': '0-9A-Fa-f',
}
_POSIX_CLASS_RE = re.compile(r'\[:(\w+):\]')

# GNU word constituents for \< \> and -w.
_WORD_L = r'(?<![A-Za-z0-9_])(?=[A-Za-z0-9_])'   # \<
_WORD_R = r'(?<=[A-Za-z0-9_])(?![A-Za-z0-9_])'   # \>


def _posix_expand(bracket):
    def _sub(m):
        name = m.group(1)
        if name not in _POSIX_CLASSES:
            raise _Refusal(f'unknown POSIX class [:{name}:]')
        return _POSIX_CLASSES[name]
    return _POSIX_CLASS_RE.sub(_sub, bracket)


def _scan_bracket(p, i):
    """p[i] == '['; return the index just past the closing ']'."""
    j = i + 1
    n = len(p)
    if j < n and p[j] == '^':
        j += 1
    if j < n and p[j] == ']':
        j += 1
    while j < n:
        if p[j] == '[' and j + 1 < n and p[j + 1] in ':.=':
            k = p.find(p[j + 1] + ']', j + 2)
            if k != -1:
                j = k + 2
                continue
        if p[j] == ']':
            return j + 1
        j += 1
    return n  # unterminated — re.compile errors, surfaced as refusal


def _translate_escaped(d):
    """GNU extension escapes valid in both BRE and ERE."""
    if d == '<':
        return _WORD_L
    if d == '>':
        return _WORD_R
    if d == 'b':
        return r'\b'
    if d in 'wWsS':
        return '\\' + d
    if d == '`':
        return r'\A'
    if d == "'":
        return r'\Z'
    return None


def _ere_to_py(p):
    out = []
    i, n = 0, len(p)
    while i < n:
        c = p[i]
        if c == '\\' and i + 1 < n:
            d = p[i + 1]
            ext = _translate_escaped(d)
            if ext is not None:
                out.append(ext)
            elif d in '()|+?{}':
                out.append('[' + d + ']')   # ERE escaped metachar = literal
            elif d == '\\':
                out.append('\\\\')
            elif d in '.^$*[]-':
                out.append('\\' + d)
            elif d in 'nt':
                out.append('\\' + d)
            elif d.isdigit():
                out.append('\\' + d)         # backref
            else:
                out.append(re.escape(d))
            i += 2
            continue
        if c == '[':
            j = _scan_bracket(p, i)
            out.append(_posix_expand(p[i:j]))
            i = j
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _bre_to_py(p):
    out = []
    i, n = 0, len(p)
    while i < n:
        c = p[i]
        if c == '\\' and i + 1 < n:
            d = p[i + 1]
            ext = _translate_escaped(d)
            if ext is not None:
                out.append(ext)
            elif d in '()|+?':
                out.append(d)                # BRE escaped = SPECIAL
            elif d == '{':
                j = p.find('\\}', i + 2)
                if j != -1 and re.fullmatch(r'\d*(,\d*)?', p[i + 2:j]):
                    out.append('{' + p[i + 2:j] + '}')
                    i = j + 2
                    continue
                out.append('\\{')
            elif d == '}':
                out.append('\\}')
            elif d == '\\':
                out.append('\\\\')
            elif d in '.^$*[]-':
                out.append('\\' + d)
            elif d in 'nt':
                out.append('\\' + d)
            elif d.isdigit():
                out.append('\\' + d)         # backref
            else:
                out.append(re.escape(d))
            i += 2
            continue
        if c in '()|+?{}':
            out.append('\\' + c)             # BRE bare metachar = literal
        elif c == '[':
            j = _scan_bracket(p, i)
            out.append(_posix_expand(p[i:j]))
            i = j
            continue
        else:
            out.append(c)
        i += 1
    return ''.join(out)


def _compile_matcher(spec):
    parts = []
    for pat in spec.patterns:
        if spec.mode == 'fixed':
            parts.append(re.escape(pat))
        elif spec.mode == 'ere':
            parts.append(_ere_to_py(pat))
        else:
            parts.append(_bre_to_py(pat))
    body = '|'.join('(?:%s)' % p for p in parts)
    if spec.word_regexp:
        body = r'(?<![A-Za-z0-9_])(?:%s)(?![A-Za-z0-9_])' % body
    if spec.line_regexp:
        body = r'^(?:%s)$' % body
    flags = re.IGNORECASE if spec.ignore_case else 0
    try:
        return re.compile(body, flags)
    except re.error as e:
        raise _Refusal(
            f'untranslatable pattern {spec.patterns!r}: {e}') from None


# ═════════════════════════════════════════════════════════════════════
#  Engine — GNU-faithful execution
# ═════════════════════════════════════════════════════════════════════

def _check_deadline(t0):
    if time.monotonic() - t0 > _DEADLINE_S:
        raise _Deadline()


def _dir_pruned(name, spec):
    """GNU --exclude-dir applies by basename during descent AND to explicit
    dir operands (probed: `grep -r --exclude-dir=logs x sub/logs` prints
    nothing). IGNORE_DIRS rides the same path, mirroring the hardening
    layer's injected --exclude-dir flags."""
    if name in IGNORE_DIRS:
        return True
    return any(fnmatch.fnmatch(name, g) for g in spec.exclude_dirs)


def _file_filtered(name, spec):
    """GNU --include/--exclude apply by basename to every searched file,
    explicit operands included (probed: --include='*.log' on an explicit
    .txt operand skips it)."""
    if spec.includes and not any(fnmatch.fnmatch(name, g) for g in spec.includes):
        return True
    if spec.excludes and any(fnmatch.fnmatch(name, g) for g in spec.excludes):
        return True
    return False


def _resolve_operand(word, cwd):
    """Apply the shell expansion the real grep never sees because we
    intercept before the shell: $VAR (unless single-quoted), ~ (unquoted),
    globs (unquoted). Returns a list of operand strings; an unquoted word
    expanding to empty is REMOVED (shell word removal)."""
    v = word.text
    if '$' in v and not word.squote:
        v = os.path.expandvars(v)
    if v == '' and not (word.squote or word.dquote):
        return []
    if v.startswith('~') and not (word.squote or word.dquote):
        v = os.path.expanduser(v)
    if not word.squote and not word.dquote and re.search(r'[*?[]', v):
        matches = _glob.glob(v) if os.path.isabs(v) else \
            _glob.glob(os.path.join(cwd, v))
        if matches:
            out = []
            for m in sorted(matches):      # bash sorts glob results
                if not os.path.isabs(v):
                    m = os.path.relpath(m, cwd)
                out.append(m)
            return out
    return [v]


def _walk_dfs(cur_abs, rel_prefix, spec, t0, out, disp_base):
    """Recursive descent in readdir order with directories descended
    INLINE — the traversal order GNU grep's fts uses, so ``grep -r … |
    head`` shows the same first matches. Symlinked directories are not
    followed (GNU -r semantics); symlinked files are read through."""
    try:
        entries = list(os.scandir(cur_abs))
    except OSError as e:
        # Unreadable directory mid-descent (permissions, FUSE blip) — GNU
        # grep reports it on stderr; here we skip the subtree but the skip
        # must still leave a trace (§2.2 ratchet).
        logger.debug('[grep_redirect] scandir %s failed, subtree skipped: %s',
                     cur_abs, e)
        return
    for ent in entries:
        _check_deadline(t0)
        rel = ent.name if not rel_prefix else rel_prefix + '/' + ent.name
        if ent.is_dir(follow_symlinks=False):
            if _dir_pruned(ent.name, spec):
                continue
            _walk_dfs(ent.path, rel, spec, t0, out, disp_base)
        elif ent.is_file():
            if _file_filtered(ent.name, spec):
                continue
            if disp_base is None:
                d = rel
            elif disp_base == '':
                d = '/' + rel
            else:
                d = disp_base + '/' + rel
            out.append((ent.path, d))


def _enumerate(spec, cwd, t0):
    """Resolve operands to (abs_path, display) pairs plus ordered error
    lines. Returns (files, errors, show_name)."""
    operands = []
    for w in spec.operands:
        if w.text == '$' and not w.squote:
            raise _Refusal('command substitution in grep arguments')
        operands.extend(_resolve_operand(w, cwd))
    if spec.implicit_dot:
        operands = ['.']
    if not operands:
        # Every operand expanded away — the real grep would read STDIN,
        # which is a different tool; refuse honestly.
        raise _Refusal('grep operands expanded to empty')
    files = []
    errors = []
    had_dir_operand = False
    for op in operands:
        _check_deadline(t0)
        p = op if os.path.isabs(op) else os.path.join(cwd, op)
        if os.path.isdir(p):
            had_dir_operand = True
            if not spec.recursive:
                if spec.dir_action == 'skip':
                    continue
                errors.append(f'grep: {op}: Is a directory')
                continue
            norm = op.rstrip('/') or '/'
            if norm != '/' and _dir_pruned(os.path.basename(norm), spec):
                continue
            if norm == '/':
                disp_base = ''
            elif spec.implicit_dot:
                disp_base = None          # bare `grep -r x` → no ./ prefix
            else:
                disp_base = norm
            _walk_dfs(p, '', spec, t0, files, disp_base)
        elif os.path.lexists(p):
            if _file_filtered(os.path.basename(p), spec):
                continue
            files.append((p, op))
        else:
            errors.append(f'grep: {op}: No such file or directory')
    show_name = bool(spec.with_filename) or (
        spec.with_filename is None and (len(operands) > 1 or had_dir_operand))
    return files, errors, show_name


def _line_iter(path):
    """Yield file lines as str, newline stripped, CR preserved."""
    with open(path, 'rb') as f:
        for raw in f:
            if raw.endswith(b'\n'):
                raw = raw[:-1]
            yield raw.decode('utf-8', errors='replace')


def _search_file(path, spec, rx, t0):
    """Search one file. Returns a result dict; OSError propagates."""
    with open(path, 'rb') as f:
        binary = b'\0' in f.read(_BINARY_SNIFF_BYTES)
    if binary and spec.binary_mode == 'without-match':
        return {'skip': True}
    selected = []          # post-invert selected (index, line)
    raw_match = False      # any pre-invert match (drives -l/-L rc, probed)
    count = 0
    for idx, line in enumerate(_line_iter(path)):
        if idx % 20000 == 0:
            _check_deadline(t0)
        m = rx.search(line)
        if m:
            raw_match = True
        if not m and not spec.invert or m and spec.invert:
            continue                       # not selected
        count += 1
        if spec.quiet:
            # -q exits at the first selected line.
            return {'quiet_hit': True}
        if spec.list_mode or spec.count_only:
            continue                       # count is all we need
        selected.append((idx, line))
        if spec.max_count is not None and len(selected) >= spec.max_count:
            break
    return {'skip': False, 'binary': binary, 'raw_match': raw_match,
            'count': count, 'selected': selected}


def _context_groups(selected, spec):
    """Merge selected indices into print ranges under -A/-B/-C."""
    groups = []
    match_idx = set()
    for idx, _ln in selected:
        match_idx.add(idx)
        lo = max(0, idx - spec.before)
        hi = idx + spec.after + 1
        if groups and lo <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], hi)
        else:
            groups.append([lo, hi])
    return groups, match_idx


def _execute(spec, cwd, t0):
    """Run the search. Returns (stdout, stderr, rc)."""
    rx = _compile_matcher(spec)
    files, errors, show_name = _enumerate(spec, cwd, t0)
    out = []
    err = []
    had_error = bool(errors)
    raw_match_any = False
    selected_any = False
    printed_group = False                  # context `--` separator state

    def flush_errors():
        if not spec.no_messages:
            err.extend(errors)
        errors.clear()

    for path, disp in files:
        _check_deadline(t0)
        try:
            res = _search_file(path, spec, rx, t0)
        except OSError as e:
            had_error = True
            errors.append(f'grep: {disp}: {e.strerror or e}')
            flush_errors()
            continue
        flush_errors()
        if res.get('skip'):
            continue
        if res.get('quiet_hit'):
            stderr = '\n'.join(err) + ('\n' if err else '')
            return '', stderr, 0
        raw_match_any = raw_match_any or res['raw_match']
        count = res.get('count', 0)
        selected_any = selected_any or count > 0
        if spec.list_mode:
            if (res['raw_match'] and spec.list_mode == 'with') or \
                    (not res['raw_match'] and spec.list_mode == 'without'):
                out.append(disp)
            continue
        if spec.count_only:
            out.append(f'{disp}:{count}' if show_name else str(count))
            continue
        selected = res.get('selected', [])
        if not selected:
            continue
        if res.get('binary') and spec.binary_mode == 'binary':
            out.append(f'Binary file {disp} matches')
            continue
        if spec.only_matching:
            for idx, line in selected:
                for m in rx.finditer(line):
                    if spec.line_number:
                        head = f'{disp}:{idx + 1}:' if show_name else f'{idx + 1}:'
                    else:
                        head = f'{disp}:' if show_name else ''
                    out.append(head + m.group(0))
            continue
        if spec.before or spec.after:
            groups, match_idx = _context_groups(selected, spec)
            need = set()
            for lo, hi in groups:
                need.update(range(lo, hi))
            lines_cache = {}
            for idx, line in enumerate(_line_iter(path)):
                if idx in need:
                    lines_cache[idx] = line
                if idx > max(need):
                    break
            for lo, hi in groups:
                if printed_group:
                    out.append('--')       # separator BETWEEN groups only
                for idx in range(lo, hi):
                    line = lines_cache.get(idx)
                    if line is None:
                        continue
                    sep = ':' if idx in match_idx else '-'
                    if show_name:
                        head = f'{disp}{sep}{idx + 1}{sep}' if spec.line_number \
                            else f'{disp}{sep}'
                    else:
                        head = f'{idx + 1}{sep}' if spec.line_number else ''
                    out.append(head + line)
                printed_group = True
            continue
        for idx, line in selected:
            if show_name:
                head = f'{disp}:{idx + 1}:' if spec.line_number else f'{disp}:'
            else:
                head = f'{idx + 1}:' if spec.line_number else ''
            out.append(head + line)
    flush_errors()

    if selected_any or (spec.list_mode and raw_match_any):
        rc = 0
        if had_error:
            rc = 2                         # errors win over matches (probed)
    elif had_error:
        rc = 2
    else:
        rc = 1
    stdout = '\n'.join(out) + ('\n' if out else '')
    stderr = '\n'.join(err) + ('\n' if err else '')
    return stdout, stderr, rc


# ═════════════════════════════════════════════════════════════════════
#  Splicer — rewrite the shell command around temp files
# ═════════════════════════════════════════════════════════════════════

# Words allowed to precede the grep binary; they stay in place and the
# subshell replacement keeps them valid (`if ( …; exit N )` etc.).
_PREFIX_KEYWORDS = frozenset({'if', 'while', 'until', '!', 'time'})
_ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# Shell builtins that never write the filesystem — allowed to precede a
# redirected grep in addition to the _READONLY_COMMANDS whitelist.
_SAFE_BUILTINS = frozenset({
    'set', 'export', 'unset', 'shopt', 'umask', 'alias', 'unalias', 'hash',
    'local', 'declare', 'typeset', 'readonly', 'shift', 'true', 'false', ':',
})


@dataclass
class GrepRedirectPlan:
    rewritten: object = None            # spliced command (exec side only)
    refused_segment: object = None    # segment that must be refused
    refusal_reason: object = None
    n_redirected: int = 0
    elapsed: float = 0.0


def _splice_replacement(stdout, stderr, rc, assigns=(), in_cmdsubst=False):
    """Build the shell group standing in for the grep segment, with output
    persisted to temp files so the pipeline continues from real bytes.

    Shape rules, all learned from bash parse failures:
      - default is a SUBSHELL ``( … ; exit N )`` — ``exit`` inside a
        top-level BRACE group would kill the whole ``bash -c`` shell and
        skip ``|| echo …`` / ``&& cat …`` chains;
      - immediately after ``$(`` the subshell form reads as ``$((`` —
        arithmetic expansion — so inside command substitution a brace
        group is used instead (``exit`` there terminates the cmdsubst
        subshell, which is exactly the local exit we need);
      - env assignments (``LC_ALL=C``) cannot prefix EITHER group form
        (``VAR=x ( … )`` is a syntax error), so they move INSIDE the
        group where they prefix the ``cat``."""
    lead = ' '.join(assigns) + ' ' if assigns else ''
    if not stdout and not stderr:
        body = f'{lead}: ; exit {rc}'
    else:
        fd, out_path = tempfile.mkstemp(prefix='tofu_gred_')
        with os.fdopen(fd, 'wb') as f:
            f.write(stdout.encode('utf-8', errors='replace'))
        _register_temp(out_path)
        body = f'{lead}cat {shlex.quote(out_path)}'
        if stderr:
            fd, err_path = tempfile.mkstemp(prefix='tofu_gred_err_')
            with os.fdopen(fd, 'wb') as f:
                f.write(stderr.encode('utf-8', errors='replace'))
            _register_temp(err_path)
            body += f' ; cat {shlex.quote(err_path)} >&2'
        body += f' ; exit {rc}'
    if in_cmdsubst:
        return f'{{ {body} ; }}'
    return f'( {body} )'


def _segment_is_side_effect_free(seg, cwd):
    """(safe, effective_cwd, reason) — whether *seg* may precede a
    plan-time-executed grep. Only provably read-only commands, shell
    builtins without filesystem effects, and literal ``cd`` (folded into
    the returned cwd) qualify; any output redirection disqualifies."""
    if _REDIRECT_PATTERN.search(_mask_quoted_literals(seg)):
        return False, cwd, 'writes via output redirection'
    words = _shell_words(seg.strip())
    if not words:
        return True, cwd, ''
    k = 0
    while k < len(words) and not words[k].squote and not words[k].dquote \
            and _ENV_ASSIGN_RE.match(words[k].text):
        k += 1
    if k >= len(words):
        return True, cwd, ''            # pure variable assignments
    base = words[k].text.split('/')[-1]
    if base in ('cd', 'pushd'):
        if k + 1 < len(words) and not words[k + 1].text.startswith('$'):
            t = words[k + 1].text
            eff = t if os.path.isabs(t) else os.path.normpath(os.path.join(cwd, t))
            return True, eff, ''
        return False, cwd, 'dynamic cd target'
    if base == 'popd':
        return False, cwd, 'popd cannot be folded statically'
    if base == 'git':
        sub = words[k + 1].text if k + 1 < len(words) else ''
        if sub in _GIT_READONLY_SUBCOMMANDS:
            return True, cwd, ''
        return False, cwd, f'git {sub} may write'
    # sed sits in _READONLY_COMMANDS as a pure stdout filter, but -i writes
    # in place; awk programs can open output files themselves (their `>` is
    # quoted and invisible to the redirection check above).
    if base == 'sed' and any(not w.squote and not w.dquote and
                             w.text.startswith('-i') for w in words[k + 1:]):
        return False, cwd, 'sed -i writes in place'
    if base == 'awk':
        return False, cwd, 'awk programs can write files'
    if base in _READONLY_COMMANDS or base in _SAFE_BUILTINS:
        return True, cwd, ''
    return False, cwd, f'preceding `{base}` may have side effects'


def plan_grep_redirect(command, cwd):
    """Plan the transparent redirect for *command*.

    Returns a plan with ``rewritten`` set when every filesystem grep was
    executed and spliced; a plan with ``refused_segment`` + reason when at
    least one cannot be translated honestly (caller falls back to the
    refuse+teach message); or None when no filesystem grep is present.
    """
    if os.name != 'posix':
        return None
    _sweep_temps()
    t0 = time.monotonic()
    spans = _split_pipeline_spans(command)
    found_any = False
    replacements = []                     # (start, end, text)
    for idx, (s, e) in enumerate(spans):
        seg_raw = command[s:e]
        words = _shell_words(seg_raw)
        if not words:
            continue
        # See through benign prefixes. Keywords (if/while/until/!/time)
        # stay in place — they are valid around either group form; env
        # assignments move INSIDE the replacement group (an assignment
        # cannot prefix a group: `VAR=x ( … )` is a bash syntax error).
        k = 0
        assigns = []
        while k < len(words) and not words[k].squote and not words[k].dquote:
            if _ENV_ASSIGN_RE.match(words[k].text):
                assigns.append(words[k].text)
            elif words[k].text not in _PREFIX_KEYWORDS:
                break
            k += 1
        if k >= len(words):
            continue
        base = words[k].text.split('/')[-1]
        if base in ('sudo', 'doas', 'command', 'builtin', 'exec'):
            if any(w.text.split('/')[-1] in _GREP_BINARIES
                   for w in words[k + 1:]):
                return GrepRedirectPlan(
                    refused_segment=seg_raw.strip(),
                    refusal_reason=f'{base}-wrapped grep cannot be redirected')
            continue
        if base not in _GREP_BINARIES:
            continue
        # Redirection boundary: the first unquoted redir word ends the argv;
        # the redir tail is preserved verbatim after the subshell.
        redir_at = None
        j = k
        while j < len(words):
            raw = seg_raw[words[j].start:words[j].end]
            if not words[j].squote and not words[j].dquote and (
                    _REDIR_FUSED_RE.match(raw) or _REDIR_BARE_RE.match(raw)):
                redir_at = j
                break
            j += 1
        argv_end = redir_at if redir_at is not None else len(words)
        grep_words = words[k:argv_end]
        # Cheap gate: is this a FILESYSTEM grep at all? Stream greps pass
        # through untouched (the legacy classifier on clean tokens).
        if not _grep_segment_reads_filesystem([w.text for w in grep_words]):
            continue
        found_any = True
        # Plan-time execution is only valid when no earlier segment can
        # change the bytes this grep reads.
        eff_cwd = cwd
        for ps, pe in spans[:idx]:
            ok, eff_cwd, why = _segment_is_side_effect_free(
                command[ps:pe], eff_cwd)
            if not ok:
                return GrepRedirectPlan(
                    refused_segment=seg_raw.strip(),
                    refusal_reason='grep runs at plan time but an earlier '
                                   f'segment is not provably read-only ({why})')
        try:
            spec = _parse_grep_args(grep_words, eff_cwd)
            stdout, stderr, rc = _execute(spec, eff_cwd, t0)
        except _Deadline:
            logger.debug('[grep_redirect] deadline hit (%.0fs) on segment: %.120s',
                         _DEADLINE_S, seg_raw.strip())
            return GrepRedirectPlan(
                refused_segment=seg_raw.strip(),
                refusal_reason=f'internal grep engine exceeded the '
                               f'{_DEADLINE_S:.0f}s deadline (hostile '
                               f'filesystem window)')
        except _Refusal as rf:
            logger.debug('[grep_redirect] segment not redirectable: %.120s — %s',
                         seg_raw.strip(), rf.reason)
            return GrepRedirectPlan(refused_segment=seg_raw.strip(),
                                    refusal_reason=rf.reason)
        first_moved = k - len(assigns)
        abs_start = s + words[first_moved].start
        # `x=$(grep …)` leaves the replacement right after `$(` — the
        # subshell form would read as arithmetic, so the splicer switches
        # to a brace group there.
        in_cmdsubst = command[:abs_start].rstrip().endswith('$(')
        replacement = _splice_replacement(stdout, stderr, rc,
                                          assigns=assigns,
                                          in_cmdsubst=in_cmdsubst)
        abs_end = s + (words[argv_end - 1].end if argv_end > k else words[k].end)
        replacements.append((abs_start, abs_end, replacement))
    if not found_any:
        return None
    rewritten = command
    for s, e, text in sorted(replacements, reverse=True):
        rewritten = rewritten[:s] + text + rewritten[e:]
    return GrepRedirectPlan(rewritten=rewritten,
                            n_redirected=len(replacements),
                            elapsed=time.monotonic() - t0)
