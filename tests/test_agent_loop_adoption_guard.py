"""Guard: NEW agentic capabilities ride lib/agent_loop.run_agent_loop.

WHY
---
Charter decision (owner, 2026-07-27, "Agent 能力复用铁律"): this project is
no longer a pure conversational agent — new agent-driven features MUST be
built on the shared chassis (``run_agent_loop`` + ``AbortSignal``), and new
PRIVATE multi-round tool-calling loops / private abort plumbing are
forbidden. History shows why: ``agent_verdict`` was hand-copied 4× before
being forced into one module; a decision without a ratchet is dead text
within three months.

This suite is the RATCHET, in three parts:

1. AST heuristic — two shapes, either one convicts:
   (a) DIRECT: a ``while`` loop whose body BOTH calls an LLM turn
       (``dispatch_stream`` / ``stream_llm_response`` / …, underscore
       prefixes normalized) AND handles tool calls (``tool_calls`` /
       ``execute_tool`` / …) — the classic copy-paste agent loop.
   (b) DELEGATED: a ``while`` loop whose body hand-checks abort
       (``task['aborted']`` / ``task.get('aborted')`` / ``abort_check()``)
       AND obtains its LLM turn from a helper call (name containing
       ``turn`` / ``dispatch`` / ``llm``) — the endpoint driver shape,
       where the LLM call itself hides inside a helper and shape (a)
       is blind to it.
   Finding either in any tracked file outside the grandfathered set
   fails the build.
2. Grandfather signatures — the REMAINING pre-decision private loops
   (chat orchestrator, endpoint driver) are pinned by a file-specific
   code token PLUS the absence of a ``run_agent_loop`` import. Migrating
   one onto the chassis breaks its pin and turns this test red until the
   entry is removed — the list only shrinks. (swarm was the first
   grandfather to migrate out, 2026-07-27.)
3. Adoption ratchet — the number of files importing ``run_agent_loop`` must
   never decrease (currently 9: paper report/qa/survey/insight/ideate/
   recommend, scheduler timer, motion-video scene author, swarm agent).

NEUTER evidence (manual):
  * a probe file under lib/ containing ``while True: … dispatch_stream(…)``
    + ``msg['tool_calls']`` handling turns test 1 red naming the file:line;
  * adding ``from lib.agent_loop import run_agent_loop`` to a grandfathered
    file turns test 2 red (import detected) until the entry is removed.
"""

from __future__ import annotations

import ast
import os
import re
import unittest

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

# Calls that count as "an LLM turn" for the loop heuristic. Leading
# underscores are stripped before matching (``self._dispatch_stream`` ==
# ``dispatch_stream``).
_LLM_CALL_NAMES = frozenset({
    'dispatch_stream', 'dispatch_chat', 'async_dispatch_stream',
    'astream', 'chat', 'stream', 'chat_stream', 'stream_llm_response',
})

# Tokens that count as "tool-call handling" inside the same loop body.
_TOOL_TOKENS = ('tool_calls', 'execute_tool', 'tool_call_id')

# Grandfathered private agent loops, each verified by audit (2026-07-27):
#   * orchestrator/_run.py:520  — run_task's while (premature-retry ceiling);
#     migration blocked on pt_03f4cdf1 (~30 cross-iteration locals).
#   * endpoint/_run.py:220      — Planner→Worker→Critic driver while; its
#     worker turn delegates to _run_single_turn (nested run_task), so the
#     DIRECT heuristic cannot see it — the DELEGATED shape catches it, and
#     this signature pins it too.
# (REMOVED 2026-07-27: 'lib/swarm/agent.py' — migrated onto the chassis;
# its abort_check callback shape rode AbortSignal.from_callback as
# predicted. See tests/test_swarm_agent_loop_chassis.py.)
# Each entry: relpath -> a code token that uniquely identifies the private
# loop. When the loop migrates onto run_agent_loop, the token disappears /
# the import appears and the pin goes red — remove the entry then.
_GRANDFATHERED = {
    'lib/tasks_pkg/orchestrator/_run.py':
        'while round_num + 1 <= max_tool_rounds + _premature_retry_count',
    'lib/tasks_pkg/endpoint/_run.py':
        '_run_single_turn(task,',
}

# Files allowed to trip the heuristic besides the grandfathered set: the
# chassis itself and low-level LLM/dispatcher internals (their loops are
# retry/stream plumbing, not agent loops).
_HEURISTIC_EXEMPT = frozenset(_GRANDFATHERED) | frozenset({
    'lib/agent_loop.py',
    'lib/llm/stream.py',
    'lib/llm/astream.py',
    'lib/llm/_sse_core.py',
    'lib/llm/chat.py',
    'lib/llm_dispatch/api.py',
    'lib/llm_dispatch/dispatcher.py',
})

# Minimum number of tracked files that must import run_agent_loop. Only
# grows — a removal means an adopter was reverted to a private loop (or the
# file was deleted), both of which need a conscious test edit.
_MIN_LOOP_IMPORTERS = 9


def _py_files():
    """Tracked Python files under lib/ + routes/.

    Enumerated via ``git ls-files`` (the repo index), NOT os.walk: walking
    the tree stats every untracked artefact on this FUSE mount and takes
    minutes, while the index answers in milliseconds and covers exactly the
    files the ratchet must police (anything committable).
    """
    import subprocess
    out = subprocess.check_output(
        ['git', 'ls-files', 'lib/*.py', 'routes/*.py'],
        cwd=ROOT, text=True)
    return [os.path.join(ROOT, p) for p in out.split()]


def _call_name(node: ast.Call) -> str:
    func = node.func
    name = ''
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    return name.lstrip('_')


