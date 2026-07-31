#!/usr/bin/env python3
"""Test-suite census: machine-check EVERY test file for known failure modes.

WHY THIS EXISTS
---------------
This repo has ~1160 test files / ~324k lines. "Read them one by one" is not a
thing a human or an agent can honestly do, so the suite's health has been
audited anecdotally — and the JOURNAL records SEVEN separate incidents of the
same family ("守卫过期家族"): a guard test that silently stopped guarding
anything, discovered only by accident. A guard that died without anyone
noticing is worse than no guard, because it manufactures the illusion of
protection.

This tool replaces the anecdote with a CENSUS. It walks every tracked test
file with the ``ast`` module (no execution, no imports, so it is safe and fast
on a FUSE mount) and classifies each one against objective, computable
criteria. Zero files are skipped; a file that cannot be parsed is reported as
its own finding rather than silently dropped.

WHAT IT DETECTS (each finding names file:line so it is actionable)

  A0  unparseable         — file does not even parse (dead weight / broken)
  A   no-assertion test   — a test function whose 1-2 level call closure
                            contains no assert / raises / mock assertion at
                            all: it can only fail by exploding
  A1  skip-only test      — the function's ONLY exit is pytest.skip(): it is
                            STRUCTURALLY incapable of failing, so it reports
                            nothing while looking like a check
  A2  laundered call      — every call under test sits inside a bare
                            ``except Exception: pass``, so a real defect and a
                            clean run are indistinguishable
  B   vacuous assertion   — ``assert True`` / ``assert ... or True``:
                            green by construction
  C   swallowed body      — the whole test body sits in try/except that
                            passes or ``pytest.skip()``s on failure
  D   unconditional skip  — module- or test-level skip/xfail with no runtime
                            condition: dead test kept on life support
  E   drifted anchor      — a source-text guard asserts a literal needle is
                            present in text READ FROM a shipped file, and the
                            needle is not there. THIS is the detector for the
                            守卫过期家族: the guard still passes/fails for
                            reasons unrelated to what it claims to guard
  F   dead path anchor    — the test references a repo-relative path that no
                            longer exists on disk
  G   implementation-face — the test reads shipped SOURCE TEXT or reaches into
                            private symbols. Per the charter this is only
                            legitimate for RATCHET guards; for behaviour
                            guards it is the rot vector. Reported, not banned
  H   near-duplicate      — same test-function name defined in N files
  I   coverage gap        — a shipped module that NO test file imports or names

CALIBRATION (2026-07-27, measured — do not loosen without re-measuring)
    The first run of this tool reported 128 A + 40 E findings. A file-by-file
    audit of ALL of them found:
      * A: ~110 of 128 were FALSE POSITIVES — the suite routinely delegates its
        assertions to a helper (``run_harness()`` in tests/_jsdom.py, a local
        ``_nc()`` neuter driver, a shared ``_run_late_finish()`` case). Hence
        ``_closure_asserts``: follow calls 2 levels deep, same-file and into
        ``tests/_*.py``, before calling a test assertion-free.
      * E: 40 of 40 were FALSE POSITIVES, because the original check never
        asked WHAT the needle was being tested against — subprocess stdout,
        ``__all__``, ``dir()``, a function's return value, an in-memory error
        list. Hence ``_shipped_source_reads``: a needle is only judged when the
        haystack is a variable provably assigned from reading a shipped file.
      * And the ONE genuine drift in the suite (test_project_board.py's NC
        anchor, broken by a block inserted between two anchored lines) was
        MISSED by the original check entirely.
    The lesson, in the charter's words: a detector's output is a claim, not a
    fact. A0/A1/A2/B/C/D/F stayed precise; A and E needed the closure and
    provenance judgements above.

USAGE
    python scripts/audit_tests.py                 # human summary
    python scripts/audit_tests.py --json out.json # machine-readable census
    python scripts/audit_tests.py --category E    # drill into one finding kind
    python scripts/audit_tests.py --check         # exit 1 if any BLOCKING
                                                  # finding appears (CI mode)

CI MODE / RATCHET
    ``--check`` compares the counts against ``tests/audit_baseline.json`` and
    fails only when a category grows. That makes this a one-way ratchet: the
    debt can be paid down but never silently re-accumulated. Regenerate the
    baseline deliberately with ``--write-baseline`` (and say why in the diff).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_PATH = os.path.join(REPO, 'tests', 'audit_baseline.json')

# ── assertion vocabulary ──────────────────────────────────────────────
# A test "asserts" if it contains any of these. Kept deliberately generous:
# a false NEGATIVE here (calling a real test assertion-free) is noise, so we
# accept every plausible assertion form the suite actually uses.
_ASSERT_CALL_NAMES = {
    'assertEqual', 'assertNotEqual', 'assertTrue', 'assertFalse', 'assertIs',
    'assertIsNot', 'assertIsNone', 'assertIsNotNone', 'assertIn', 'assertNotIn',
    'assertRaises', 'assertRaisesRegex', 'assertAlmostEqual', 'assertGreater',
    'assertGreaterEqual', 'assertLess', 'assertLessEqual', 'assertCountEqual',
    'assertListEqual', 'assertDictEqual', 'assertSetEqual', 'assertRegex',
    'assertNotRegex', 'assertIsInstance', 'fail', 'failUnless',
    'assert_called', 'assert_called_once', 'assert_called_with',
    'assert_called_once_with', 'assert_any_call', 'assert_has_calls',
    'assert_not_called', 'assert_awaited', 'assert_awaited_once',
    'raises', 'warns', 'approx', 'check_output', 'assert_frame_equal',
}
# Helper-name heuristic: many files funnel assertions through a local helper
# (``_expect(...)`` / ``_must(...)`` / ``check_x(...)``). A test calling one of
# these is credited as asserting.
_ASSERT_HELPER_RE = re.compile(
    r'^(_?(expect|must|check|verify|require|ensure|assert)\w*)$', re.I)

_SKIP_NAMES = {'skip', 'skipif', 'xfail', 'skipTest', 'skipUnless'}

# Repo-relative path literal, e.g. 'lib/tasks_pkg/orchestrator/_run.py'.
_PATH_RE = re.compile(
    r'^(lib|routes|static|scripts|tests|tofu|docs|desktop|clients)/'
    r'[\w./\-]+\.(py|js|css|md|json|html|txt|toml|sh|d\.ts)$')

# Shipped source dirs whose TEXT being read by a test = implementation-face.
_SHIPPED_DIRS = ('lib/', 'routes/', 'static/', 'server.py', 'export.py')


def _tracked(pattern: str) -> list[str]:
    """git ls-files — never os.walk (FUSE mounts make walks pathologically slow)."""
    out = subprocess.run(['git', 'ls-files', pattern], cwd=REPO,
                         capture_output=True, text=True, timeout=120)
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


# ══════════════════════════════════════════════════════════════════════
#  Per-file analysis
# ══════════════════════════════════════════════════════════════════════

class FileReport:
    def __init__(self, rel: str):
        self.rel = rel
        self.loc = 0
        self.n_tests = 0
        self.findings: list[tuple[str, int, str]] = []   # (category, line, detail)
        self.test_names: set[str] = set()
        self.imports: set[str] = set()

    def add(self, cat: str, line: int, detail: str):
        self.findings.append((cat, line, detail))


def _is_test_func(node) -> bool:
    return (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith('test'))


def _call_name(node) -> str:
    """Best-effort dotted-tail name of a Call's func (``a.b.c(x)`` → ``c``)."""
    f = node.func if isinstance(node, ast.Call) else node
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ''


