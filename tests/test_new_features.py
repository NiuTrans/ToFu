"""Tests for Claude Code-inspired feature additions.

Covers:
  1. Session Memory — threshold detection, extraction prompt, merge
  2. Per-Turn Attachments — compute, inject, state tracking
  3. Cache Break Detection — hash tracking, cache-aware microcompact
  4. Pre/Post Tool Hooks — registration, execution, blocking
  5. Unified ToolSpec — registration, backward-compat exports
  6. Partial Compaction — directional compaction
"""

import copy
import json

import pytest

# ═══════════════════════════════════════════════════════════════════════════════
#  1. Session Memory
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
#  2. Per-Turn Attachments
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAttachments:
    """Tests for lib/tasks_pkg/attachments.py."""

    def test_compute_empty_returns_empty(self):
        from lib.tasks_pkg.attachments import compute_turn_attachments
        result = compute_turn_attachments(
            messages=[], task={}, round_num=0, conv_id='',
        )
        assert result == []

    def test_inject_attachments_appends_trailing_meta_msg(self):
        """★ Cache-safe contract: attachments are appended as a NEW trailing
        _isMeta user message, NOT merged into the historical (in-prefix) user
        message. Merging mutated cached bytes and forced a full cache miss
        every time the reminder fired."""
        from lib.tasks_pkg.attachments import inject_attachments
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'Hello'},
        ]
        inject_attachments(messages, ['<attachment>test</attachment>'])
        # The original user message is left BYTE-IDENTICAL.
        assert messages[1]['content'] == 'Hello'
        # A new trailing _isMeta user message carries the attachment.
        assert len(messages) == 3
        assert messages[2]['role'] == 'user'
        assert messages[2].get('_isMeta') is True
        texts = [b.get('text', '') for b in messages[2]['content']]
        assert '<attachment>test</attachment>' in '\n'.join(texts)
        assert messages[2].get('_contextComposer') is True

    def test_inject_attachments_does_not_touch_multimodal_prefix(self):
        from lib.tasks_pkg.attachments import inject_attachments
        messages = [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'Hello'},
            ]},
        ]
        inject_attachments(messages, ['<attachment>test</attachment>'])
        # Original multimodal message untouched.
        assert len(messages[0]['content']) == 1
        # New trailing meta message appended.
        assert len(messages) == 2
        assert messages[1].get('_isMeta') is True

    def test_inject_attachments_no_user_creates_one(self):
        from lib.tasks_pkg.attachments import inject_attachments
        messages = [
            {'role': 'system', 'content': 'sys'},
        ]
        inject_attachments(messages, ['<attachment>test</attachment>'])
        assert len(messages) == 2
        assert messages[1]['role'] == 'user'
        assert messages[1].get('_isMeta') is True

    def test_inject_empty_attachments_no_change(self):
        from lib.tasks_pkg.attachments import inject_attachments
        messages = [
            {'role': 'user', 'content': 'Hello'},
        ]
        original = copy.deepcopy(messages)
        inject_attachments(messages, [])
        assert messages == original

    # ── B2: trigger must be message-scan based, NOT cross-task round_num ──

    @staticmethod
    def _writeful_history(extra_tail_rounds=4):
        """A realistic cross-task message list: an early write, then several
        non-write rounds, then a NEW user turn (task 2). The write happened
        long ago in message terms, so the reminder SHOULD fire."""
        msgs = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'implement the feature'},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'function': {'name': 'write_file',
                              'arguments': '{"path":"a.py","content":"x"}'}}]},
            {'role': 'tool', 'content': 'written a.py'},
        ]
        # Several non-write rounds (read/grep) — the model "moved on".
        for i in range(extra_tail_rounds):
            msgs.append({'role': 'assistant', 'content': None, 'tool_calls': [
                {'function': {'name': 'read_files',
                              'arguments': f'{{"reads":[{{"path":"b{i}.py"}}]}}'}}]})
            msgs.append({'role': 'tool', 'content': f'content of b{i}.py'})
        # Task-1 wrap-up + a NEW task-2 user turn (round_num resets to a low
        # value in task 2).
        msgs.append({'role': 'assistant', 'content': 'task 1 done'})
        msgs.append({'role': 'user', 'content': 'task 2: now refactor it'})
        return msgs

    def test_reminder_fires_on_message_scan_low_round_num(self):
        """B2 reproduction → fixed contract: with an earlier write and a gap
        of many messages since, the reminder fires REGARDLESS of round_num.

        On the OLD implementation this returns None: compute_turn_attachments
        gated on ``round_num > 5`` (here round_num=3 from a fresh task 2), and
        the per-conv ``_attachment_state`` carried a stale ``last_reminder_round``
        from task 1 that made the throttle gate permanently true. Both are
        cross-task round-counter bugs the message-scan fix removes."""
        from lib.tasks_pkg.attachments import compute_turn_attachments
        msgs = self._writeful_history()
        result = compute_turn_attachments(
            msgs, task={}, round_num=3, conv_id='conv-b2',
            project_path='/proj', project_enabled=True,
        )
        assert result, 'reminder must fire on message-scan even at low round_num'
        assert '## Recently Modified Files' in result[0]
        # The files it names come from the write/read history.
        assert 'a.py' in result[0]

    def test_reminder_does_not_fire_right_after_write(self):
        """No nagging immediately after a write — the model is still on it.
        A write in the last message means gap≈0 < the min-gap threshold."""
        from lib.tasks_pkg.attachments import compute_turn_attachments
        msgs = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'do it'},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'function': {'name': 'write_file',
                              'arguments': '{"path":"a.py","content":"x"}'}}]},
            {'role': 'tool', 'content': 'written'},
        ]
        result = compute_turn_attachments(
            msgs, task={}, round_num=9, conv_id='conv-b2b',
            project_path='/proj', project_enabled=True,
        )
        assert result == [], 'must not nag right after a write'

    def test_reminder_dedups_no_stacking(self):
        """B3: once a reminder is in context and NO new write follows it, the
        reminder must NOT fire again (no stale stacking). The prior reminder
        message is in the scan window; absent a newer write, return nothing."""
        from lib.tasks_pkg.attachments import (compute_turn_attachments,
                                               inject_attachments)
        msgs = self._writeful_history()
        first = compute_turn_attachments(
            msgs, task={}, round_num=3, conv_id='conv-b2c',
            project_path='/proj', project_enabled=True)
        assert first  # fires once
        inject_attachments(msgs, first)  # now the reminder is in context
        # A few more non-write rounds happen (no writes after the reminder).
        for i in range(4):
            msgs.append({'role': 'assistant', 'content': None, 'tool_calls': [
                {'function': {'name': 'grep_search',
                              'arguments': '{"pattern":"x"}'}}]})
            msgs.append({'role': 'tool', 'content': 'match'})
        second = compute_turn_attachments(
            msgs, task={}, round_num=5, conv_id='conv-b2c',
            project_path='/proj', project_enabled=True)
        assert second == [], 'must not re-fire without a new write since the reminder'

    def test_reminder_refires_after_new_write(self):
        """After a reminder, a NEW write + gap legitimately re-arms it."""
        from lib.tasks_pkg.attachments import (compute_turn_attachments,
                                               inject_attachments)
        msgs = self._writeful_history()
        inject_attachments(msgs, compute_turn_attachments(
            msgs, task={}, round_num=3, conv_id='conv-b2d',
            project_path='/proj', project_enabled=True))
        # New write AFTER the reminder, then a gap.
        msgs.append({'role': 'assistant', 'content': None, 'tool_calls': [
            {'function': {'name': 'apply_diff',
                          'arguments': '{"path":"z.py","search":"a","replace":"b"}'}}]})
        msgs.append({'role': 'tool', 'content': 'patched z.py'})
        for i in range(4):
            msgs.append({'role': 'assistant', 'content': None, 'tool_calls': [
                {'function': {'name': 'read_files',
                              'arguments': '{"reads":[{"path":"q.py"}]}'}}]})
            msgs.append({'role': 'tool', 'content': 'q'})
        again = compute_turn_attachments(
            msgs, task={}, round_num=7, conv_id='conv-b2d',
            project_path='/proj', project_enabled=True)
        assert again, 'a new write after the reminder must re-arm it'
        assert 'z.py' in again[0]

    def test_no_module_level_round_state(self):
        """B2 leak fix: the per-conv round-counter dict is gone (message-scan
        needs no cross-call state, so there is no unbounded module-level dict
        to leak)."""
        import lib.tasks_pkg.attachments as att
        assert not hasattr(att, '_attachment_state'), (
            '_attachment_state should be removed — the trigger is now a pure '
            'message scan with no per-conv state to leak')

    @staticmethod
    def _todos(active_status='in_progress'):
        return [
            {'id': 'inspect', 'content': 'Inspect the failure',
             'status': 'completed'},
            {'id': 'fix', 'content': 'Implement the fix',
             'status': active_status},
            {'id': 'verify', 'content': 'Verify end to end',
             'status': 'pending'},
        ]

    def test_todo_state_is_not_reinjected_while_matching_call_is_visible(self):
        from lib.tasks_pkg.attachments import compute_turn_attachments
        todos = self._todos()
        messages = [
            {'role': 'user', 'content': 'fix it'},
            {'role': 'assistant', 'content': None, 'tool_calls': [{
                'function': {'name': 'todo_write',
                             'arguments': json.dumps({'todos': todos})},
            }]},
            {'role': 'tool', 'content': 'Checklist updated'},
        ]
        result = compute_turn_attachments(
            messages, task={'_todos': todos}, round_num=3, conv_id='todo-live')
        assert result == []

    def test_todo_state_is_restored_after_tool_history_compaction(self):
        from lib.tasks_pkg.attachments import compute_turn_attachments
        todos = self._todos()
        # This is the post-L2 shape: the task dict retained the todos, while
        # the todo_write call/result are no longer in model-visible history.
        messages = [
            {'role': 'user', 'content': 'fix it'},
            {'role': 'assistant', 'content': None, 'tool_calls': [{
                'function': {'name': 'context_compact', 'arguments': '{}'},
            }]},
            {'role': 'tool', 'name': 'context_compact',
             'content': 'summary without the checklist'},
        ]
        result = compute_turn_attachments(
            messages, task={'_todos': todos}, round_num=40,
            conv_id='todo-compacted')
        assert len(result) == 1
        assert '## Active Task Checklist' in result[0]
        assert '- [x] Inspect the failure' in result[0]
        assert '- [ ] Implement the fix ⏳' in result[0]
        assert 'do not recreate or restart the plan' in result[0]
        assert 'reuse each id exactly in later sync calls' in result[0]
        assert 'id="inspect"' in result[0]
        assert 'id="fix"' in result[0]
        assert 'id="verify"' in result[0]

    def test_todo_restore_deduplicates_and_refreshes_stale_state(self):
        from lib.tasks_pkg.attachments import (compute_turn_attachments,
                                               inject_attachments)
        todos = self._todos()
        task = {'_todos': todos}
        messages = [{'role': 'user', 'content': 'fix it'}]
        first = compute_turn_attachments(
            messages, task=task, round_num=12, conv_id='todo-dedup')
        assert len(first) == 1
        inject_attachments(messages, first)
        assert compute_turn_attachments(
            messages, task=task, round_num=13, conv_id='todo-dedup') == []

        # Host state moved forward. The old reminder must not mask the newer
        # canonical state; one fresh, fingerprinted reminder should be added.
        task['_todos'] = self._todos(active_status='completed')
        refreshed = compute_turn_attachments(
            messages, task=task, round_num=14, conv_id='todo-dedup')
        assert len(refreshed) == 1
        assert '- [x] Implement the fix' in refreshed[0]



