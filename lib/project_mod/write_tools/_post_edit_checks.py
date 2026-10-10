"""lib/project_mod/write_tools/_post_edit_checks.py — report-only post-edit
checks for non-Python code: JSON and C/C++.

Problem: write/edit tools land code with no immediate compile feedback, so
broken code survives until someone runs a build by hand.

Decision: extend the existing report-only ``_py_syntax_guard`` pattern to
JSON (in-process tolerant parse) and C/C++ (``cc|c++ -fsyntax-only`` fed over
stdin with the edited file's directory as cwd, optionally reusing the file's
own flags from a nearby ``compile_commands.json``). Warnings ride the same
``syntaxWarning`` result field; nothing here ever blocks or fails a write.

Accuracy contract — accuracy first, speed second. A check returns a warning
ONLY when it is confident the edit introduced the defect. Every rule below
exists to eliminate a false-positive class; any residual doubt means abstain
(return ''):

- Regression gate: warn only when the pre-edit text passed the same check
  and the post-edit text fails (or a newly created file fails). Pre-existing
  breakage is never reported.
- The C/C++ check reports only ``error``/``fatal error`` diagnostics located
  in the edited file itself. Missing-include fatals, ``#error`` directives,
  unrecognized-flag failures, header-located errors, unknown failure modes,
  missing compilers and timeouts are all INCONCLUSIVE → silence.
- The before-state baseline is computed lazily: it runs only when the
  after-state found something to blame, so clean edits pay at most one
  bounded ``-fsyntax-only`` invocation (hard timeout, best-effort).

Alternatives considered and rejected: full project builds per edit (minutes
of latency — builds stay with run_command); reporting after-state errors
without a baseline (floods warnings on pre-existing breakage); running the
compiler with no include context and reporting everything (include-resolution
noise dominates real signal).
"""

import json
import os
import re
import shlex
import shutil
import subprocess

from lib.log import get_logger

logger = get_logger(__name__)

# Same bound as the echo-repair/syntax-guard budget in _ops.py: checks are
# in-memory text operations and must stay cheap.
_CHECK_MAX_CHARS = 2_000_000

# Hard per-invocation bound for one -fsyntax-only run. A single TU with real
# headers is typically 0.1–2s; anything slower is inconclusive rather than a
# delay in the write path.
_COMPILER_TIMEOUT_S = 5.0

# Diagnostics beyond this prefix of stderr are ignored (first errors matter).
_STDERR_CLIP = 16_000

# compile_commands.json discovery: walk at most this many directories up
# from the edited file, checking each level and its build/ child.
_CCDB_WALK_UP = 8

_C_EXTS = {'.c'}
_CPP_EXTS = {'.cc', '.cpp', '.cxx', '.c++', '.hpp', '.hh', '.hxx', '.h++'}
# '.h' is deliberately absent: ambiguous C-vs-C++, checked only when a
# compile_commands.json entry pins the language.


def post_edit_check(rel_path, before_text, new_text, abs_path=None):
    """Return a report-only warning string, or '' when not applicable or not
    confident. Never raises: the write already landed; a broken checker must
    not break the tool."""
    try:
        if os.environ.get('TOFU_EDIT_SYNTAX_GUARD', '1') == '0':
            return ''
        if not isinstance(rel_path, str) or not isinstance(new_text, str):
            return ''
        if len(new_text) > _CHECK_MAX_CHARS:
            return ''
        if rel_path.endswith(('.json', '.jsonc')):
            return _json_check(rel_path, before_text, new_text)
        if abs_path:
            return _c_compile_check(rel_path, before_text, new_text, abs_path)
        return ''
    except Exception:  # noqa: BLE001 — report-only path must never raise
        logger.debug('[PostEditCheck] checker failed for %s', rel_path,
                     exc_info=True)
        return ''


# ═══════════════════════════════════════════════════════════════════════
#  JSON / JSONC
# ═══════════════════════════════════════════════════════════════════════

