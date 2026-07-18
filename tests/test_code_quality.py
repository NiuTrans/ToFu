"""tests/test_code_quality.py — Automated code quality enforcement.

Ensures that CLAUDE.md logging standards are maintained:
- No silent exception catches (except blocks without logging)
- No f-strings in logger calls
- All lib/ and routes/ .py files use lib.log.get_logger (not raw logging)
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

# ── Tier: this is a BLOCKING unit-tier guard, NOT slow/opt-in. ──
# It AST-parses the lib/ + routes/ tree, but discovery is git-index-backed
# (``git ls-files`` ~15ms, see _git_tracked_py) and the parse is pure-Python
# with NO server / NO DB — the whole module runs in ~6s. It was FORMERLY marked
# ``slow`` ONLY because its old ``os.walk`` discovery timed out on the FUSE
# mount; ``slow`` quarantined it behind ``pytest -m slow``, which ``make ci``
# does not run and CI runs ``continue-on-error`` (non-blocking) — so this
# logging-discipline guard NEVER failed a build even when it should have (the
# very "green because nobody runs it" rot it exists to prevent). Now that
# discovery is fast, it belongs in the default ``unit`` tier: ``make test-unit``
# / ``make ci`` / the blocking CI ``test-unit`` job all run ``-m unit``, so a
# new silent catch fails the build on the PR that introduces it.
pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = PROJECT_ROOT / 'lib'
ROUTES_DIR = PROJECT_ROOT / 'routes'

# Files that are allowed to use raw `import logging` for valid reasons:
# - lib/log.py: IS the logging module
# - lib/__init__.py: loaded before lib.log may be ready
# - lib/_pkg_utils.py: needs `import logging` for type hints (Logger param)
# - lib/version.py: loaded at import-time before lib.log may exist
# - lib/project_error_tracker.py: standalone universal module, no lib.log dependency
# - lib/fetch/utils.py: uses `import logging as _logging` to silence urllib3
# - lib/compat.py: platform detection, loaded very early
RAW_LOGGING_ALLOWLIST = {
    'lib/log.py',
    'lib/__init__.py',
    'lib/_pkg_utils.py',
    'lib/version.py',
    'lib/project_error_tracker.py',
    'lib/fetch/utils.py',
    'lib/compat.py',
}


import functools


@functools.lru_cache(maxsize=None)
def _git_tracked_py(reldir: str) -> tuple[str, ...]:
    """Return every ``.py`` file under ``reldir`` (project-root-relative) known
    to git — both committed and untracked-but-not-ignored.

    CRITICAL: discovery MUST NOT use ``os.walk``. On a FUSE/NFS-mounted checkout
    (the deployment target — see lib/log._writable_base_dir) a recursive
    ``os.walk('lib')`` cannot even *count* the tree within 10s, so the whole
    convention guard silently times out and STOPS ENFORCING the CLAUDE.md §2
    logging discipline it exists to protect. ``git ls-files`` answers the same
    question in ~15ms because it reads the index instead of stat-ing every
    inode. ``--others --exclude-standard`` also picks up a brand-new file before
    its first commit, so a fresh silent catch is caught on the turn it lands,
    not one commit later. Returns posix relpaths.
    """
    root = str(PROJECT_ROOT)
    names: set[str] = set()
    for extra in ((), ('--others', '--exclude-standard')):
        try:
            out = subprocess.check_output(
                ['git', 'ls-files', '-z', *extra, '--', reldir],
                cwd=root, text=True)
        except (OSError, subprocess.SubprocessError):
            continue
        for rel in out.split('\0'):
            if rel.endswith('.py'):
                names.add(rel)
    return tuple(sorted(names))


def _py_files(*dirs: Path):
    """Yield all git-tracked .py files under the given directories as Paths."""
    for d in dirs:
        try:
            reldir = str(d.relative_to(PROJECT_ROOT))
        except ValueError:
            reldir = d.name
        for rel in _git_tracked_py(reldir):
            yield PROJECT_ROOT / rel


@functools.lru_cache(maxsize=None)
def _parsed_trees(directory: Path) -> tuple[tuple[str, ast.Module], ...]:
    """Walk + read + parse every .py file under ``directory`` ONCE.

    Cached across tests so the 7 checks don't each re-walk and re-parse the
    whole tree — a big win on slow FUSE mounts. Returns ``(rel_path, tree)``
    tuples; files that fail to read/parse are skipped.
    """
    out: list[tuple[str, ast.Module]] = []
    for path in _py_files(directory):
        rel = str(path.relative_to(PROJECT_ROOT))
        try:
            source = path.read_text(encoding='utf-8')
            tree = ast.parse(source, rel)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        out.append((rel, tree))
    return tuple(out)


# Names that count as "the failure was handled" inside an except block.
# Includes lib.api_response error helpers — they communicate the failure
# outward to the HTTP client (NOT silent) and api_internal_error also
# auto-logs at ERROR with traceback (CLAUDE.md §4.6.2).
_LOG_OR_HANDLE_NAMES = frozenset({
    'debug', 'info', 'warning', 'error', 'critical', 'exception',
    'log_exception', 'audit_log',
    'api_internal_error', 'api_error', 'api_bad_request',
    'api_not_found', 'api_unauthorized', 'api_forbidden',
    'api_conflict', 'api_payload_too_large', 'api_method_not_allowed',
})


def _handler_has_log_or_raise(node: ast.ExceptHandler) -> bool:
    """True if the except body logs, calls an api_* error helper, or re-raises."""
    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            return True
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr in _LOG_OR_HANDLE_NAMES:
                return True
            if isinstance(func, ast.Name) and func.id in _LOG_OR_HANDLE_NAMES:
                return True
    return False


def _exc_type_str(node: ast.ExceptHandler) -> str:
    """Human-readable caught-exception spec (comma-joined for tuples)."""
    if node.type is None:
        return '<bare>'
    t = node.type
    if isinstance(t, ast.Name):
        return t.id
    if isinstance(t, ast.Attribute):
        return t.attr
    if isinstance(t, ast.Tuple):
        return ','.join(getattr(e, 'id', getattr(e, 'attr', '?')) for e in t.elts)
    return '?'


def _body_action(body: list[ast.stmt]) -> str:
    """Describe a log-free except body: pass / return / assign / continue /
    break / logic (anything more complex)."""
    if not body or all(isinstance(s, ast.Pass) for s in body):
        return 'pass'
    simple = (ast.Pass, ast.Return, ast.Continue, ast.Break,
              ast.Assign, ast.AnnAssign, ast.AugAssign)
    if not all(isinstance(s, simple) for s in body):
        return 'logic'
    for kind, label in ((ast.Return, 'return'),
                        ((ast.Assign, ast.AnnAssign, ast.AugAssign), 'assign'),
                        (ast.Continue, 'continue'),
                        (ast.Break, 'break')):
        if any(isinstance(s, kind) for s in body):
            return label
    return 'pass'


class _ContextMixin(ast.NodeVisitor):
    """Tracks the enclosing function qualname + loop nesting for richer reports."""

    def __init__(self):
        self._func_stack: list[str] = []
        self._loop_depth = 0

    def visit_FunctionDef(self, node):
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_For(self, node):
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1

    visit_AsyncFor = visit_For
    visit_While = visit_For

    @property
    def _qualname(self) -> str:
        return '.'.join(self._func_stack) if self._func_stack else '<module>'

    def _record(self, node: ast.ExceptHandler):
        """Build a detailed finding dict for a silent except handler."""
        flags = []
        if self._loop_depth > 0:
            flags.append('in-loop')
        if node.name is None:
            flags.append('unbound')
        return {
            'lineno': node.lineno,
            'exc': _exc_type_str(node),
            'func': self._qualname,
            'action': _body_action(node.body),
            'flags': flags,
        }


def _fmt_violation(rel: str, f: dict) -> str:
    """Render a finding as a single detailed line for a failure message."""
    flag_str = (' [' + ', '.join(f['flags']) + ']') if f['flags'] else ''
    return (f"  {rel}:{f['lineno']}  except {f['exc']}  "
            f"in {f['func']}()  action={f['action']}{flag_str}")


def _render_violations(label: str, violations: list[tuple[str, dict]]) -> str:
    """Build a detailed pytest.fail message, grouped by body action.

    Each line carries file:line, caught exception type, enclosing function,
    the body action (pass/return/assign/…), and flags (in-loop / unbound) so
    the failure points straight at the fix without re-grepping.
    """
    from collections import Counter
    by_action: Counter = Counter(f['action'] for _, f in violations)
    summary = ', '.join(f'{a}={n}' for a, n in by_action.most_common())
    lines = [f'{len(violations)} {label} found [by action: {summary}]:']
    for rel, f in sorted(violations, key=lambda rv: (rv[0], rv[1]['lineno'])):
        lines.append(_fmt_violation(rel, f))
    return '\n'.join(lines)


class _SilentCatchFinder(_ContextMixin):
    """AST visitor that finds except blocks (single pass/return/continue body)
    with no logging."""

    _LOG_OR_HANDLE_NAMES = _LOG_OR_HANDLE_NAMES

    def __init__(self):
        super().__init__()
        self.issues: list[dict] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        body = node.body
        is_silent = (len(body) == 1
                     and isinstance(body[0], (ast.Pass, ast.Return, ast.Continue)))
        if is_silent and not _handler_has_log_or_raise(node):
            if not _all_exc_exempt(_exc_type_str(node)):
                self.issues.append(self._record(node))
        self.generic_visit(node)


# Exception types whose silent catch is a legitimate control-flow /
# optional-dependency branch, not a swallowed error. Catching ONLY these and
# discarding (pass / fallback) needs no log — mirrors the audit's TIER C.
_EXEMPT_EXC_TYPES = frozenset({
    'ImportError', 'ModuleNotFoundError', 'NameError', 'StopIteration',
    'StopAsyncIteration', 'GeneratorExit', 'KeyboardInterrupt', 'SystemExit',
    'CancelledError', 'TimeoutError', 'Empty', 'Full',
    'BlockingIOError', 'TimeoutExpired',
})
# Back-compat alias (older references).
_ASSIGN_EXEMPT_EXC_TYPES = _EXEMPT_EXC_TYPES


def _all_exc_exempt(exc_str: str) -> bool:
    """True iff EVERY caught type in ``exc_str`` is a control-flow / optional-dep
    type (so a silent catch is legitimate). A bare ``except:`` ('<bare>') or any
    broad/narrow-data type makes the whole handler non-exempt."""
    parts = [p.strip() for p in exc_str.split(',') if p.strip()]
    if not parts:
        return False
    return all(p in _EXEMPT_EXC_TYPES for p in parts)


def _finding_sig(rel: str, f: dict) -> tuple[str, str, str]:
    """Stable identity of a silent-catch finding: ``(relpath, qualname, exc)``.

    Deliberately EXCLUDES the line number. The previous allowlist keyed on
    ``(relpath, lineno)`` and silently rotted the moment a file was edited or
    split into a package — e.g. the ``system_context.py:454`` instrumentation
    swallow moved to ``system_context/_inject.py:134`` and the two
    ``api_response.py`` safe_route entries slid from 340/348 to 357/365, so the
    guard (had it run) would have re-flagged already-triaged code. Keying on the
    enclosing function name + caught-type tuple survives edits, line shifts, and
    the module→package renames this project does routinely.
    """
    return (rel, f['func'], f['exc'])


class _AssignSilentCatchFinder(_ContextMixin):
    """Find except blocks whose body only assigns a fallback value (or
    pass/return/continue/break) with NO logging — the ``body = ''`` class of
    silent swallow that the statement-shape check in _SilentCatchFinder misses.

    Optional-dep / control-flow exception types (see _ASSIGN_EXEMPT_EXC_TYPES)
    are skipped: there, ``x = default`` is a legitimate branch, not an error
    that went unlogged.
    """

    def __init__(self):
        super().__init__()
        self.issues: list[dict] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        body = node.body
        only_assign_or_flow = all(
            isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign,
                           ast.Pass, ast.Return, ast.Continue, ast.Break))
            for s in body
        )
        if only_assign_or_flow and not _handler_has_log_or_raise(node):
            if not _all_exc_exempt(_exc_type_str(node)):
                self.issues.append(self._record(node))
        self.generic_visit(node)


class _FStringLoggerFinder(ast.NodeVisitor):
    """AST visitor that finds f-strings used in logger method calls."""

    LOGGER_METHODS = {'debug', 'info', 'warning', 'error', 'critical', 'exception'}

    def __init__(self):
        self.issues: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr in self.LOGGER_METHODS):
            # Check if first positional arg is an f-string
            if node.args:
                first_arg = node.args[0]
                if isinstance(first_arg, ast.JoinedStr):
                    self.issues.append((node.lineno, func.attr))
        self.generic_visit(node)


# ─── Tests ───────────────────────────────────────────────


class TestSilentCatches:
    """No except blocks should silently swallow exceptions without logging."""

    # Known acceptable silent catches, keyed by the STABLE signature
    # (relpath, enclosing-function qualname, caught-type-tuple) — NOT line
    # number (see _finding_sig for why line-keyed allowlists rot). Each entry
    # must have a comment explaining why it's acceptable.
    ACCEPTABLE_SIGS = {
        # safe_route.wrapper: `except Exception: return _handle(e)` — _handle
        # routes to api_internal_error (auto-logs ERROR+traceback per §4.6.2)
        # or api_bad_request. Not visible through the local _handle indirection.
        ('lib/api_response.py', 'safe_route.wrapper', 'Exception'),
        # _db_safe.wrapper: `except _db_errors: return _handle(e)` — _handle
        # logs (warning for 'database is locked' 503, else error+traceback then
        # re-raise). Not visible through the local _handle indirection.
        ('routes/common.py', '_db_safe.wrapper', '_db_errors'),
        ('routes/common.py', '_db_safe.async_wrapper', '_db_errors'),
        # system_context._trace_fallback: the LAST-RESORT trace-helper swallow —
        # deliberately silent so a failing logging backend can never propagate
        # out of pure instrumentation and break the turn it only observes.
        ('lib/tasks_pkg/system_context/_inject.py',
         '_inject_system_contexts._trace_fallback', 'Exception'),
        # terminal_state_log_summary: builds a diagnostic string that is ITSELF
        # only ever passed to logger.error on a persist-failure branch; its own
        # fallback returns a marker string rather than logging (logging here
        # would recurse into the very failure it describes).
        ('lib/tasks_pkg/manager/_persist.py',
         'terminal_state_log_summary', 'Exception'),
        # ── Single-statement (pass/return-only) narrow fallbacks that ALSO
        #    trip this finder (a lone pass/return body qualifies as both a
        #    "silent catch" and an "assignment-only" one). Full rationale on
        #    the matching entries in TestAssignmentSilentCatches.ACCEPTABLE_SIGS:
        #    each catches a DATA-shaped error over persisted JSON / a DB rev /
        #    an optional stat and degrades to a safe default, with the real
        #    outcome logged (or the row skipped) at the caller's boundary.
        ('routes/chat.py', '_log_poll_task_id_mismatch', 'JSONDecodeError,TypeError'),
        ('routes/conversations.py', '_row_rev', 'TypeError,ValueError'),
        # RuntimeError = "no running event loop" in a sync context → skip spawn.
        ('routes/conversations.py', '_maybe_backfill_narration_on_open', 'RuntimeError'),
        # os.getsize transient stat error → treat row as present (never hide a
        # real paper).
        ('routes/paper.py', '_is_ghost_library_row', 'OSError'),
        # lib/ single-statement (pass/return/continue) narrow fallbacks that
        # also trip this finder — same DATA-shaped-except-with-safe-default
        # rationale documented on the matching TestAssignmentSilentCatches sigs.
        ('lib/database/_pg_backup/__init__.py', '<module>', 'AttributeError,TypeError'),
        ('lib/database/messages_rows.py',
         'load_message_window._seq', 'KeyError,IndexError,TypeError,ValueError'),
        ('lib/llm_dispatch/big_prefix_gate.py',
         'estimate_prefix_tokens', 'TypeError,ValueError'),
        ('lib/self_update/_apply.py', '_apply_via_tarball._req_digest', 'Exception'),
        ('lib/shutdown_marker.py', 'record_boot', 'TypeError,ValueError'),
        ('lib/shutdown_marker.py', '_is_num', 'TypeError,ValueError'),
        ('lib/tasks_pkg/killed_recovery.py',
         'list_killed_turn_convs', 'JSONDecodeError,TypeError'),
        ('lib/tasks_pkg/killed_recovery.py',
         '_context_weight', 'TypeError,ValueError'),
        ('lib/tasks_pkg/manager/_sync.py',
         '_stamp_aborted_fragment_finish_reason', 'JSONDecodeError,TypeError'),
    }

    def test_no_silent_catches_in_lib(self):
        """All except blocks in lib/ must log something."""
        violations = self._scan(LIB_DIR)
        if violations:
            pytest.fail(_render_violations('silent catch(es)', violations))

    def test_no_silent_catches_in_routes(self):
        """All except blocks in routes/ must log something."""
        violations = self._scan(ROUTES_DIR)
        if violations:
            pytest.fail(_render_violations('silent catch(es)', violations))

    def _scan(self, directory: Path) -> list[tuple[str, dict]]:
        violations = []
        for rel, tree in _parsed_trees(directory):
            finder = _SilentCatchFinder()
            finder.visit(tree)
            for f in finder.issues:
                if _finding_sig(rel, f) not in self.ACCEPTABLE_SIGS:
                    violations.append((rel, f))
        return violations


class TestAssignmentSilentCatches:
    """No except block may swallow an error by only assigning a fallback
    value (e.g. ``body = ''``) without logging.

    Complements TestSilentCatches, which only inspects pass/return/continue
    single-statement bodies. Optional-dep / control-flow exception types are
    exempt (see _ASSIGN_EXEMPT_EXC_TYPES).
    """

    # Genuinely-legit assignment-only catches, keyed by the STABLE signature
    # (relpath, enclosing-function qualname, caught-type-tuple) — NOT line
    # number (see _finding_sig). Each entry has a reason. The bulk are narrow
    # parse-fallbacks over PERSISTED/OPTIONAL JSON or env values: catching a
    # data-shaped error (JSONDecodeError / TypeError / ValueError / KeyError /
    # IndexError) and assigning a safe default ({} / [] / 0) is the documented
    # degrade path, and the caller ALWAYS logs the real outcome at its own
    # boundary (a warning on the outer handler, or the row simply skipped). A
    # logger.debug on every one of these would be pure per-row noise (CLAUDE.md
    # §2.2 "expected/harmless fallback → debug, optional").
    ACCEPTABLE_SIGS = {
        # ── Blessed broad catches shared with TestSilentCatches (see there). ──
        ('lib/api_response.py', 'safe_route.wrapper', 'Exception'),
        ('routes/common.py', '_db_safe.wrapper', '_db_errors'),
        ('routes/common.py', '_db_safe.async_wrapper', '_db_errors'),
        ('lib/tasks_pkg/system_context/_inject.py',
         '_inject_system_contexts._trace_fallback', 'Exception'),
        ('lib/memory/prefetch/_rerank.py',
         '_run_with_deadline._worker', 'BaseException'),
        ('lib/tasks_pkg/manager/_persist.py',
         'terminal_state_log_summary', 'Exception'),
        # ── entry_points().get(...) TypeError fallback → Python <3.10 API shape.
        #    Control-flow (version branch), not an error swallow. Same idiom
        #    across every plugin-discovery seam (tools / providers / schema /
        #    flags / blueprints / task-runtimes).
        ('lib/llm_dispatch/provider_registry.py',
         'discover_provider_plugins', 'TypeError'),
        ('lib/database/schema_registry.py', 'discover_schema_plugins', 'TypeError'),
        ('lib/feature_registry.py', 'discover_flag_plugins', 'TypeError'),
        ('routes/plugin_registry.py', 'discover_blueprint_plugins', 'TypeError'),
        ('routes/plugin_registry.py', 'run_startup_hooks', 'TypeError'),
        ('routes/plugin_registry.py', 'discover_task_runtime_plugins', 'TypeError'),
        # ── Narrow parse-fallbacks over PERSISTED JSON — assign safe default,
        #    outer caller logs / skips the row. Data-shaped except only. ──
        ('lib/database/messages_rows.py',
         'load_message_window._seq', 'KeyError,IndexError,TypeError,ValueError'),
        ('lib/tasks_pkg/autopilot.py',
         '_resolve_run_anchor_msgid', 'JSONDecodeError,TypeError'),
        ('lib/tasks_pkg/cache_tracking/_persist.py',
         'read_persisted_boundary', 'TypeError,KeyError,IndexError'),
        ('lib/tasks_pkg/killed_recovery.py',
         'list_killed_turn_convs', 'JSONDecodeError,TypeError'),
        ('lib/tasks_pkg/killed_recovery.py',
         '_context_weight', 'TypeError,ValueError'),
        ('lib/tasks_pkg/killed_recovery.py',
         '_redispatch_conv', 'JSONDecodeError,TypeError'),
        ('lib/tasks_pkg/killed_recovery.py',
         'restamp_killed_after_internal_fatal', 'JSONDecodeError,TypeError'),
        ('lib/tasks_pkg/killed_recovery.py',
         '_dispatch_one', 'JSONDecodeError,TypeError'),
        ('lib/tasks_pkg/killed_recovery.py',
         'run_killed_recovery', 'JSONDecodeError,TypeError'),
        ('lib/tasks_pkg/manager/_recovery.py',
         'recover_stale_tasks_on_startup', 'JSONDecodeError,TypeError'),
        ('lib/tasks_pkg/manager/_sync.py',
         '_reconcile_orphan_placeholder_on_settle', 'JSONDecodeError,TypeError'),
        ('lib/tasks_pkg/manager/_sync.py',
         '_stamp_aborted_fragment_finish_reason', 'JSONDecodeError,TypeError'),
        ('routes/api_v1/conversations.py',
         'create_branch', 'KeyError,TypeError,IndexError'),
        ('routes/chat.py', '_log_poll_task_id_mismatch', 'JSONDecodeError,TypeError'),
        ('routes/conversations.py', '_save_conv_blocking', 'JSONDecodeError,TypeError'),
        ('routes/conversations.py', '_persist_reconcile',
         'TypeError,ValueError,KeyError,IndexError'),
        # ── Narrow parse of a DB `rev` int / window arg → 0 fallback. ──
        ('routes/conversations.py', '_row_rev', 'TypeError,ValueError'),
        ('routes/conversations.py', 'list_convs', 'TypeError,ValueError'),
        ('routes/conversations.py', '_parse_window_args', 'TypeError,ValueError'),
        # ── __module__-normalise / env-parse ValueError branches — assign a
        #    default, no error to report. ──
        ('lib/database/_pg_backup/__init__.py', '<module>', 'AttributeError,TypeError'),
        ('lib/translate/segment_backfill.py', '<module>', 'ValueError,TypeError'),
        ('lib/self_update/_apply.py', '_apply_via_tarball', 'TypeError,ValueError'),
        ('lib/self_update/_apply.py', '_apply_via_tarball._req_digest', 'Exception'),
        ('lib/shutdown_marker.py', 'record_boot', 'TypeError,ValueError'),
        ('lib/shutdown_marker.py', '_is_num', 'TypeError,ValueError'),
        ('lib/llm_dispatch/big_prefix_gate.py',
         'estimate_prefix_tokens', 'TypeError,ValueError'),
        ('lib/llm/anthropic_outbound/_sse.py', 'translate', 'TypeError,ValueError'),
        ('lib/tasks_pkg/wire_fingerprint.py', 'system_fingerprint', 'TypeError,ValueError'),
        # ── RuntimeError = "no running event loop" in a sync context → skip
        #    the async spawn. Control-flow, not an error. ──
        ('routes/conversations.py', '_maybe_backfill_narration_on_open', 'RuntimeError'),
        ('routes/conversations.py', '_schedule_reconcile_persist', 'RuntimeError'),
        # ── os.getsize on a listing row: transient stat error → treat as
        #    present (never hide a real paper). Best-effort, caller logs. ──
        ('routes/paper.py', '_is_ghost_library_row', 'OSError'),
    }

    def test_no_assignment_silent_catches_in_lib(self):
        violations = self._scan(LIB_DIR)
        if violations:
            pytest.fail(_render_violations(
                'assignment-only silent catch(es) (add a logger.debug or '
                'narrow the except)', violations))

    def test_no_assignment_silent_catches_in_routes(self):
        violations = self._scan(ROUTES_DIR)
        if violations:
            pytest.fail(_render_violations(
                'assignment-only silent catch(es) (add a logger.debug or '
                'narrow the except)', violations))

    def _scan(self, directory: Path) -> list[tuple[str, dict]]:
        violations = []
        for rel, tree in _parsed_trees(directory):
            finder = _AssignSilentCatchFinder()
            finder.visit(tree)
            for f in finder.issues:
                if _finding_sig(rel, f) not in self.ACCEPTABLE_SIGS:
                    violations.append((rel, f))
        return violations


class TestNoFStringInLoggerCalls:
    """Logger calls must use %-style formatting, not f-strings."""

    def test_no_fstrings_in_lib(self):
        violations = self._scan(LIB_DIR)
        if violations:
            msg = f'{len(violations)} f-string logger call(s) found:\n'
            msg += '\n'.join(f'  {f}:{line} logger.{method}(f"...")' for f, line, method in violations)
            pytest.fail(msg)

    def test_no_fstrings_in_routes(self):
        violations = self._scan(ROUTES_DIR)
        if violations:
            msg = f'{len(violations)} f-string logger call(s) found:\n'
            msg += '\n'.join(f'  {f}:{line} logger.{method}(f"...")' for f, line, method in violations)
            pytest.fail(msg)

    def _scan(self, directory: Path) -> list[tuple[str, int, str]]:
        violations = []
        for rel, tree in _parsed_trees(directory):
            finder = _FStringLoggerFinder()
            finder.visit(tree)
            for lineno, method in finder.issues:
                violations.append((rel, lineno, method))
        return violations


class TestLoggerStandardization:
    """All .py files in lib/ (except allowlist) must use lib.log.get_logger."""

    def test_no_raw_logging_getlogger(self):
        """Files should not use `logging.getLogger(__name__)` for their module logger."""
        violations = []
        pattern = re.compile(r'^\s*_?logger\s*=\s*logging\.getLogger\(', re.MULTILINE)

        for path in _py_files(LIB_DIR):
            rel = str(path.relative_to(PROJECT_ROOT))
            if rel in RAW_LOGGING_ALLOWLIST:
                continue
            try:
                source = path.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                # OSError/FileNotFound: git ls-files can list a path a sibling
                # has already deleted on disk (mid-refactor package split) —
                # skip it rather than crash the whole guard.
                continue
            if pattern.search(source):
                violations.append(rel)

        if violations:
            msg = f'{len(violations)} file(s) use raw logging.getLogger instead of lib.log.get_logger:\n'
            msg += '\n'.join(f'  {f}' for f in sorted(violations))
            pytest.fail(msg)


class TestGuardStaysRunnable:
    """Meta-guards on the guard itself.

    The whole convention suite was silently INERT for a long time: its
    discovery walked the tree with ``os.walk``, which cannot finish within the
    test timeout on the FUSE/NFS deployment mount, so every check timed out and
    enforced NOTHING (letting an allowlist of moved line numbers rot + a
    backlog of untriaged findings accumulate). These meta-tests keep the guard
    both FAST (git-index discovery) and SHARP (the finders still bite a genuine
    new silent catch, and the allowlists carry no dead entries)."""

    def test_discovery_is_git_indexed_not_oswalk(self):
        """``_py_files`` MUST resolve via the git index, never ``os.walk``.

        Regression pin for the timeout-to-inert bug: a recursive ``os.walk`` of
        lib/ does not complete in time on FUSE. We assert (a) discovery returns
        the lib tree essentially instantly, and (b) it returns a plausible file
        count — so a future refactor that reintroduces ``os.walk`` (which would
        hang here) fails loudly instead of silently disabling the guard."""
        import time
        t0 = time.monotonic()
        files = _git_tracked_py('lib')
        elapsed = time.monotonic() - t0
        assert len(files) > 100, (
            f'git-tracked lib/ discovery returned only {len(files)} files — '
            'discovery is broken (is git available? is CWD the repo root?)')
        assert elapsed < 5.0, (
            f'lib/ discovery took {elapsed:.1f}s — it must be git-index-backed '
            '(~ms), never os.walk (which times out on the FUSE mount and left '
            'the whole guard silently inert). See _git_tracked_py.')

    def test_finder_bites_a_fresh_silent_catch(self):
        """The finders must still FLAG a genuine broad silent swallow.

        Proves the stable-signature allowlist migration did not accidentally
        neuter the check (e.g. by exempting a too-broad type). A synthetic
        ``except Exception: pass`` over a data operation is caught by BOTH
        finders and is NOT in either allowlist."""
        src = (
            'def risky():\n'
            '    try:\n'
            '        do_thing()\n'
            '    except Exception:\n'
            '        pass\n'
        )
        tree = ast.parse(src, 'synthetic_silent.py')
        f1 = _SilentCatchFinder(); f1.visit(tree)
        f2 = _AssignSilentCatchFinder(); f2.visit(tree)
        assert f1.issues, 'the single-statement finder stopped biting a bare Exception:pass'
        assert f2.issues, 'the assignment finder stopped biting a bare Exception:pass'
        # And it would NOT be silently exempted by either allowlist.
        sig = _finding_sig('synthetic_silent.py', f1.issues[0])
        assert sig not in TestSilentCatches.ACCEPTABLE_SIGS
        assert sig not in TestAssignmentSilentCatches.ACCEPTABLE_SIGS

    def test_allowlists_have_no_dead_entries(self):
        """Every ACCEPTABLE_SIGS entry must still correspond to a real finding.

        A stale allowlist entry (its function renamed / its catch narrowed away)
        is dead weight that masks nothing and misleads the next reader. This
        recomputes the live finding signatures and asserts each allowlisted sig
        is still present — turning a rotted entry into a visible failure instead
        of silent cruft (the exact failure mode of the OLD line-keyed list)."""
        live_single: set = set()
        live_assign: set = set()
        for directory in (LIB_DIR, ROUTES_DIR):
            for rel, tree in _parsed_trees(directory):
                f1 = _SilentCatchFinder(); f1.visit(tree)
                for f in f1.issues:
                    live_single.add(_finding_sig(rel, f))
                f2 = _AssignSilentCatchFinder(); f2.visit(tree)
                for f in f2.issues:
                    live_assign.add(_finding_sig(rel, f))
        dead_single = TestSilentCatches.ACCEPTABLE_SIGS - live_single
        dead_assign = TestAssignmentSilentCatches.ACCEPTABLE_SIGS - live_assign
        assert not dead_single, (
            'TestSilentCatches.ACCEPTABLE_SIGS has dead entries (no matching '
            f'live finding — remove or fix them): {sorted(dead_single)}')
        assert not dead_assign, (
            'TestAssignmentSilentCatches.ACCEPTABLE_SIGS has dead entries '
            f'(no matching live finding — remove or fix them): {sorted(dead_assign)}')