def _body_asserts(fn) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, ast.Raise):
            return True
        if isinstance(n, ast.Call):
            nm = _call_name(n)
            if nm in _ASSERT_CALL_NAMES or _ASSERT_HELPER_RE.match(nm or ''):
                return True
        # ``with pytest.raises(...)`` shows up as a Call, covered above.
    return False


def _closure_asserts(fn, local_funcs: dict, helper_funcs: dict, depth: int = 2) -> bool:
    """True if ``fn`` OR any function it calls (up to ``depth`` levels) asserts.

    The suite delegates assertions constantly — a jsdom test body is often just
    ``run_harness('x.js', min_pass=12)`` with the returncode/PASS-count checks
    living in ``tests/_jsdom.py``, and every NEUTER test funnels through a local
    ``_nc()`` driver. Judging only the immediate body called ~110 such tests
    "assertion-free", which is noise that buries the handful of real ones.
    """
    if _body_asserts(fn):
        return True
    if depth <= 0:
        return False
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        nm = _call_name(n)
        callee = local_funcs.get(nm) or helper_funcs.get(nm)
        if callee is not None and callee is not fn:
            if _closure_asserts(callee, local_funcs, helper_funcs, depth - 1):
                return True
    return False


def _skip_only(fn) -> bool:
    """The function's only way out is ``pytest.skip()`` — it cannot fail.

    A test like this looks like a check in the report but is structurally
    incapable of reporting a problem: whatever it discovers, the verdict is
    'skipped'. Measured instances: two ratchet-tightness checks that skipped
    instead of failing when their BASELINE had gone loose, so a stale baseline
    could sit above the real count forever while every run looked clean.

    Excluded: a test whose subject is 'this call must not raise'. Those bodies
    legitimately hold no assertion — the call IS the assertion — and skip only
    to gate on a missing external tool (node/tsc). Recognised by the guarded
    call sitting at STATEMENT level after the skip gate.
    """
    calls = {_call_name(c) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    if 'skip' not in calls:
        return False
    hard = calls & (_ASSERT_CALL_NAMES | {'fail'})
    has_assert = any(isinstance(x, ast.Assert) for x in ast.walk(fn))
    has_raise = any(isinstance(x, ast.Raise) for x in ast.walk(fn))
    if hard or has_assert or has_raise:
        return False
    # 'must not raise' contract: a bare statement-level call to the subject
    # (not the skip itself) is the verification.
    for st in fn.body:
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            if _call_name(st.value) not in _SKIP_NAMES:
                return False
    return True


def _laundered(fn) -> str:
    """Every call under test is wrapped in a bare ``except Exception: pass``.

    Distinct from C (which is about assertions being swallowed): here there may
    be no assertion at all, and the exception handler makes 'the code worked'
    and 'the code blew up' produce the same green result.
    """
    for n in ast.walk(fn):
        if not isinstance(n, ast.Try):
            continue
        # Does the guarded body actually exercise the subject (any call)?
        body_mod = ast.Module(body=n.body, type_ignores=[])
        if not any(isinstance(c, ast.Call) for c in ast.walk(body_mod)):
            continue
        for h in n.handlers:
            if not all(isinstance(s, ast.Pass) for s in h.body):
                continue
            # bare ``except:`` or ``except Exception:`` — the broad kind
            t = h.type
            broad = t is None or (isinstance(t, ast.Name)
                                  and t.id in ('Exception', 'BaseException'))
            if broad:
                return 'subject call wrapped in bare `except Exception: pass`'
    return ''


def _vacuous_assert(node: ast.Assert) -> str:
    t = node.test
    if isinstance(t, ast.Constant):
        if t.value in (True, 1) or (isinstance(t.value, str) and t.value):
            return f'assert {t.value!r} — true by construction'
    # ``assert x or True`` / ``assert True or x``
    if isinstance(t, ast.BoolOp) and isinstance(t.op, ast.Or):
        for v in t.values:
            if isinstance(v, ast.Constant) and v.value in (True, 1):
                return 'assert ... or True — short-circuits to green'
    return ''


def _swallows(fn) -> str:
    """A try/except whose handler cannot fail the test (pass / skip / print)."""
    for n in ast.walk(fn):
        if not isinstance(n, ast.Try):
            continue
        # Only interesting when the guarded body itself contains assertions:
        # a try around a *setup* call is normal defensive code.
        if not any(isinstance(b, ast.Assert) for b in ast.walk(ast.Module(
                body=n.body, type_ignores=[]))):
            continue
        for h in n.handlers:
            names = {_call_name(c) for c in ast.walk(h) if isinstance(c, ast.Call)}
            has_reraise = any(isinstance(x, ast.Raise) for x in ast.walk(h))
            fails = bool(names & (_ASSERT_CALL_NAMES | {'fail'}))
            if has_reraise or fails:
                continue
            if names & _SKIP_NAMES:
                return 'assertions wrapped in try/except → pytest.skip()'
            body_is_pass = all(isinstance(s, ast.Pass) for s in h.body)
            if body_is_pass or not (fails or has_reraise):
                return 'assertions wrapped in try/except that cannot fail'
    return ''


def _unconditional_skip(dec) -> str:
    """``@pytest.mark.skip(...)`` / ``@unittest.skip(...)`` with no condition."""
    nm = _call_name(dec)
    if nm not in ('skip', 'xfail'):
        return ''
    # skipif/xfail(condition=...) are conditional → fine.
    if isinstance(dec, ast.Call):
        for kw in dec.keywords:
            if kw.arg in ('condition',):
                return ''
        if nm == 'xfail':
            # xfail(strict=True) still runs and reports — treat as conditional.
            for kw in dec.keywords:
                if kw.arg == 'strict':
                    return ''
        if dec.args and not isinstance(dec.args[0], ast.Constant):
            return ''
    return f'unconditional @{nm}'


_SHARED_HELPERS_CACHE: dict = {}


def _shared_helper_defs() -> dict:
    """Function defs from the shared ``tests/_*.py`` drivers, parsed once.

    ``tests/_jsdom.py::run_harness`` alone carries the assertions for ~36 test
    functions (returncode + no ``FAIL`` line + a ``PASS`` count floor), so
    without these a third of the frontend suite looks assertion-free.
    """
    if _SHARED_HELPERS_CACHE:
        return _SHARED_HELPERS_CACHE
    for rel in _tracked('tests/_*.py'):
        try:
            with open(os.path.join(REPO, rel), encoding='utf-8') as f:
                t = ast.parse(f.read(), filename=rel)
        except (OSError, SyntaxError):
            continue
        for n in ast.walk(t):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _SHARED_HELPERS_CACHE.setdefault(n.name, n)
    return _SHARED_HELPERS_CACHE


def _shipped_source_reads(tree, mentioned_paths: set) -> dict:
    """Map ``(scope_node, varname) -> shipped file text`` for variables provably
    assigned from reading a shipped source file.

    This is the provenance judgement that makes category E trustworthy — and it
    MUST be scoped per function, which cost two calibration rounds to get right:

      Round 1 compared every ``'needle' in X`` against the union of every shipped
      path the FILE mentioned. 40/40 false positives: ``X`` was as often
      subprocess stdout, ``__all__``, ``dir()`` output, a formatter's return
      value, or an in-memory error list. A needle 'missing' from a haystack that
      was never source text says nothing.

      Round 2 checked provenance but resolved variables MODULE-wide. Another 20
      false positives, because the idiomatic shape here is a helper named
      ``_read`` called by many tests that each bind the result to a local named
      ``src`` — test_frontend_p2p3_batch.py binds ``src`` to 17 DIFFERENT JS
      files. Module-level resolution picks an arbitrary one of those, so the
      needle gets searched in the wrong file. Variable scope is per function;
      resolving it per module is a defect in the detector, not in the suite.
    """
    out: dict = {}

    def _shipped_arg(call) -> str:
        for a in list(getattr(call, 'args', [])):
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                v = a.value.strip()
                if v in mentioned_paths and v.startswith(_SHIPPED_DIRS):
                    return v
        return ''

    scopes = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes:
        # Direct children only, so a nested def's assignments stay in ITS scope.
        for n in ast.walk(scope):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not scope:
                continue
            if not isinstance(n, ast.Assign) or len(n.targets) != 1:
                continue
            tgt = n.targets[0]
            if not isinstance(tgt, ast.Name):
                continue
            rel = ''
            for c in ast.walk(n.value):
                if isinstance(c, ast.Call):
                    nm = _call_name(c)
                    if nm in ('read', 'read_text', 'open') or nm.startswith('_'):
                        rel = rel or _shipped_arg(c)
            if not rel:
                continue
            try:
                with open(os.path.join(REPO, rel), encoding='utf-8') as f:
                    out[(id(scope), tgt.id)] = (f.read(), rel)
            except OSError:
                pass
    return out


def analyze_file(rel: str) -> FileReport:
    rep = FileReport(rel)
    path = os.path.join(REPO, rel)
    try:
        with open(path, encoding='utf-8') as f:
            src = f.read()
    except OSError as e:
        rep.add('A0', 0, f'unreadable: {e}')
        return rep
    rep.loc = src.count('\n') + 1
    try:
        tree = ast.parse(src, filename=rel)
    except SyntaxError as e:
        rep.add('A0', e.lineno or 0, f'SyntaxError: {e.msg}')
        return rep

    # ── string literals: repo paths + containment needles ──────────────
    # A needle is recorded WITH its enclosing SCOPE and the haystack variable,
    # so category E can demand that this scope's haystack be provably
    # shipped-source text. ``or``-branches are skipped (``'a' in s or "a" in s``
    # is satisfied by either), and so are haystacks whose name marks them as a
    # NEUTER product (``neutered``/``poisoned``/``patched``) — those hold
    # deliberately-mutated text and assert the anchor is GONE.
    mentioned_paths: set[str] = set()
    needles: list[tuple[str, int, str, int]] = []
    _or_branch = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
            for v in n.values:
                _or_branch.add(id(v))
    _neuter_var = re.compile(r'(?i)(neuter|poison|patched|mutat|broken|stripped)')
    _scope_of: dict = {}
    for scope in [tree] + [x for x in ast.walk(tree)
                          if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for c in ast.walk(scope):
            _scope_of.setdefault(id(c), id(scope))
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) and c is not scope:
                for d in ast.walk(c):
                    _scope_of[id(d)] = id(c)
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value.strip()
            if _PATH_RE.match(v):
                mentioned_paths.add(v)
        # ``'needle' in haystack`` — the source-anchor idiom
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], ast.In):
            if id(n) in _or_branch:
                continue
            left = n.left
            if isinstance(left, ast.Constant) and isinstance(left.value, str):
                s = left.value
                if len(s) >= 12 and re.search(r'[(_.\[=]', s):
                    hay = n.comparators[0]
                    if isinstance(hay, ast.Name) and not _neuter_var.search(hay.id):
                        needles.append((s, getattr(n, 'lineno', 0), hay.id,
                                        _scope_of.get(id(n), id(tree))))
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mod = getattr(n, 'module', None) or ''
            for a in n.names:
                rep.imports.add(mod or a.name)
            if mod:
                rep.imports.add(mod)

    # F: dead path anchors
    for p in sorted(mentioned_paths):
        if not os.path.exists(os.path.join(REPO, p)):
            rep.add('F', 0, f'references missing path {p}')

    # G: implementation-face (reads shipped source text)
    live_shipped = [p for p in mentioned_paths
                    if p.startswith(_SHIPPED_DIRS) and os.path.exists(os.path.join(REPO, p))]
    if live_shipped and needles:
        rep.add('G', 0, f'source-text anchors on {len(live_shipped)} shipped file(s): '
                        + ', '.join(sorted(live_shipped)[:4]))

    # E: drifted anchor — needle absent from the shipped source text it is
    # ACTUALLY tested against, resolved in the needle's OWN scope.
    src_vars = _shipped_source_reads(tree, mentioned_paths)
    for needle, line, hay_name, scope_id in needles:
        entry = src_vars.get((scope_id, hay_name))
        if entry is None:
            continue   # haystack not proven shipped-source text in this scope
        text, from_rel = entry
        if needle not in text:
            rep.add('E', line,
                    f'anchor absent from {from_rel} (read into {hay_name!r}): '
                    f'{needle[:70]!r}')

    # ── per-test-function checks ───────────────────────────────────────
    # Collect callable defs so a DELEGATED assertion counts (_closure_asserts):
    # same-file helpers plus the shared tests/_*.py drivers.
    local_funcs = {n.name: n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    helper_funcs = _shared_helper_defs()

    for n in ast.walk(tree):
        if isinstance(n, ast.Assert):
            why = _vacuous_assert(n)
            if why:
                rep.add('B', n.lineno, why)
        if not _is_test_func(n):
            continue
        rep.n_tests += 1
        rep.test_names.add(n.name)
        if _skip_only(n):
            rep.add('A1', n.lineno, f'{n.name}() can only skip — never fails')
        elif not _closure_asserts(n, local_funcs, helper_funcs):
            rep.add('A', n.lineno, f'{n.name}() has no assertion (incl. helpers)')
        _laun = _laundered(n)
        if _laun:
            rep.add('A2', n.lineno, f'{n.name}(): {_laun}')
        sw = _swallows(n)
        if sw:
            rep.add('C', n.lineno, f'{n.name}(): {sw}')
        for d in n.decorator_list:
            why = _unconditional_skip(d)
            if why:
                rep.add('D', n.lineno, f'{n.name}(): {why}')

    # module-level pytestmark skip
    for n in tree.body:
        if isinstance(n, ast.Assign):
            tgt = n.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == 'pytestmark':
                for c in ast.walk(n):
                    if isinstance(c, ast.Call):
                        why = _unconditional_skip(c)
                        if why:
                            rep.add('D', n.lineno, f'module-level {why}')
    return rep


# ══════════════════════════════════════════════════════════════════════
#  Cross-file analysis
# ══════════════════════════════════════════════════════════════════════

# Subsystems on the critical path. A zero-coverage module here is materially
# worse than one in a leaf utility: a silent regression costs data (task results,
# user context), money (billing), or correctness that only shows up in
# production. Used to rank gaps by RISK rather than alphabetically.
_HIGH_RISK_PREFIXES = (
    'lib/database/', 'lib/tasks_pkg/', 'lib/agent_core/', 'lib/agent_loop',
    'lib/agent_verdict/', 'lib/llm/', 'lib/llm_dispatch/', 'lib/billing/',
    'lib/production/', 'lib/conversations/', 'routes/api_v1/',
)


def _is_pure_facade(path: str) -> bool:
    """True for a module that only re-exports / declares constants.

    A facade has no behaviour of its own, so "nothing tests it directly" is not
    a coverage gap — the tests exercise it through whatever it re-exports.
    Counting facades would pad the gap list with unactionable entries.
    """
    try:
        with open(os.path.join(REPO, path), encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=path)
    except (OSError, SyntaxError):
        return False
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.Expr)):
            continue          # imports + docstring
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            continue          # __all__ / constant tables
        if isinstance(n, ast.If):
            continue          # TYPE_CHECKING blocks
        return False          # a def/class = real behaviour
    return True


