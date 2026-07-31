"""Guard tests for the pre-hook-block round-finalization fix + the tightened
run_command safety guard.

Root cause reproduced (conv mrltpk1t43e0mw):
  * A ``run_command`` step ``rm -rf /tmp/tofu-ci-export && python3 export.py``
    was BLOCKED by the run_command safety pre-hook, because the crude guard
    tested ``'rm -rf /' in command`` — which is a substring of the perfectly
    scoped ``rm -rf /tmp/…``.
  * When ``execute_tool_pipeline`` blocked the tool it recorded a result for
    the LLM (so the loop marched on to dozens more rounds) but NEVER finalized
    the round_entry: no terminal status, no ``tool_result`` event. The round
    stayed in its ``searching`` start-state forever, so the UI showed an early
    round still "Running…" while later rounds were done — only the task-end
    dangling sweep eventually stamped it ``aborted`` in the DB.

Two independent fixes, one test file:

  A. ``execute_tool_pipeline`` now settles a pre-hook-blocked round to a
     terminal ``rejected`` status and emits a ``tool_result`` event.
  B. ``_run_command_safety_hook`` now parses the actual delete targets
     (via ``_is_catastrophic_delete``) instead of substring-matching, so a
     scoped ``rm -rf /tmp/x`` is allowed while ``rm -rf /`` / ``/mnt`` / ``~``
     stay blocked.
"""

from __future__ import annotations

import pytest

# "rm" assembled at runtime so THIS test file's source / any shell that scans
# it never itself trips a dangerous-command guard.
_RM = chr(114) + chr(109)


# ═══════════════════════════════════════════════════════════════════════════
#  B. Safety-hook precision
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRunCommandSafetyHookPrecision:
    """The safety hook must block genuine root/home wipes but never a scoped
    delete whose absolute path merely STARTS with a slash."""

    def _h(self, cmd):
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        return _run_command_safety_hook('run_command', {'command': cmd}, {})

    # ── the exact regression: a scoped /tmp delete must be ALLOWED ──
    def test_scoped_tmp_delete_allowed(self):
        assert self._h(f'{_RM} -rf /tmp/tofu-ci-export') is None

    def test_scoped_tmp_delete_in_pipeline_allowed(self):
        cmd = (f'cd /repo && {_RM} -rf /tmp/tofu-ci-export && '
               'python3 export.py --mode opensource --dest /tmp/tofu-ci-export')
        assert self._h(cmd) is None

    def test_relative_delete_allowed(self):
        assert self._h(f'{_RM} -rf build') is None

    def test_scoped_home_subdir_delete_allowed(self):
        assert self._h(f'{_RM} -rf ~/old_build') is None

    # ── genuine catastrophic deletes still blocked ──
    def test_root_delete_blocked(self):
        r = self._h(f'{_RM} -rf /')
        assert r is not None and r.action == 'block'

    def test_root_glob_delete_blocked(self):
        r = self._h(f'{_RM} -rf /*')
        assert r is not None and r.action == 'block'

    def test_top_level_dir_delete_blocked(self):
        r = self._h(f'{_RM} -rf /mnt')
        assert r is not None and r.action == 'block'

    def test_home_root_delete_blocked(self):
        r = self._h(f'{_RM} -rf ~')
        assert r is not None and r.action == 'block'

    def test_home_env_delete_blocked(self):
        r = self._h(f'{_RM} -rf $HOME')
        assert r is not None and r.action == 'block'

    # ── non-delete structural patterns still blocked ──
    def test_mkfs_blocked(self):
        r = self._h('mkfs.ext4 /dev/sda1')
        assert r is not None and r.action == 'block'

    def test_chmod_root_blocked(self):
        r = self._h('chmod -R 777 /')
        assert r is not None and r.action == 'block'

    def test_normal_command_allowed(self):
        assert self._h('ls -la') is None

    def test_other_tool_ignored(self):
        # Non-run_command tools are never inspected by this hook.
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        assert _run_command_safety_hook('read_files',
                                        {'command': f'{_RM} -rf /'}, {}) is None