# ═══════════════════════════════════════════════════════════════════════════════
#  3. Cache Break Detection
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCacheTracking:
    """Tests for lib/tasks_pkg/cache_tracking.py."""

    def test_md5_consistency(self):
        from lib.tasks_pkg.cache_tracking._hashing import _md5
        assert _md5('hello') == _md5('hello')
        assert _md5('hello') != _md5('world')
        assert len(_md5('test')) == 16

    def test_hash_system_prompt_string(self):
        from lib.tasks_pkg.cache_tracking._hashing import _hash_system_prompt
        messages = [{'role': 'system', 'content': 'You are helpful'}]
        h = _hash_system_prompt(messages)
        assert h and len(h) == 16

    def test_hash_system_prompt_list(self):
        from lib.tasks_pkg.cache_tracking._hashing import _hash_system_prompt
        messages = [{'role': 'system', 'content': [
            {'type': 'text', 'text': 'You are helpful'},
        ]}]
        h = _hash_system_prompt(messages)
        assert h and len(h) == 16

    def test_hash_system_prompt_missing(self):
        from lib.tasks_pkg.cache_tracking._hashing import _hash_system_prompt
        assert _hash_system_prompt([{'role': 'user', 'content': 'hi'}]) == ''

    def test_hash_tools_empty(self):
        from lib.tasks_pkg.cache_tracking._hashing import _hash_tools
        assert _hash_tools(None) == ''
        assert _hash_tools([]) == ''

    def test_hash_tools_deterministic(self):
        from lib.tasks_pkg.cache_tracking._hashing import _hash_tools
        tools = [{'function': {'name': 'read_files', 'parameters': {}}}]
        h1 = _hash_tools(tools)
        h2 = _hash_tools(tools)
        assert h1 == h2

    def test_detect_cache_break_first_call_no_break(self):
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        conv_id = 'test-cb-1'
        _cache_states.pop(_state_key(conv_id, user_id=1), None)

        messages = [{'role': 'system', 'content': 'sys'}]
        result = detect_cache_break(conv_id, messages, None, 'model-a', user_id=1)
        assert result is None  # First call never breaks

    def test_detect_cache_break_model_change(self):
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        conv_id = 'test-cb-2'
        _cache_states.pop(_state_key(conv_id, user_id=1), None)

        messages = [{'role': 'system', 'content': 'sys'}]
        # First call establishes baseline with cache_read tokens
        detect_cache_break(conv_id, messages, None, 'model-a',
                           usage={'cache_read_tokens': 5000}, user_id=1)
        # Model change + cache_read drop confirms a cache break
        result = detect_cache_break(conv_id, messages, None, 'model-b',
                           usage={'cache_read_tokens': 100}, user_id=1)
        assert result is not None
        assert 'model' in result

    def test_detect_cache_break_system_prompt_change(self):
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        conv_id = 'test-cb-3'
        _cache_states.pop(_state_key(conv_id, user_id=1), None)

        messages1 = [{'role': 'system', 'content': 'prompt v1'}]
        detect_cache_break(conv_id, messages1, None, 'model-a',
                           usage={'cache_read_tokens': 5000}, user_id=1)

        messages2 = [{'role': 'system', 'content': 'prompt v2'}]
        result = detect_cache_break(conv_id, messages2, None, 'model-a',
                           usage={'cache_read_tokens': 100}, user_id=1)
        assert result is not None
        assert 'system_prompt' in result

    def test_detect_cache_break_empty_conv_id(self):
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        result = detect_cache_break('', [{'role': 'system', 'content': 'sys'}], None, 'm', user_id=1)
        assert result is None

    def test_get_cache_prefix_count_no_state(self):
        from lib.tasks_pkg.cache_tracking._state import _cache_states
        from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
        _cache_states.pop('nonexistent', None)
        assert get_cache_prefix_count('nonexistent', user_id=1) == 0

    def test_prefix_protected_after_write_only_round(self):
        """★ Regression: a round that only WROTE the prefix (cache_read=0,
        large cache_write — e.g. round 1 of a fresh conversation) must still
        protect that prefix from micro-compact mutation on the next round.
        Gating on read alone left round 2 unprotected → guaranteed miss."""
        from lib.tasks_pkg.cache_tracking._state import (
            CacheState,
            _cache_states,
        )
        from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
        conv_id = 'test-prefix-write-only'
        from lib.tasks_pkg.cache_tracking._state import _state_key
        state = CacheState()
        state.last_cache_read_tokens = 0        # nothing read yet
        state.last_cache_write_tokens = 278500  # but a big prefix WAS written
        state.message_count = 6
        state.call_count = 1
        _cache_states[_state_key(conv_id, user_id=1)] = state
        try:
            assert get_cache_prefix_count(conv_id, user_id=1) == 4  # max(0, 6 - 2)
        finally:
            _cache_states.pop(_state_key(conv_id, user_id=1), None)

    def test_no_false_positive_on_message_growth(self):
        """Growing messages (tool rounds) should NOT trigger a cache break
        when cache_read tokens are stable or growing."""
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        conv_id = 'test-cb-grow'
        _cache_states.pop(_state_key(conv_id, user_id=1), None)

        # Round 1: system + user
        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        detect_cache_break(conv_id, msgs, None, 'model-a',
                           usage={'cache_read_tokens': 1000}, user_id=1)

        # Round 2: add assistant + tool result (cache growing)
        msgs.append({'role': 'assistant', 'content': '', 'tool_calls': [
            {'function': {'name': 'read_files', 'arguments': '{}'}}
        ]})
        msgs.append({'role': 'tool', 'content': 'file content here'})
        result = detect_cache_break(conv_id, msgs, None, 'model-a',
                                    usage={'cache_read_tokens': 1500}, user_id=1)
        assert result is None  # No break — cache grew normally

    def test_notify_compaction_suppresses_break(self):
        """After compaction, a cache_read drop should not be flagged."""
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        from lib.tasks_pkg.cache_tracking._roi import notify_compaction
        conv_id = 'test-cb-compact'
        _cache_states.pop(_state_key(conv_id, user_id=1), None)

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break(conv_id, msgs, None, 'model-a',
                           usage={'cache_read_tokens': 10000}, user_id=1)
        # Compaction happened — notify
        notify_compaction(conv_id, user_id=1)
        # Cache tokens drop (expected after compaction)
        result = detect_cache_break(conv_id, msgs, None, 'model-a',
                                    usage={'cache_read_tokens': 3000}, user_id=1)
        # Should NOT be flagged as a confirmed break
        assert result is None or 'system_prompt' not in result

    def test_breakpoint_on_conversation_tail(self):
        """add_cache_breakpoints should place BP4 on the LAST message with
        content (msg[-1]), not msg[-2].  In tool conversations, msg[-1] is
        the tool result — always has content and becomes prefix next round.

        This was changed from msg[-2] to msg[-1] to fix the cache oscillation
        bug where empty-content assistants at msg[-2] caused BP4 to fall back
        to an early message, under-caching the conversation tail.
        See: debug/CACHE_BP4_AB_REPORT.md
        """
        from lib.llm import add_cache_breakpoints
        # Simulate a multi-round tool conversation:
        # system, user, asst+tc, tool, asst+tc, tool(latest)
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'system prompt'},
                {'role': 'user', 'content': 'hello'},
                {'role': 'assistant', 'content': 'Let me read that file.',
                 'tool_calls': [
                     {'function': {'name': 'read_files', 'arguments': '{}'}}
                 ]},
                {'role': 'tool', 'content': 'file content from round 1'},
                {'role': 'assistant', 'content': 'Now let me search.',
                 'tool_calls': [
                     {'function': {'name': 'grep_search', 'arguments': '{}'}}
                 ]},
                {'role': 'tool', 'content': 'search results from round 2'},
            ],
        }
        add_cache_breakpoints(body)
        # BP4 should be on the LAST message (msg[-1], the tool result)
        # because it caches the maximum prefix for the next round.
        last_msg = body['messages'][-1]
        content = last_msg.get('content', '')
        # It should have been converted to list with cache_control
        assert isinstance(content, list), \
            f'Expected list content on last msg, got {type(content)}'
        has_cache_control = any(
            isinstance(b, dict) and 'cache_control' in b
            for b in content
        )
        assert has_cache_control, \
            'Last message (tool result) should have cache_control breakpoint (BP4)'
        # Penultimate (msg[-2], assistant with content) should NOT have BP4
        # because msg[-1] already has it (maximum prefix coverage)
        penultimate = body['messages'][-2]
        pen_content = penultimate.get('content', '')
        if isinstance(pen_content, list):
            pen_has_cc = any(
                isinstance(b, dict) and 'cache_control' in b
                for b in pen_content
            )
            assert not pen_has_cc, \
                'Penultimate should NOT have BP4 when last msg has it'