def _blank_jsonc(text):
    """Blank out // and /* */ comments with spaces (newlines preserved, so
    error positions still match), string-aware. Then blank commas whose next
    non-space character outside a string is '}' or ']' (trailing commas).
    The result parses under strict ``json.loads`` iff the input is valid
    JSONC."""
    out = list(text)
    n = len(text)
    i = 0
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == '\\':
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            j = i
            while j < n and text[j] != '\n':
                out[j] = ' '
                j += 1
            i = j
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            j = i
            while j + 1 < n and text[j:j + 2] != '*/':
                if text[j] != '\n':
                    out[j] = ' '
                j += 1
            for k in range(j, min(j + 2, n)):
                if text[k] != '\n':
                    out[k] = ' '
            i = min(j + 2, n)
            continue
        if ch == ',':
            j = i + 1
            while j < n and text[j] in ' \t\r\n':
                j += 1
            if j < n and text[j] in '}]':
                out[i] = ' '
            i += 1
            continue
        i += 1
    return ''.join(out)


def _json_parses(text):
    """(ok, err) for JSON-or-JSONC text. JSONC files (tsconfig, .jsonc, VS
    Code settings) are legal inputs, so a strict failure only counts when the
    comment/trailing-comma-tolerant form also fails. Unverifiable inputs
    (pathological nesting) count as ok — abstain, never guess."""
    try:
        json.loads(text)
        return True, None
    except json.JSONDecodeError as err:
        strict_err = err
    except (ValueError, RecursionError, MemoryError):
        return True, None
    cleaned = _blank_jsonc(text)
    if cleaned == text:
        return False, strict_err
    try:
        json.loads(cleaned)
        return True, None
    except json.JSONDecodeError as tolerant_err:
        return False, tolerant_err
    except (ValueError, RecursionError, MemoryError):
        return True, None


def _json_check(rel_path, before_text, new_text):
    if before_text is not None:
        if len(before_text) > _CHECK_MAX_CHARS:
            return ''
        pre_ok, _ = _json_parses(before_text)
        if not pre_ok:
            return ''  # already broken before the edit — not ours to report
    post_ok, err = _json_parses(new_text)
    if post_ok:
        return ''
    where = (f'line {err.lineno} column {err.colno}: {err.msg}'
             if getattr(err, 'lineno', None) else str(err))
    if before_text is None:
        lead = f'{rel_path} was created with invalid JSON'
    else:
        lead = (f'this edit left {rel_path} invalid JSON '
                '(it parsed cleanly before)')
    return (f'JSON CHECK: {lead} — {where}. Fix it in a follow-up edit.')


# ═══════════════════════════════════════════════════════════════════════
#  C/C++ -fsyntax-only
# ═══════════════════════════════════════════════════════════════════════

_COMPILER_CACHE = {}
_CCDB_CACHE = {}
_CCDB_CACHE_MAX = 8

_DIAG_RE = re.compile(
    r'^(?P<file>[^\n:]+):(?P<line>\d+)(?::\d+)?:\s*'
    r'(?P<sev>fatal error|error|warning|note):\s*(?P<msg>[^\n]*)$',
    re.MULTILINE)

# Failure signatures that mean "environment/flags problem", never an edit
# regression — the whole run is discarded as inconclusive.
_INCONCLUSIVE_MSG = (
    'file not found',          # missing include (no project include path)
    'No such file or directory',
    'unrecognized command-line option',
    'unrecognized command line option',
    'unknown argument',
    'command-line option',     # covers "is not supported"/"is unknown"
)


def _compiler_for(lang, preferred=None):
    """Resolve a compiler binary for 'c'|'c++' once per process; None when
    no toolchain exists (→ abstain)."""
    key = (lang, preferred or '')
    if key in _COMPILER_CACHE:
        return _COMPILER_CACHE[key]
    if lang == 'c':
        names = ('cc', 'gcc', 'clang')
    else:
        names = ('c++', 'g++', 'clang++')
    if preferred:
        names = (preferred,) + names
    path = None
    for name in names:
        path = shutil.which(name)
        if path:
            break
    if len(_COMPILER_CACHE) < 8:
        _COMPILER_CACHE[key] = path
    return path