def _loc(path: str) -> int:
    try:
        with open(os.path.join(REPO, path), encoding='utf-8') as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def coverage_gaps(reports: list[FileReport]) -> list[dict]:
    """Shipped modules that NO test file imports or even names.

    ★ CALIBRATION (2026-07-27): the first version of this reported ZERO gaps
    and that was a FALSE GREEN with two causes, both now fixed:

      1. It only enumerated ``lib/*.py`` + ``routes/*.py`` — the TOP LEVEL. But
         nearly all behaviour in this repo lives in sub-packages
         (``lib/tasks_pkg/**``, ``lib/database/**``, ``lib/llm/**``, …), so the
         scan looked past the entire codebase. Measured after switching to a
         recursive walk: **195 modules / 23,190 LOC with no test reference at
         all** (12.7% of shipped LOC), where the tool had claimed 0.
      2. ``mod.startswith(k + '.')`` credited a module as covered when a test
         merely named its PARENT package. ``import lib.database`` would mark
         every one of the ~90 modules under it as tested. Only an exact match or
         a reference to the module ITSELF (or something inside it) counts now.

    The lesson is the charter's, applied to my own tool: a detector that reports
    nothing is indistinguishable from a detector that is not looking. Cross-check
    a "clean" result against an independent method before believing it.

    Returns dicts sorted by risk then LOC, so the output is a work queue rather
    than an alphabetical dump.
    """
    haystack = set()
    for r in reports:
        haystack |= r.imports
    # Also scan raw test text for dotted module names (importlib / patch('lib.x.y')
    # string usage that AST imports miss) AND for bare path mentions.
    blob_names: set[str] = set()
    blob_paths: set[str] = set()
    for r in reports:
        try:
            with open(os.path.join(REPO, r.rel), encoding='utf-8') as f:
                txt = f.read()
        except OSError:
            continue
        for m in re.finditer(r'\b((?:lib|routes)(?:\.\w+)+)', txt):
            blob_names.add(m.group(1))
        for m in re.finditer(r'\b((?:lib|routes)/[\w/]+\.py)', txt):
            blob_paths.add(m.group(1))
    known = haystack | blob_names

    gaps = []
    for rel in _tracked('lib/**/*.py') + _tracked('routes/**/*.py') + \
            _tracked('lib/*.py') + _tracked('routes/*.py'):
        if rel.endswith('__init__.py'):
            continue
        if rel in blob_paths:
            continue
        mod = rel[:-3].replace('/', '.')
        # EXACT module, or a test naming something INSIDE it. Naming only the
        # PARENT package no longer counts (that was false-green cause #2).
        if any(k == mod or k.startswith(mod + '.') for k in known):
            continue
        if _is_pure_facade(rel):
            continue
        risk = 'high' if rel.startswith(_HIGH_RISK_PREFIXES) else 'low'
        gaps.append({'path': rel, 'loc': _loc(rel), 'risk': risk})
    seen = set()
    uniq = []
    for g in gaps:
        if g['path'] in seen:
            continue
        seen.add(g['path'])
        uniq.append(g)
    return sorted(uniq, key=lambda g: (g['risk'] != 'high', -g['loc']))