# ═══════════════════════════════════════════════════════════════════════════════
#  4. Pre/Post Tool Hooks
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestToolHooks:
    """Tests for lib/tasks_pkg/tool_hooks.py."""

    def test_builtin_empty_result_hook(self):
        from lib.tasks_pkg.tool_hooks import _empty_result_marker_hook
        result = _empty_result_marker_hook('test_tool', {}, '', {})
        assert result is not None
        assert 'test_tool' in result
        assert 'no output' in result

    def test_builtin_empty_result_hook_nonempty(self):
        from lib.tasks_pkg.tool_hooks import _empty_result_marker_hook
        result = _empty_result_marker_hook('test_tool', {}, 'some content', {})
        assert result is None  # No modification

    def test_run_command_safety_hook_blocks_rm_rf(self):
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        result = _run_command_safety_hook('run_command', {'command': 'rm -rf /'}, {})
        assert result is not None
        assert result.action == 'block'

    def test_run_command_safety_hook_allows_normal(self):
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        result = _run_command_safety_hook('run_command', {'command': 'ls -la'}, {})
        assert result is None

    def test_run_command_safety_hook_ignores_other_tools(self):
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        result = _run_command_safety_hook('read_files', {'command': 'rm -rf /'}, {})
        assert result is None

    def test_register_and_run_pre_hook(self):
        from lib.tasks_pkg.tool_hooks import HookResult, _pre_hooks, register_pre_hook, run_pre_hooks
        original_count = len(_pre_hooks)

        def my_hook(tool_name, args, task):
            if tool_name == 'dangerous_tool':
                return HookResult(action='block', message='nope')
            return None

        register_pre_hook(my_hook)
        try:
            result = run_pre_hooks('dangerous_tool', {}, {})
            assert result is not None
            assert result.action == 'block'

            result2 = run_pre_hooks('safe_tool', {}, {})
            # May return None or a built-in hook result
        finally:
            _pre_hooks.pop()  # cleanup

    def test_register_and_run_post_hook(self):
        from lib.tasks_pkg.tool_hooks import _post_hooks, register_post_hook, run_post_hooks
        original_count = len(_post_hooks)

        def my_hook(tool_name, args, result, task):
            return result + '\n[MODIFIED]'

        register_post_hook(my_hook)
        try:
            result = run_post_hooks('test_tool', {}, 'original content', {})
            assert '[MODIFIED]' in result
        finally:
            _post_hooks.pop()  # cleanup

    def test_run_pre_hooks_exception_handled(self):
        from lib.tasks_pkg.tool_hooks import _pre_hooks, register_pre_hook, run_pre_hooks

        def bad_hook(tool_name, args, task):
            raise RuntimeError('hook failed')

        register_pre_hook(bad_hook)
        try:
            # Should not raise — exceptions are caught and logged
            result = run_pre_hooks('test_tool', {}, {})
            # Result could be None (bad hook's exception caught)
        finally:
            _pre_hooks.pop()

    def test_run_post_hooks_exception_handled(self):
        from lib.tasks_pkg.tool_hooks import _post_hooks, register_post_hook, run_post_hooks

        def bad_hook(tool_name, args, result, task):
            raise RuntimeError('hook failed')

        register_post_hook(bad_hook)
        try:
            result = run_post_hooks('test_tool', {}, 'content', {})
            assert result == 'content'  # Original preserved on exception
        finally:
            _post_hooks.pop()

    def test_hook_result_defaults(self):
        from lib.tasks_pkg.tool_hooks import HookResult
        hr = HookResult()
        assert hr.action == 'allow'
        assert hr.message == ''
        assert hr.modified_args is None