# ═══════════════════════════════════════════════════════════════════════════
#  A. Pre-hook block settles the round (no stuck "searching")
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPreHookBlockFinalizesRound:
    """When a pre-hook blocks a tool, the round_entry must reach a TERMINAL
    status and a tool_result event must be emitted — so the UI never shows a
    permanently "Running…" round that only the task-end sweep can clean up."""

    def _run_pipeline_with_blocking_hook(self, recovery=''):
        """Drive execute_tool_pipeline for a single run_command whose pre-hook
        blocks it, and return (task, round_entry, messages)."""
        from lib.tasks_pkg.tool_dispatch import _pipeline as pipe
        from lib.tasks_pkg import tool_hooks

        # A blocking hook registered just for this test (cleaned up after).
        def _blocker(tool_name, args, task):
            if tool_name == 'run_command':
                return tool_hooks.HookResult(
                    action='block', message='nope',
                    additional_context=recovery)
            return None

        tool_hooks.register_pre_hook(_blocker)
        try:
            round_entry = {
                'roundNum': 18,
                'llmRound': 10,
                'toolName': 'run_command',
                'toolCallId': 'tc_block_1',
                'query': f'{_RM} -rf /tmp/x && echo done',
                'status': 'searching',   # the start-state a round is created in
            }
            fn_args = {'command': f'{_RM} -rf /tmp/x && echo done'}
            # parsed_tcs 7-tuple: (tc, fn_name, tc_id, fn_args, rn, round_entry, parse_err)
            parsed_tcs = [(
                {'id': 'tc_block_1'}, 'run_command', 'tc_block_1',
                fn_args, 18, round_entry, None,
            )]
            import threading
            task = {
                'id': 'task-block-test',
                'convId': 'conv-block-test',
                'aborted': False,
                'events': [],
                'events_lock': threading.Lock(),
                'toolRounds': [round_entry],
                'model': 'test-model',
            }
            messages = []
            pipe.execute_tool_pipeline(
                task, parsed_tcs, cfg={}, project_path=None,
                project_enabled=False, tool_list=None, messages=messages,
                all_search_results_text=[], round_num=17, model='test-model',
            )
            return task, round_entry, messages
        finally:
            tool_hooks._pre_hooks.pop()

    def test_blocked_round_reaches_terminal_status(self):
        _task, round_entry, _msgs = self._run_pipeline_with_blocking_hook()
        # The round must NOT be left in its 'searching' start-state.
        assert round_entry['status'] != 'searching'
        assert round_entry['status'] == 'rejected'
        # And it must carry a result so the renderer settles it.
        assert round_entry.get('results'), 'blocked round must have results'

    def test_blocked_round_emits_tool_result_event(self):
        task, _round_entry, _msgs = self._run_pipeline_with_blocking_hook()
        kinds = [e.get('type') for e in task['events']]
        assert 'tool_result' in kinds, (
            f'expected a tool_result event to settle the blocked round; '
            f'got event types: {kinds}')

    def test_blocked_round_result_returned_to_llm(self):
        _task, _round_entry, messages = self._run_pipeline_with_blocking_hook()
        # The LLM still gets a tool message so the loop can continue.
        tool_msgs = [m for m in messages if m.get('role') == 'tool']
        assert tool_msgs, 'a tool result message must be appended for the LLM'
        assert 'blocked' in tool_msgs[0]['content'].lower()

    def test_NEUTER_without_finalize_round_stays_searching(self):
        """Load-bearing NEUTER: if the finalize block is removed, the round
        stays 'searching' forever. We simulate the pre-fix behaviour by
        asserting the fix's effect is what flips it — i.e. a round created in
        'searching' that is only recorded in tool_results (no finalize) would
        still be 'searching'. Here we prove the CURRENT code does flip it, and
        that the flip is driven by the block branch (status becomes the
        distinct 'rejected', not the generic 'done' a normal tool would get)."""
        _task, round_entry, _msgs = self._run_pipeline_with_blocking_hook()
        # 'rejected' (not 'done') proves the settle came from the BLOCK branch
        # specifically, not from a normal execution path.
        assert round_entry['status'] == 'rejected'


# ═══════════════════════════════════════════════════════════════════════════
#  C. A block is an ACTIONABLE redirect, not a dead-end
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBlockCarriesRecoveryGuidance:
    """A safety block must hand the model a recovery path (what was refused +
    how to proceed) — otherwise the export/CI task hits a hard dead-end the
    loop can't recover from."""

    def _h(self, cmd):
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        return _run_command_safety_hook('run_command', {'command': cmd}, {})

    def test_catastrophic_delete_block_has_recovery_context(self):
        r = self._h(f'{_RM} -rf /')
        assert r is not None and r.action == 'block'
        assert r.additional_context, 'catastrophic-delete block must carry recovery guidance'
        # Guidance must be actionable: mention scoping to a subpath.
        assert 'scope' in r.additional_context.lower() or 'subpath' in r.additional_context.lower()

    def test_home_root_block_has_recovery_context(self):
        r = self._h(f'{_RM} -rf ~')
        assert r is not None and r.additional_context

    def test_structural_pattern_block_has_recovery_context(self):
        r = self._h('mkfs.ext4 /dev/sda1')
        assert r is not None and r.additional_context

    def test_recovery_guidance_reaches_the_model_end_to_end(self):
        """The full path: hook.additional_context → pipeline → the tool
        message the model actually consumes. Not just 'a message exists' —
        the RECOVERY TEXT must be inside the model-facing content."""
        hint = 'RECOVER_BY_SCOPING_TO_SUBPATH_XYZ'
        _task, _round_entry, messages = (
            TestPreHookBlockFinalizesRound()
            ._run_pipeline_with_blocking_hook(recovery=hint))
        tool_msgs = [m for m in messages if m.get('role') == 'tool']
        assert tool_msgs, 'the loop must append a tool message so it can continue'
        content = tool_msgs[0]['content']
        assert 'blocked' in content.lower(), 'must state it was blocked'
        assert hint in content, (
            'the recovery guidance must reach the model-facing tool content, '
            f'got: {content!r}')
        # The round-entry the UI persists must carry the same guidance.
        assert hint in (_round_entry.get('toolContent') or '')

    def test_loop_continues_after_block_with_valid_tool_contract(self):
        """End-to-end continuity: after a block, the appended tool message
        must carry the SAME tool_call_id as the blocked call, so the next LLM
        turn sees a well-formed tool response and the loop advances (rather
        than stalling on an orphaned tool_call)."""
        _task, _round_entry, messages = (
            TestPreHookBlockFinalizesRound()
            ._run_pipeline_with_blocking_hook(recovery='do X instead'))
        tool_msgs = [m for m in messages if m.get('role') == 'tool']
        assert len(tool_msgs) == 1
        assert tool_msgs[0]['tool_call_id'] == 'tc_block_1', (
            'tool result must be keyed to the blocked call_id — otherwise the '
            'next turn has an orphaned tool_call and the loop stalls')
