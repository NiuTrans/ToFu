"""tests/test_chat_mode.py — Two-tier chat mode (chat/studio).

Pins the backend half of the single-source-of-truth contract:

  * ``chat_mode.apply_chat_mode`` expands a declared tier into atomic flags,
    tier defaults OVERRIDE explicit flags, absent tier = pass-through.
  * The ``chat`` tier keeps the FULL default tool set (memory/todo/scheduler +
    code execution on) — it is the everyday all-rounder (formerly ``pro``).
  * ``studio`` ⟺ a project is attached (project tools present).
  * Legacy tier codes ``air`` / ``pro`` persisted in old conversations
    normalise forward to ``chat`` so they load unchanged.
  * ``is_lean_mode`` is always False today (the lean ``air`` tier was merged
    away) but the seam is retained for a future auto-retract feature.

The FE↔BE table equality is guarded separately by
``tests/test_chat_mode_parity.py``.
"""

from __future__ import annotations

import unittest

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.chat_mode import (
    CHAT_MODES,
    DEFAULT_CHAT_MODE,
    apply_chat_mode,
    chat_mode_defaults,
    is_lean_mode,
    normalize_chat_mode,
)
from lib.tasks_pkg.model_config import _assemble_tool_list


def _names(tool_list):
    return [t['function']['name'] for t in (tool_list or [])]


def _assemble(cfg, **kw):
    """Assemble via the public shim with sensible defaults for a chat turn.

    Mirrors the production path: the resolver expands the tier
    (``apply_chat_mode``) into atomic flags BEFORE assembling, so the derived
    ``code_exec_enabled`` etc. flow through exactly as ``_resolve_model_config``
    would produce them.
    """
    cfg = apply_chat_mode(cfg)
    base = dict(
        project_path=cfg.get('projectPath', ''),
        project_enabled=bool(cfg.get('projectPath')),
        task_id='t-mode', search_mode=cfg.get('searchMode', 'multi'),
        search_enabled=cfg.get('searchMode', 'multi') in ('single', 'multi'),
        fetch_enabled=cfg.get('fetchEnabled', True),
        code_exec_enabled=cfg.get('codeExecEnabled', False),
        browser_enabled=False, desktop_enabled=False,
        messages=[],
    )
    base.update(kw)
    return _assemble_tool_list(cfg, **base)


class TestNormalize(unittest.TestCase):
    def test_known_modes(self):
        for m in CHAT_MODES:
            self.assertEqual(normalize_chat_mode({'chatMode': m}), m)

    def test_default_is_chat(self):
        self.assertEqual(DEFAULT_CHAT_MODE, 'chat')
        self.assertIn('chat', CHAT_MODES)

    def test_case_and_whitespace(self):
        self.assertEqual(normalize_chat_mode({'chatMode': ' Chat '}), 'chat')

    def test_legacy_air_pro_normalize_to_chat(self):
        # Old conversations persisted air/pro; both merge forward to chat so
        # they load without error and get the everyday tool set.
        self.assertEqual(normalize_chat_mode({'chatMode': 'air'}), 'chat')
        self.assertEqual(normalize_chat_mode({'chatMode': 'pro'}), 'chat')
        self.assertEqual(normalize_chat_mode({'chatMode': ' PRO '}), 'chat')

    def test_absent_and_invalid_are_none(self):
        self.assertIsNone(normalize_chat_mode({}))
        self.assertIsNone(normalize_chat_mode({'chatMode': 'turbo'}))
        self.assertIsNone(normalize_chat_mode(None))