def _ccdb_entry(abs_path):
    """Return (directory, args) for *abs_path* from the nearest
    compile_commands.json, or None. Read-only discovery: at most
    ``_CCDB_WALK_UP`` levels up from the file, each level plus its build/
    child; parsed databases are cached by path+mtime (bounded)."""
    here = os.path.dirname(os.path.abspath(abs_path))
    target = os.path.normpath(abs_path)
    for _ in range(_CCDB_WALK_UP):
        for candidate in (os.path.join(here, 'compile_commands.json'),
                          os.path.join(here, 'build', 'compile_commands.json')):
            mapping = _ccdb_load(candidate)
            if mapping:
                hit = mapping.get(target)
                if hit:
                    return (hit[0], hit[1], target)
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


def _ccdb_load(json_path):
    """(mtime, {norm_source_path: (directory, args)}) for one database."""
    try:
        mtime = os.stat(json_path).st_mtime_ns
    except OSError:
        return None
    cached = _CCDB_CACHE.get(json_path)
    if cached and cached[0] == mtime:
        return cached[1]
    mapping = {}
    try:
        with open(json_path, 'r', encoding='utf-8') as fh:
            entries = json.load(fh)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            directory = entry.get('directory') or os.path.dirname(json_path)
            src = entry.get('file')
            if not src:
                continue
            args = entry.get('arguments')
            if args is None and isinstance(entry.get('command'), str):
                try:
                    args = shlex.split(entry['command'])
                except ValueError:
                    continue
            if not isinstance(args, list) or not args:
                continue
            norm = (src if os.path.isabs(src)
                    else os.path.normpath(os.path.join(directory, src)))
            mapping[norm] = (directory, [str(a) for a in args])
    except (OSError, ValueError):
        mapping = {}
    if len(_CCDB_CACHE) >= _CCDB_CACHE_MAX:
        _CCDB_CACHE.pop(next(iter(_CCDB_CACHE)))
    _CCDB_CACHE[json_path] = (mtime, mapping)
    return mapping


# Args dropped from a compile_commands entry: output/dependency-generation
# and anything that changes what "compile" means. Include/define/std flags
# are kept verbatim — they are what makes the check accurate.
_ARG_DROP_EXACT = frozenset((
    '-c', '-S', '-E', '-MD', '-MMD', '-MP', '-MG', '-fsyntax-only'))
_ARG_DROP_VALUE = frozenset(('-o', '-MF', '-MT', '-MQ', '-x'))
_ARG_DROP_PREFIX = ('-o', '-MF', '-MT', '-MQ', '--output')


def _sanitize_db_args(args, directory, src_abs_norm):
    """Strip a compile_commands argv down to syntax-check-safe flags, or
    None when the entry is not usable (response files). *src_abs_norm* is the
    normalized absolute source path; argv may carry it in relative form, so
    comparison joins against the entry directory."""
    if any(a.startswith('@') for a in args):
        return None
    out = []
    i = 1  # args[0] is the compiler
    while i < len(args):
        arg = args[i]
        if arg in _ARG_DROP_VALUE:
            i += 2
            continue
        if (arg in _ARG_DROP_EXACT
                or any(arg.startswith(p) and len(arg) > len(p)
                       for p in _ARG_DROP_PREFIX)):
            i += 1
            continue
        if arg == '-':
            i += 1
            continue
        if not arg.startswith('-') and os.path.normpath(
                os.path.join(directory, arg)) == src_abs_norm:
            i += 1
            continue
        out.append(arg)
        i += 1
    return out


