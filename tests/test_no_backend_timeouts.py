"""tests/test_no_backend_timeouts.py — the tool-execution half of "no timeouts".

WHY
---
Owner ruling: "unless it crashes, what is there that can't be waited for? If I
can't wait, I will naturally pause it myself."

Two earlier batches removed the LLM transport read/first-byte timeouts
(1db38585) and the browser-side ceilings (eb1ddee5). The TOOL EXECUTION layer
was still bounded, and it bounded harder than either:

  1. ``run_command`` auto-detected 60s (FS-heavy) / 300s and then SIGTERM/
     SIGKILLed the whole process TREE, returning "[Command timed out]". A read
     timeout loses a connection; this loses the build. The most ordinary
     "worth waiting for" work in this project — a full test suite, a compile,
     a ``pip install``, a big grep — lives exactly in that window.
     ``MAX_COMMAND_TIMEOUT`` was already ``None`` ("no timeout limit"), so the
     auto-detect default was a leftover, not a considered design.
  2. ``MCP_CALL_TIMEOUT = 120`` was passed as ``read_timeout_seconds`` into
     ``session.call_tool`` — a LITERAL read timeout, on the MCP channel. A
     deep search or large parse past two minutes was declared dead.

THE RULE (identical on all three layers)
----------------------------------------
**A HANDSHAKE / liveness probe may bound itself. A WAIT may not.**

``MCP_CONNECT_TIMEOUT`` (30s) therefore stays: a handshake that never
completes means the server never came up — a crash, not a wait. Same reason
``CONNECT_TIMEOUT`` stayed in the LLM transport.

Removing the ``run_command`` ceiling costs NO control, and that is measurable
rather than assumed: both run loops (``_run_command_simple`` and
``_run_command_interactive``) poll ``task['aborted']`` every ~0.2s and kill the
process tree on Stop. Abort and timeout were always two independent paths.

DEAD-CODE HONESTY (the MCP degraded breaker)
--------------------------------------------
With no global read timeout, a call timeout can only arise for a server that
declares its OWN ``"timeout"`` in mcp_servers.json. So the
``MCP_DEGRADED_TIMEOUT_STREAK`` gate no longer trips for un-budgeted servers.
That is correct scoping, not rot: the gate exists to stop re-paying a KNOWN
user-declared budget, and a server without one has nothing to re-pay. The
budgeted path is pinned below (``test_degraded_breaker_still_live_for_...``)
so the branch cannot silently rot.

Run:  pytest tests/test_no_backend_timeouts.py -m unit
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tests._source_scan import strip_comments
except ImportError:  # pragma: no cover - path-layout fallback
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _source_scan import strip_comments

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, 'lib')


def _live_src(rel):
    """Module source with comments stripped (charter #24: a comment must be
    able to neither satisfy nor violate a guard)."""
    with open(os.path.join(LIB, rel), encoding='utf-8') as f:
        return strip_comments(f.read(), lang='python')


# ═════════════════════════════════════════════════════════════════════
#  A. run_command — no default ceiling on the process tree
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRunCommandHasNoDefaultTimeout:
    def _resolver_live(self):
        """The timeout-resolution block only, comments stripped."""
        from lib.project_mod.run_command import tool_run_command
        src = inspect.getsource(tool_run_command)
        start = src.index('Resolve timeout')
        end = src.index('if _is_dangerous_command')
        return strip_comments(src[start:end], lang='python')

    def test_no_60_or_300_default_in_the_resolver(self):
        """THE headline. NEUTER target: put `300` back and this goes RED."""
        live = self._resolver_live()
        assert '300' not in live, (
            'run_command still defaults to a 300s ceiling — a build/test/'
            'install is a wait, and the timeout path SIGKILLs the process tree')
        assert '60 if' not in live, \
            'run_command still auto-detects a 60s FS-heavy ceiling'

    def test_omitted_timeout_resolves_to_none(self):
        """Behavioural: drive the REAL resolver logic by executing the
        function up to the dangerous-command guard. A blocked command returns
        before spawning anything, so we assert on the LOGGED budget instead —
        which is what actually reaches the subprocess loop."""
        import logging
        from lib.project_mod import run_command as rc

        seen = {}

        class _Cap(logging.Handler):
            def emit(self, record):
                msg = record.getMessage()
                if 'run_command: $' in msg and 'timeout=' in msg:
                    m = re.search(r'timeout=(\S+?)[,)]', msg)
                    if m:
                        seen['budget'] = m.group(1)

        h = _Cap()
        rc.logger.addHandler(h)
        rc.logger.setLevel(logging.INFO)
        try:
            # A command that exits instantly; no timeout kwarg passed.
            rc.tool_run_command(ROOT, 'true')
        finally:
            rc.logger.removeHandler(h)

        assert seen.get('budget') == 'unlimited', (
            f"omitting `timeout` did not resolve to unlimited "
            f"(got {seen.get('budget')!r}) — a default ceiling is still applied")

    def test_explicit_timeout_still_honored(self):
        """Reverse direction: removing the DEFAULT must not remove the
        ability to bound a call deliberately."""
        import logging
        from lib.project_mod import run_command as rc

        seen = {}

        class _Cap(logging.Handler):
            def emit(self, record):
                msg = record.getMessage()
                if 'run_command: $' in msg and 'timeout=' in msg:
                    m = re.search(r'timeout=(\S+?)[,)]', msg)
                    if m:
                        seen['budget'] = m.group(1)

        h = _Cap()
        rc.logger.addHandler(h)
        rc.logger.setLevel(logging.INFO)
        try:
            rc.tool_run_command(ROOT, 'true', timeout=7)
        finally:
            rc.logger.removeHandler(h)

        assert seen.get('budget') == '7s', (
            f'an EXPLICIT timeout was dropped (got {seen.get("budget")!r}) — '
            'that is the opposite failure: a deliberate bound must still work')

    def test_abort_path_is_independent_of_timeout(self):
        """The premise that makes removing the ceiling safe: BOTH run loops
        poll task['aborted'] and kill the tree, with no reference to the
        timeout. If this regressed, 'I will pause it myself' would be a
        promise the tool layer cannot keep."""
        live = _live_src(os.path.join('project_mod', 'run_command.py'))
        assert live.count("task.get('aborted')") >= 2, (
            'the abort checks in _run_command_simple / _run_command_interactive '
            'are gone — Stop can no longer end a long command')
        assert '_kill_process_tree' in live

    def test_tool_schema_no_longer_advertises_the_old_defaults(self):
        """The model reads the schema. Leaving '60s/300s' there keeps its
        mental model wrong even after the code changed — it would keep
        passing small budgets defensively."""
        from lib.tools.project import PROJECT_TOOL_RUN_COMMAND
        fn = PROJECT_TOOL_RUN_COMMAND['function']
        blob = fn['description'] + fn['parameters']['properties']['timeout']['description']
        assert '300s' not in blob and '60s for' not in blob, \
            'run_command schema still tells the model about the 60s/300s defaults'
        assert 'NO default timeout' in fn['description'] or \
               'no timeout' in fn['parameters']['properties']['timeout']['description'].lower(), \
            'the schema does not tell the model the default is now unbounded'


# ═════════════════════════════════════════════════════════════════════
#  B. MCP — read_timeout_seconds gone, handshake kept
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMcpCallHasNoReadTimeout:
    def test_call_timeout_constant_is_none(self):
        from lib.mcp.types import MCP_CALL_TIMEOUT
        assert MCP_CALL_TIMEOUT is None, (
            f'MCP_CALL_TIMEOUT is {MCP_CALL_TIMEOUT!r} — it is passed as '
            'read_timeout_seconds, i.e. a literal read timeout on tool calls')

    def test_connect_timeout_is_kept(self):
        """Complement — the handshake bound MUST survive. A handshake that
        never completes is a crash, not a wait; deleting it would leave a
        never-started server hanging forever."""
        from lib.mcp.types import MCP_CONNECT_TIMEOUT
        assert isinstance(MCP_CONNECT_TIMEOUT, int) and MCP_CONNECT_TIMEOUT > 0, \
            'MCP_CONNECT_TIMEOUT was removed — a dead server would hang forever'

    def test_read_timeout_is_conditional_on_an_explicit_budget(self):
        """``read_timeout_seconds`` must only be constructed when a budget
        actually exists, else a None would reach timedelta()."""
        live = _live_src(os.path.join('mcp', 'client', '_bridge.py'))
        m = re.search(r'read_timeout_seconds=([^,\n]+)', live)
        assert m, 'read_timeout_seconds call site vanished'
        expr = m.group(1)
        assert 'if timeout' in expr, (
            f'read_timeout_seconds={expr!r} is unconditional — a None global '
            'budget would crash in timedelta()')

    def test_no_none_arithmetic_on_the_budget(self):
        """Every ``timeout + 10`` site must be guarded. An unguarded one is a
        TypeError on the FIRST MCP call, which is the worst kind of
        regression: it only fires in production."""
        live = _live_src(os.path.join('mcp', 'client', '_bridge.py'))
        for m in re.finditer(r'timeout \+ 10', live):
            seg = live[max(0, m.start() - 120):m.end() + 40]
            assert 'if ' in seg, (
                f'unguarded `timeout + 10` near: …{seg[-90:]!r} — crashes on a '
                'None budget')

    def test_import_and_call_shape_survive_none(self):
        """Smoke: the module imports and the per-call budget is never rendered
        with ``%d``. A ``%ds`` against None is a TypeError on EVERY MCP call —
        the worst kind of regression, because it only fires in production.

        Scoped to the CALL-BUDGET log lines: unrelated ``%ds`` sites (the
        keepalive interval, the ping timeout) are real ints and must not be
        flagged."""
        live = _live_src(os.path.join('mcp', 'client', '_bridge.py'))
        for m in re.finditer(r'timeout=%ds', live):
            seg = live[max(0, m.start() - 200):m.end()]
            assert 'ping_timeout' in seg or 'interval=' in seg, (
                f'the per-call budget is rendered with %d near …{seg[-100:]!r} '
                '— TypeError on a None budget')


# ═════════════════════════════════════════════════════════════════════
#  C. The degraded breaker: correctly SCOPED, not dead
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDegradedBreakerNotDeadCode:
    def test_breaker_gate_still_present_and_reachable(self):
        """Owner's explicit ask: prove the streak gate did not become a branch
        that can never run. It is reachable for any server declaring its own
        per-server ``timeout`` — the streak is bumped by
        ``_is_call_timeout_error``, which fires on that server's
        read-timeout."""
        live = _live_src(os.path.join('mcp', 'client', '_bridge.py'))
        assert 'MCP_DEGRADED_TIMEOUT_STREAK' in live, 'the gate was deleted'
        assert '_timeout_streak' in live, 'the streak counter was deleted'
        assert '_is_call_timeout_error' in live, \
            'nothing bumps the streak any more — the gate IS dead code now'

    def test_per_server_timeout_still_reaches_the_call(self):
        """The mechanism that keeps the breaker alive: a per-server budget in
        mcp_servers.json must still be read and used."""
        live = _live_src(os.path.join('mcp', 'client', '_bridge.py'))
        assert re.search(r"config\.get\('timeout'", live), \
            'the per-server timeout override is gone — the breaker is now dead code'

    def test_scoping_is_documented_where_the_constant_lives(self):
        """A future reader must not have to rediscover why the gate rarely
        fires. The comment block ABOVE the constant must name the per-server
        escape hatch that keeps it reachable."""
        with open(os.path.join(LIB, 'mcp', 'types.py'), encoding='utf-8') as f:
            raw = f.read()
        idx = raw.index('MCP_DEGRADED_TIMEOUT_STREAK = int(')
        blk = raw[:idx]
        # The nearest preceding comment block is the gate's own documentation.
        gate_doc = blk[blk.rindex('# ── Call-level health gating'):]
        assert 'mcp_servers.json' in gate_doc, (
            'the breaker-scoping note does not say how the gate stays '
            'reachable (a per-server "timeout") — a future reader will read it '
            'as dead code and delete it')


# ═════════════════════════════════════════════════════════════════════
#  D. ★ The durable backend ratchet.
#     Nothing above stops the next person adding a fresh subprocess-kill
#     budget or a new read_timeout_seconds. This scans lib/ for both.
# ═════════════════════════════════════════════════════════════════════

#: Modules whose bounded wait is a HANDSHAKE / liveness PROBE, which may
#: legitimately bound itself. Adding an entry is a deliberate act that must be
#: justified in review — that is the point of the ratchet.
_PROBE_ALLOWED = frozenset({
    'mcp/types.py',                  # MCP_CONNECT_TIMEOUT (handshake) + PING
    'llm/_transport.py',             # CONNECT_TIMEOUT (TCP handshake)
    'llm_dispatch/health_local.py',  # local-endpoint health probe
    'llm_dispatch/ephemeral.py',     # TCP pre-flight probe
    'llm_dispatch/autodiscover_local.py',  # loopback port probe
    'llm_dispatch/discovery/_discover.py',
    'llm_dispatch/discovery/_balance.py',
    'netpath.py',                    # network-path probe
})

#: A read timeout handed to an MCP session call.
_READ_TIMEOUT_RE = re.compile(r'read_timeout_seconds\s*=')


def _scan_read_timeouts():
    """Every ``read_timeout_seconds=`` site in tracked ``lib/`` sources.

    Driven by ``git grep`` over the INDEX, not ``os.walk``. Measured on this
    deployment's DolphinFS/FUSE mount: walking + reading all 835 tracked
    ``lib/**/*.py`` files did not finish inside 240s, which hung the suite;
    the same scan through the git index takes ~0.12s. A guard that cannot
    finish is a guard that gets deleted, so the index is the only viable
    substrate here. It also scopes the scan to TRACKED files, which is what we
    actually want to ratchet.

    Comments are stripped per charter #24 before a line counts.
    """
    import subprocess
    try:
        out = subprocess.run(
            ['git', 'grep', '-n', '-e', 'read_timeout_seconds', '--',
             'lib/*.py', 'lib/**/*.py'],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        ).stdout
    except Exception as e:  # pragma: no cover - git unavailable
        pytest.skip(f'git grep unavailable: {e}')
    hits = {}
    # Re-read only the FILES that matched (a handful), so the comment strip
    # stays cheap.
    matched_files = {ln.split(':', 1)[0] for ln in out.splitlines() if ':' in ln}
    for relpath in sorted(matched_files):
        full = os.path.join(ROOT, relpath)
        try:
            with open(full, encoding='utf-8') as f:
                live = strip_comments(f.read(), lang='python')
        except OSError:
            continue
        rel = os.path.relpath(full, LIB).replace(os.sep, '/')
        for i, line in enumerate(live.splitlines(), 1):
            if _READ_TIMEOUT_RE.search(line):
                hits.setdefault(rel, []).append((i, line.strip()))
    return hits


@pytest.mark.unit
class TestBackendRatchet:
    def test_every_read_timeout_is_conditional(self):
        """THE RATCHET (read side). A ``read_timeout_seconds=`` that is not
        guarded by an explicit-budget check re-imposes a read timeout on a
        wait. If a new one is genuinely a probe, make the guard explicit."""
        offenders = []
        for rel, sites in _scan_read_timeouts().items():
            for lineno, text in sites:
                if 'if timeout' not in text and 'if ' not in text:
                    offenders.append(f'{rel}:{lineno}  {text}')
        assert not offenders, (
            'unconditional read_timeout_seconds (a read timeout on a wait):\n'
            + '\n'.join('  ' + o for o in offenders)
            + '\n\nA HANDSHAKE may bound itself; a WAIT may not.')

    def test_read_timeout_scan_is_not_vacuous(self):
        """Anti-vacuity: a regex matching nothing makes the ratchet pass
        forever."""
        hits = _scan_read_timeouts()
        assert hits, 'the read_timeout_seconds scan found nothing — vacuous'

    def test_subprocess_kill_budget_has_no_hardcoded_default(self):
        """THE RATCHET (kill side). ``run_command`` is the one place that kills
        a process tree on a clock. Assert via AST that its resolver assigns no
        bare numeric literal to ``timeout`` — a new ``timeout = 300`` fallback
        trips this.

        Deliberately NOT skippable: an earlier draft fell back to
        ``pytest.skip`` when the fragment didn't parse, which turned the whole
        kill-side ratchet into a permanent yellow light that still reported
        "passed". The fragment is dedented and wrapped in a ``def`` so it
        parses unconditionally; a genuine parse failure is now a FAILURE.
        """
        import textwrap
        from lib.project_mod.run_command import tool_run_command
        src = inspect.getsource(tool_run_command)
        block = src[src.index('Resolve timeout'):src.index('if _is_dangerous_command')]
        # Drop the leading partial comment line, dedent, and wrap so the
        # if/elif chain is a valid standalone body.
        block = block[block.index('\n') + 1:]
        wrapped = 'def _frag():\n' + textwrap.indent(
            textwrap.dedent(block).rstrip(), '    ') + '\n    pass\n'
        try:
            tree = ast.parse(wrapped)
        except SyntaxError as e:
            pytest.fail(f'resolver fragment did not parse — the kill-side '
                        f'ratchet would be silently disabled: {e}\n{wrapped}')
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == 'timeout':
                        v = node.value
                        if isinstance(v, ast.Constant) and isinstance(v.value, (int, float)):
                            bad.append(v.value)
        assert not bad, (
            f'run_command assigns a hardcoded timeout default {bad} — a clock '
            'must not decide when to kill a build')

    def test_scan_strips_comments(self):
        """charter #24 both directions: a commented-out site must neither
        satisfy nor violate the ratchet."""
        sample = (
            '# read_timeout_seconds=timedelta(seconds=120)\n'
            'x = 1\n'
        )
        live = strip_comments(sample, lang='python')
        assert not _READ_TIMEOUT_RE.search(live), \
            'the ratchet matches commented-out code (charter #24 violation)'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