class TestApply(unittest.TestCase):
    def test_absent_mode_is_passthrough(self):
        cfg = {'searchMode': 'off', 'codeExecEnabled': True}
        self.assertEqual(apply_chat_mode(cfg), cfg)

    def test_chat_enables_full_tool_set(self):
        out = apply_chat_mode({'chatMode': 'chat'})
        self.assertTrue(out['codeExecEnabled'])
        self.assertTrue(out['memoryEnabled'])
        self.assertEqual(out['searchMode'], 'multi')

    def test_legacy_pro_expands_like_chat(self):
        # A stored 'pro' must produce the SAME flags as 'chat'.
        self.assertEqual(
            {k: v for k, v in apply_chat_mode({'chatMode': 'pro'}).items()
             if k != 'chatMode'},
            {k: v for k, v in apply_chat_mode({'chatMode': 'chat'}).items()
             if k != 'chatMode'},
        )
        self.assertEqual(apply_chat_mode({'chatMode': 'pro'})['chatMode'], 'chat')

    def test_studio_leaves_code_exec_alone(self):
        # studio does NOT pin codeExec (run_command supersedes it downstream).
        out = apply_chat_mode({'chatMode': 'studio'})
        self.assertNotIn('codeExecEnabled', chat_mode_defaults('studio'))
        self.assertTrue(out['memoryEnabled'])

    def test_input_not_mutated(self):
        cfg = {'chatMode': 'chat'}
        apply_chat_mode(cfg)
        self.assertEqual(cfg, {'chatMode': 'chat'})


class TestLeanGate(unittest.TestCase):
    def test_is_lean_always_false(self):
        # The lean 'air' tier was merged away — no tier is lean today. The seam
        # remains for a future auto-retract-tools feature.
        self.assertFalse(is_lean_mode('chat'))
        self.assertFalse(is_lean_mode('studio'))
        self.assertFalse(is_lean_mode('air'))
        self.assertFalse(is_lean_mode('pro'))
        self.assertFalse(is_lean_mode(None))

    def test_chat_keeps_default_capability_tools(self):
        tl, has_real = _assemble({'chatMode': 'chat', 'mcpEnabled': False})
        names = _names(tl)
        self.assertTrue(has_real)
        # Base tools present.
        self.assertIn('web_search', names)
        self.assertIn('fetch_url', names)
        self.assertIn('read_files', names)
        # Everyday capability tools stay on (nothing is dropped anymore).
        self.assertIn('create_memory', names)
        self.assertIn('todo_write', names)
        # Scheduler remains task-authorized but is routed through Tool Search
        # under the default native-exposure experiment, so it need not spend
        # tokens on the initial wire surface.
        from lib.tools.registry import all_specs
        scheduler = next(spec for spec in all_specs() if spec.key == 'scheduler')
        self.assertIn('schedule_create', scheduler.provides)
        self.assertEqual(scheduler.discovery_policy, 'searchable')
        # chat enables code exec (no project) → the standalone code-exec tool,
        # whose function name is 'run_command' (CODE_EXEC_TOOL is a copy of
        # PROJECT_TOOL_RUN_COMMAND). The project-ONLY tools stay absent.
        self.assertIn('run_command', names)
        self.assertNotIn('grep_search', names)
        self.assertNotIn('write_file', names)

    def test_legacy_air_gets_full_tool_set(self):
        # A stored 'air' conv now loads with the full chat tool set (no longer
        # a stripped-down tier).
        tl, _ = _assemble({'chatMode': 'air', 'mcpEnabled': False})
        names = _names(tl)
        self.assertIn('create_memory', names)
        self.assertIn('todo_write', names)
        self.assertIn('run_command', names)

    def test_studio_has_project_tools_not_code_exec(self):
        tl, _ = _assemble({'chatMode': 'studio', 'projectPath': '/tmp/x',
                              'mcpEnabled': False})
        names = _names(tl)
        # Project family present; run_command supersedes code_exec.
        self.assertIn('run_command', names)
        self.assertIn('grep_search', names)
        self.assertNotIn('code_exec', names)
        # Everyday capability tools still on in studio.
        self.assertIn('create_memory', names)

    def test_no_chatmode_is_unchanged_legacy(self):
        # A legacy caller with no chatMode keeps memory/todo/scheduler
        # (has_base_tools path) — proves the pass-through is intact.
        tl, _ = _assemble({'mcpEnabled': False})
        names = _names(tl)
        self.assertIn('create_memory', names)
        self.assertIn('todo_write', names)


if __name__ == '__main__':
    unittest.main(verbosity=2)
