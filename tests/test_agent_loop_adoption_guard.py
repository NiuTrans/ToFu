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
       ``turn`` / ``dispatch`` / ``llm``) — the delegated driver shape,
       where the LLM call itself hides inside a helper and shape (a)
       is blind to it.
   Finding either in any tracked file outside the grandfathered set
   fails the build.
2. Detector self-test — synthetic direct and delegated loop shapes prove the
   AST heuristic still fires without requiring a forbidden production loop to
   remain in the repository as its own test fixture.
3. Adoption ratchet — the number of files importing ``run_agent_loop`` must
   never decrease (currently 6; the seven Paper workflow call sites were
   deliberately consolidated into ``lib/paper/agent_loop_policy.py``, which
   still rides ``run_agent_loop`` through ``run_guarded_paper_agent_loop``).

NEUTER evidence (manual):
  * a probe file under lib/ containing ``while True: … dispatch_stream(…)``
    + ``msg['tool_calls']`` handling turns test 1 red naming the file:line;
  * removing either synthetic detector shape turns test 2 red.
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

# Files allowed to trip the heuristic: the chassis itself and low-level
# LLM/dispatcher internals (their loops are
# retry/stream plumbing, not agent loops).
_HEURISTIC_EXEMPT = frozenset({
    'lib/agent_loop.py',
    'lib/llm/stream.py',
    'lib/llm/astream.py',
    'lib/llm/_sse_core.py',
    'lib/llm/chat.py',
    'lib/llm_dispatch/api.py',
    'lib/llm_dispatch/_api_chat.py',
    'lib/llm_dispatch/_api_stream.py',
    'lib/llm_dispatch/_api_stream_state.py',
    'lib/llm_dispatch/_api_multi.py',
    'lib/llm_dispatch/_api_budget.py',
    'lib/llm_dispatch/_api_contention.py',
    'lib/llm_dispatch/_api_hygiene.py',
    'lib/llm_dispatch/_api_errors.py',
    'lib/llm_dispatch/dispatcher.py',
})

# Minimum number of tracked files that must import run_agent_loop. Only
# grows — a removal means an adopter was reverted to a private loop (or the
# file was deleted), both of which need a conscious test edit.
#
# 2026-08-30: floor lowered 12 -> 6 deliberately. The Paper workflows
# (deepen/ideate/qa/recommend/report/survey/_synthesize) stopped importing
# run_agent_loop directly and now import run_guarded_paper_agent_loop from
# lib/paper/agent_loop_policy.py, which is itself counted below and wraps
# run_agent_loop. This is a consolidation of adopters, not a reversion to a
# private loop (test_no_new_private_agent_loops still guards that shape).
_MIN_LOOP_IMPORTERS = 6


def _py_files():
    """Tracked, present Python files under lib/ + routes/.

    Enumerated via ``git ls-files`` (index + non-ignored untracked files), NOT
    os.walk: walking the tree stats every artefact on this FUSE mount and takes
    minutes. Including new files is important during a pre-commit migration:
    otherwise the new adopter is invisible until somebody stages it.
    """
    import subprocess
    out = subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard',
         'lib/*.py', 'routes/*.py'],
        cwd=ROOT, text=True)
    # ``git ls-files`` retains index entries for unstaged deletions.  Those
    # files are absent from the current executable tree and must not turn a
    # loop census into FileNotFoundError halfway through a migration.
    paths = [os.path.join(ROOT, p) for p in out.split()]
    return [path for path in paths if os.path.isfile(path)]


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

    def test_heuristic_recognizes_direct_and_delegated_shapes(self):
        """The detector stays live without preserving a production violation."""
        direct = ast.parse('''
def run():
    while True:
        message = dispatch_stream()
        if message["tool_calls"]:
            execute_tool(message)
''')
        delegated = ast.parse('''
def run(task):
    while not task.get("aborted"):
        run_single_turn()
''')
        harmless = ast.parse('''
def run(items):
    while items:
        items.pop()
''')

        for tree in (direct, delegated):
            loop = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.While))
            self.assertTrue(_while_is_agent_loop(loop))
        harmless_loop = next(node for node in ast.walk(harmless)
                             if isinstance(node, ast.While))
        self.assertFalse(_while_is_agent_loop(harmless_loop))

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