def duplicate_names(reports: list[FileReport]) -> list[tuple[str, list[str]]]:
    by_name: dict[str, list[str]] = defaultdict(list)
    for r in reports:
        for t in r.test_names:
            by_name[t].append(r.rel)
    out = [(n, sorted(fs)) for n, fs in by_name.items() if len(fs) >= 3]
    return sorted(out, key=lambda kv: -len(kv[1]))


# ══════════════════════════════════════════════════════════════════════
#  Reporting
# ══════════════════════════════════════════════════════════════════════

_CAT_LABEL = {
    'A0': 'unparseable file',
    'A1': 'SKIP-ONLY test (structurally cannot fail)',
    'A2': 'subject call laundered by bare except: pass',
    'A': 'test with NO assertion (incl. helper closure)',
    'B': 'vacuous assertion (green by construction)',
    'C': 'assertions swallowed by try/except',
    'D': 'unconditional skip/xfail',
    'E': 'DRIFTED source anchor (guard may be dead)',
    'F': 'reference to a missing repo path',
    'G': 'implementation-face (reads shipped source text)',
}
# Categories that block CI when they GROW (ratchet). G is informational: the
# charter permits implementation-face for ratchet guards.
BLOCKING = ('A0', 'A1', 'A2', 'A', 'B', 'C', 'D', 'E', 'F')
_REPORT_ORDER = ('A0', 'A1', 'A2', 'A', 'B', 'C', 'D', 'E', 'F', 'G')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', help='write the full census here')
    ap.add_argument('--category', help='print every finding of one category')
    ap.add_argument('--check', action='store_true',
                    help='CI ratchet: exit 1 if a blocking category grew')
    ap.add_argument('--write-baseline', action='store_true')
    ap.add_argument('--limit', type=int, default=25)
    args = ap.parse_args()

    files = _tracked('tests/test_*.py')
    reports = [analyze_file(f) for f in files]

    counts: dict[str, int] = defaultdict(int)
    per_cat: dict[str, list] = defaultdict(list)
    for r in reports:
        for cat, line, detail in r.findings:
            counts[cat] += 1
            per_cat[cat].append((r.rel, line, detail))

    total_tests = sum(r.n_tests for r in reports)
    total_loc = sum(r.loc for r in reports)

    if args.category:
        for rel, line, detail in sorted(per_cat.get(args.category, [])):
            print(f'{rel}:{line}: {detail}')
        return 0

    print('═' * 72)
    print(f'  TEST CENSUS — {len(files)} files · {total_tests} test fns · {total_loc:,} LOC')
    print('═' * 72)
    for cat in _REPORT_ORDER:
        n = counts.get(cat, 0)
        files_hit = len({x[0] for x in per_cat.get(cat, [])})
        flag = '  ' if n == 0 else ('!!' if cat in BLOCKING else ' ·')
        print(f'{flag} [{cat}] {_CAT_LABEL[cat]:<48} {n:>5} findings / {files_hit:>4} files')
        for rel, line, detail in sorted(per_cat.get(cat, []))[:args.limit if n else 0]:
            print(f'       {rel}:{line}: {detail[:100]}')
        if n > args.limit:
            print(f'       … {n - args.limit} more (--category {cat})')

    dups = duplicate_names(reports)
    print(f'\n[H] test-function names defined in >=3 files: {len(dups)}')
    for name, fs in dups[:10]:
        print(f'       {name} — {len(fs)} files')

    gaps = coverage_gaps(reports)
    hi = [g for g in gaps if g['risk'] == 'high']
    gap_loc = sum(g['loc'] for g in gaps)
    print(f'\n[I] shipped modules NO test names: {len(gaps)} '
          f'({gap_loc:,} LOC) — {len(hi)} on the critical path')
    for g in gaps[:30]:
        print(f"       [{g['risk']:>4}] {g['loc']:>5}L  {g['path']}")

    census = {
        'files': len(files), 'tests': total_tests, 'loc': total_loc,
        'counts': dict(counts),
        'findings': {c: per_cat[c] for c in per_cat},
        'duplicate_names': dups[:100],
        'coverage_gaps': gaps,
    }
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(census, f, indent=1, ensure_ascii=False)
        print(f'\nwrote {args.json}')

    if args.write_baseline:
        with open(BASELINE_PATH, 'w', encoding='utf-8') as f:
            json.dump({'counts': {c: counts.get(c, 0) for c in BLOCKING}},
                      f, indent=1)
        print(f'wrote baseline {BASELINE_PATH}')
        return 0

    if args.check:
        try:
            with open(BASELINE_PATH, encoding='utf-8') as f:
                base = json.load(f)['counts']
        except OSError:
            print('\nno baseline — run --write-baseline first')
            return 1
        grew = [(c, base.get(c, 0), counts.get(c, 0)) for c in BLOCKING
                if counts.get(c, 0) > base.get(c, 0)]
        if grew:
            print('\nRATCHET BROKEN — these categories grew:')
            for c, b, n in grew:
                print(f'  [{c}] {_CAT_LABEL[c]}: {b} → {n}')
            return 1
        print('\nratchet OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