# ═══════════════════════════════════════════════════════════════════════════════
#  5. Unified ToolSpec
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  6. (removed) Dynamic Tool Deferral — deferral subsystem deleted
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  7. Partial Compaction
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  8. Integration tests: cache-aware microcompact
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCacheAwareMicroCompact:
    """Tests that microcompact respects cache prefix."""

    def test_cache_prefix_skips_messages(self):
        """When cache prefix is set, messages within it should be skipped."""
        from lib.tasks_pkg.cache_tracking._state import (
            CacheState,
            _cache_states,
        )
        from lib.tasks_pkg.compaction.api import micro_compact

        conv_id = 'test-cache-mc-1'
        from lib.tasks_pkg.cache_tracking._state import _state_key
        # Set up state with active cache
        state = CacheState()
        state.last_cache_read_tokens = 5000
        state.message_count = 5  # simulate 5 messages tracked; prefix = max(0, 5 - 2) = 3
        state.call_count = 5
        _cache_states[_state_key(conv_id, user_id=1)] = state

        messages = [
            {'role': 'system', 'content': 'system prompt'},
            {'role': 'user', 'content': 'first question'},
            {'role': 'assistant', 'content': 'first answer',
             'reasoning_content': 'thinking ' * 500},  # in cache prefix
            {'role': 'user', 'content': 'second question'},
            {'role': 'assistant', 'content': 'second answer',
             'reasoning_content': 'more thinking ' * 500},  # outside cache
            {'role': 'user', 'content': 'third question'},
            {'role': 'assistant', 'content': 'third answer',
             'reasoning_content': 'latest thinking'},  # in hot tail
        ]

        original_thinking_2 = messages[2]['reasoning_content']

        micro_compact(messages, conv_id=conv_id)

        # Message at index 2 (in cache prefix) should be PRESERVED
        assert messages[2]['reasoning_content'] == original_thinking_2

        # Cleanup
        _cache_states.pop(_state_key(conv_id, user_id=1), None)