# Call names (underscore prefixes stripped) that indicate a delegated
# LLM-turn helper in the DELEGATED shape (e.g. ``_run_single_turn``).
_TURN_HELPER_RE = re.compile(r'turn|dispatch|llm')


def _is_abort_handcheck(sub: ast.AST) -> bool:
    """A manual abort poll: task['aborted'] / task.get('aborted') /
    abort_check() — NOT an AbortSignal predicate read."""
    if isinstance(sub, ast.Subscript) \
            and isinstance(sub.slice, ast.Constant) \
            and sub.slice.value == 'aborted':
        return True
    if isinstance(sub, ast.Call):
        if isinstance(sub.func, ast.Attribute):
            if sub.func.attr == 'get' and sub.args \
                    and isinstance(sub.args[0], ast.Constant) \
                    and sub.args[0].value == 'aborted':
                return True
            if sub.func.attr.lstrip('_') == 'abort_check':
                return True
        elif isinstance(sub.func, ast.Name) \
                and sub.func.id.lstrip('_') == 'abort_check':
            return True
    return False


def _while_is_agent_loop(node: ast.While) -> bool:
    """Convicting shapes (see module docstring): (a) DIRECT = LLM call +
    tool handling; (b) DELEGATED = abort hand-check + turn-helper call."""
    has_llm_call = False
    has_tool_handling = False
    has_abort_handcheck = False
    has_turn_helper = False
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _call_name(sub) in _LLM_CALL_NAMES:
            has_llm_call = True
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                and any(tok in sub.value for tok in _TOOL_TOKENS):
            has_tool_handling = True
        elif isinstance(sub, ast.Attribute) \
                and any(tok in sub.attr for tok in _TOOL_TOKENS):
            has_tool_handling = True
        if _is_abort_handcheck(sub):
            has_abort_handcheck = True
        elif isinstance(sub, ast.Call) \
                and _TURN_HELPER_RE.search(_call_name(sub)):
            has_turn_helper = True
        if (has_llm_call and has_tool_handling) \
                or (has_abort_handcheck and has_turn_helper):
            return True
    return False


def _iter_agent_loops():
    """Yield (relpath, lineno) for every while-loop that looks agentic."""
    for path in _py_files():
        with open(path, encoding='utf-8') as f:
            src = f.read()
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError:
            continue
        rel = os.path.relpath(path, ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.While) and _while_is_agent_loop(node):
                yield rel, node.lineno


def _imports_run_agent_loop(src: str) -> bool:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) \
                and node.module == 'lib.agent_loop' \
                and any(a.name == 'run_agent_loop' for a in node.names):
            return True
    return False


def _loop_importer_files():
    """Tracked lib/routes files that import run_agent_loop."""
    importers = []
    for path in _py_files():
        with open(path, encoding='utf-8') as f:
            src = f.read()
        if _imports_run_agent_loop(src):
            importers.append(os.path.relpath(path, ROOT))
    return importers


class TestPrivateAgentLoopRatchet(unittest.TestCase):

    def test_no_new_private_agent_loops(self):
        violations = []
        for rel, lineno in _iter_agent_loops():
            if rel in _HEURISTIC_EXEMPT:
                continue
            violations.append(
                f'{rel}:{lineno}: private agent loop (while + LLM '
                'dispatch + tool handling) — new agentic capabilities '
                'MUST ride lib/agent_loop.run_agent_loop '
                '(charter 2026-07-27); see docs/AGENT_CAPABILITY_GUIDE.md')
        self.assertEqual(violations, [], '\n'.join(violations))

    def test_heuristic_actually_finds_a_loop(self):
        """Guard against a silently empty scan (AST heuristic drift): the
        endpoint DELEGATED loop MUST be detected."""
        found = set(rel for rel, _ in _iter_agent_loops())
        # NOTE: the orchestrator's while loop survived the deliberate slice
        # refactors only as a SHELL — the LLM turn and tool handling moved
        # into helpers, so the DIRECT heuristic can no longer see it (its
        # grandfather pin in _GRANDFATHERED still guards the shell token).
        # The endpoint driver loop is the only heuristic-detectable private
        # loop left; losing it means the scan silently sees nothing.
        self.assertIn(
            'lib/tasks_pkg/endpoint/_run.py', found,
            'endpoint driver loop no longer detected — the DELEGATED '
            'heuristic (abort hand-check + turn-helper) may be broken '
            '(or endpoint was migrated: then relax this pin deliberately)')

    def test_grandfathered_loops_still_pinned(self):
        stale = []
        for rel, token in _GRANDFATHERED.items():
            path = os.path.join(ROOT, rel)
            if not os.path.exists(path):
                stale.append(f'{rel}: file gone — remove its entry')
                continue
            with open(path, encoding='utf-8') as f:
                src = f.read()
            if _imports_run_agent_loop(src):
                stale.append(
                    f'{rel}: now imports run_agent_loop — migration '
                    'landed, REMOVE its _GRANDFATHERED entry (the list '
                    'only shrinks)')
            elif token not in src:
                stale.append(
                    f'{rel}: private-loop signature {token!r} gone but no '
                    'run_agent_loop import — the loop changed shape; '
                    're-audit and update or remove the pin deliberately')
        self.assertEqual(stale, [], '\n'.join(stale))

    def test_loop_adoption_never_regresses(self):
        importers = _loop_importer_files()
        self.assertGreaterEqual(
            len(importers), _MIN_LOOP_IMPORTERS,
            f'run_agent_loop importers dropped to {len(importers)} '
            f'(< {_MIN_LOOP_IMPORTERS}): {importers} — an adopter was '
            'reverted to a private loop or deleted; restore it or raise '
            'the floor deliberately')


if __name__ == '__main__':
    unittest.main(verbosity=2)