def _classify_c_diagnostics(stderr, returncode):
    """Classify one -fsyntax-only run.

    Returns a list of (line, msg) reportable diagnostics located in the
    edited file itself (<stdin>), [] for a clean/enough run, or None for
    INCONCLUSIVE — environment or toolchain failures we must never surface
    as an edit regression."""
    clipped = stderr[:_STDERR_CLIP] if stderr else ''
    if returncode == 0:
        return []
    if not clipped.strip():
        return None  # non-zero exit with no diagnostics: unknown failure mode
    for marker in _INCONCLUSIVE_MSG:
        if marker in clipped:
            return None
    reportable = []
    saw_diag = False
    for m in _DIAG_RE.finditer(clipped):
        sev = m.group('sev')
        if sev not in ('fatal error', 'error'):
            continue
        saw_diag = True
        msg = m.group('msg').strip()
        if msg.startswith('#error'):
            return None  # macro/config-dependent, not a syntax regression
        if m.group('file') != '<stdin>':
            continue  # breakage attributed to an unchanged header, not the edit
        reportable.append((int(m.group('line')), msg))
        if len(reportable) >= 5:
            break
    if reportable:
        return reportable
    return [] if saw_diag else None


def _run_syntax_only(compiler, extra_args, cwd, lang, text):
    """One bounded -fsyntax-only invocation over stdin; None on any
    transport failure (timeout/OSError) → inconclusive."""
    cmd = [compiler, *extra_args, '-fsyntax-only', '-x', lang, '-']
    try:
        proc = subprocess.run(
            cmd, input=text, cwd=cwd or None, capture_output=True, text=True,
            errors='replace', timeout=_COMPILER_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug('[PostEditCheck] syntax-only run failed: %s', e)
        return None
    return _classify_c_diagnostics(proc.stderr or '', proc.returncode)


def _c_compile_check(rel_path, before_text, new_text, abs_path):
    lang = _c_language_for(rel_path)
    entry = _ccdb_entry(abs_path)
    preferred = None
    if entry is not None:
        _db_dir, db_args, _src = entry
        preferred = os.path.basename(db_args[0]) if db_args else None
        if lang is None and preferred:
            lang = 'c++' if '++' in preferred else 'c'
    if lang is None:
        return ''  # '.h' with no compile_commands pin: language unknown
    compiler = _compiler_for(lang, preferred=preferred)
    if not compiler:
        return ''
    extra_args, cwd = [], os.path.dirname(os.path.abspath(abs_path))
    if entry is not None:
        db_dir, db_args, src_abs_norm = entry
        sanitized = _sanitize_db_args(db_args, db_dir, src_abs_norm)
        if sanitized is None:
            return ''
        extra_args, cwd = sanitized, db_dir
    after = _run_syntax_only(compiler, extra_args, cwd, lang, new_text)
    if not after:
        return ''  # clean, or inconclusive → silence either way
    if before_text is None:
        return _format_c_warning(rel_path, compiler, after, is_new=True)
    if len(before_text) > _CHECK_MAX_CHARS:
        return ''
    before = _run_syntax_only(compiler, extra_args, cwd, lang, before_text)
    if before is None or before:
        return ''  # baseline unknown or already failing → not a regression
    return _format_c_warning(rel_path, compiler, after, is_new=False)


def _c_language_for(rel_path):
    ext = os.path.splitext(rel_path)[1]
    if ext in _C_EXTS:
        return 'c'
    if ext in _CPP_EXTS:
        return 'c++'
    return None  # includes '.h' — needs a compile_commands pin


def _format_c_warning(rel_path, compiler, diags, is_new):
    first_line, first_msg = diags[0]
    if len(first_msg) > 160:
        first_msg = first_msg[:159] + '…'
    more = f' (+{len(diags) - 1} more errors)' if len(diags) > 1 else ''
    tool = f'{os.path.basename(compiler)} -fsyntax-only'
    if is_new:
        lead = f'{rel_path} was created with code that fails `{tool}`'
    else:
        lead = (f'this edit left {rel_path} failing `{tool}` '
                '(it passed before)')
    return (f'COMPILE CHECK: {lead} — line {first_line}: {first_msg}{more}. '
            'Fix it in a follow-up edit (verify with run_command on the '
            'project).')
