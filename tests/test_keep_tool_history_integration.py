"""Integration tests for keepToolHistory feature — server-side tool history preservation.

Tests the full flow:
  1. Frontend sends messages to /api/chat/start
  2. Orchestrator detects keepToolHistory flag
  3. On first turn: runs normally, saves full messages to server store
  4. On subsequent turns: rebuilds messages from store (restoring tool history)
  5. Verifies token overhead measurements and message structure

Uses mock LLM responses to avoid real API calls.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.tasks_pkg.server_message_store import (
    _store,
    _store_lock,
    clear,
    get_messages,
    get_stats,
    save_messages,
    rebuild_messages_with_history,
    estimate_token_overhead,
)


# ═══════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clear_store():
    """Clear the store before and after each test."""
    with _store_lock:
        _store.clear()
    yield
    with _store_lock:
        _store.clear()


# ═══════════════════════════════════════════════════════════
#  Helpers — simulate orchestrator message flow
# ═══════════════════════════════════════════════════════════

def _sim_tool_call(tc_id, name, args):
    return {
        'id': tc_id,
        'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(args)},
    }


def _sim_turn_with_tools(conv_id, user_msg, tool_calls_and_results, final_text):
    """Simulate what the orchestrator does during a turn with tool calls.

    This mirrors the actual orchestrator flow:
    1. Start with messages from task['messages'] (from frontend)
    2. LLM produces assistant message with tool_calls
    3. Tools execute and produce tool results
    4. LLM produces final text response
    5. Messages list now contains the full history

    Returns the complete messages list as the orchestrator would have it.
    """
    messages = []

    # If we have stored messages from a previous turn, use those
    stored = get_messages(conv_id)
    if stored:
        messages = list(stored)
    else:
        # First turn or no store: start with system + user
        messages.append({'role': 'system', 'content': 'You are a helpful assistant.'})

    # Add the new user message
    messages.append({'role': 'user', 'content': user_msg})

    # System contexts would be injected here (skipped for test)

    # Simulate LLM producing tool calls
    for tc_group, results in tool_calls_and_results:
        # Assistant message with tool_calls
        assistant_msg = {'role': 'assistant', 'tool_calls': tc_group}
        messages.append(assistant_msg)

        # Tool result messages
        for tc_id, result_content in results:
            messages.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': result_content,
            })

    # Final assistant text response
    messages.append({'role': 'assistant', 'content': final_text})

    # Save to store (like orchestrator does at end of run_task)
    save_messages(conv_id, messages)

    return messages


def _sim_frontend_summary(turns):
    """Build what the frontend would send for subsequent turns.

    The frontend only keeps:
      system → user → assistant(content=final_text) → user → assistant(content=final_text) → ...

    No tool_use blocks, no tool_result messages.
    """
    messages = [{'role': 'system', 'content': 'You are a helpful assistant.'}]
    for user_msg, final_text in turns:
        messages.append({'role': 'user', 'content': user_msg})
        messages.append({'role': 'assistant', 'content': final_text})
    return messages


# ═══════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestKeepToolHistoryFlow:
    """Test the complete keepToolHistory flow across multiple turns."""

    def test_turn1_saves_to_store(self):
        """First turn: orchestrator saves full messages to store."""
        conv_id = 'test_conv_001'

        # Simulate turn 1: user asks question, model searches, model answers
        messages = _sim_turn_with_tools(
            conv_id,
            user_msg="What is Python's GIL?",
            tool_calls_and_results=[
                (
                    [_sim_tool_call('tc_1', 'web_search', {'query': 'Python GIL'})],
                    [('tc_1', 'The GIL is a mutex in CPython that allows only one thread to execute Python bytecodes at a time.')],
                ),
            ],
            final_text="The GIL (Global Interpreter Lock) is a mutex in CPython...",
        )

        # Verify store has the full messages
        stored = get_messages(conv_id)
        assert stored is not None
        assert len(stored) == len(messages)

        # Verify tool_calls and tool results are preserved
        tool_call_msgs = [m for m in stored if m.get('tool_calls')]
        tool_result_msgs = [m for m in stored if m.get('role') == 'tool']
        assert len(tool_call_msgs) == 1
        assert len(tool_result_msgs) == 1
        assert tool_call_msgs[0]['tool_calls'][0]['function']['name'] == 'web_search'

    def test_turn2_rebuilds_from_store(self):
        """Second turn: frontend sends summaries, we rebuild from store."""
        conv_id = 'test_conv_002'

        # Turn 1
        _sim_turn_with_tools(
            conv_id,
            user_msg="What is Python's GIL?",
            tool_calls_and_results=[
                (
                    [_sim_tool_call('tc_1', 'web_search', {'query': 'Python GIL'})],
                    [('tc_1', 'The GIL is a mutex in CPython...')],
                ),
            ],
            final_text="The GIL is a mutex in CPython that allows only one thread...",
        )

        # Turn 2: frontend sends summary of turn 1 + new question
        frontend_msgs = _sim_frontend_summary([
            ("What is Python's GIL?", "The GIL is a mutex in CPython that allows only one thread..."),
        ]) + [{'role': 'user', 'content': 'How can I work around the GIL?'}]

        # Rebuild
        rebuilt, stats = rebuild_messages_with_history(conv_id, frontend_msgs)

        assert stats['used_store'] is True
        assert stats['tool_msgs_restored'] > 0

        # Rebuilt should have tool messages
        tool_call_msgs = [m for m in rebuilt if m.get('tool_calls')]
        tool_result_msgs = [m for m in rebuilt if m.get('role') == 'tool']
        assert len(tool_call_msgs) >= 1
        assert len(tool_result_msgs) >= 1

        # Last message should be the new question
        assert rebuilt[-1]['role'] == 'user'
        assert 'work around' in rebuilt[-1]['content']

    def test_three_turn_accumulation(self):
        """Three turns: messages accumulate with full tool history each time."""
        conv_id = 'test_conv_003'

        # Turn 1: search
        _sim_turn_with_tools(
            conv_id,
            user_msg="What is asyncio?",
            tool_calls_and_results=[
                (
                    [_sim_tool_call('tc_1', 'web_search', {'query': 'Python asyncio'})],
                    [('tc_1', 'asyncio is a library for async/await...')],
                ),
            ],
            final_text="asyncio is Python's standard library for async I/O...",
        )

        # Turn 2: more searches (simulate using stored messages)
        stored = get_messages(conv_id)
        assert stored is not None

        # Add turn 2 on top of stored messages
        turn2_messages = list(stored)
        turn2_messages.append({'role': 'user', 'content': 'Show me an example'})
        turn2_messages.append({
            'role': 'assistant',
            'tool_calls': [_sim_tool_call('tc_2', 'web_search', {'query': 'Python asyncio example'})],
        })
        turn2_messages.append({
            'role': 'tool',
            'tool_call_id': 'tc_2',
            'content': 'import asyncio\nasync def main():\n    await asyncio.sleep(1)',
        })
        turn2_messages.append({
            'role': 'assistant',
            'content': "Here's a basic asyncio example...",
        })
        save_messages(conv_id, turn2_messages)

        # Turn 3: frontend sends summaries of turns 1+2 + new question
        frontend_turn3 = _sim_frontend_summary([
            ("What is asyncio?", "asyncio is Python's standard library for async I/O..."),
            ("Show me an example", "Here's a basic asyncio example..."),
        ]) + [{'role': 'user', 'content': 'How does it compare to threading?'}]

        rebuilt, stats = rebuild_messages_with_history(conv_id, frontend_turn3)

        assert stats['used_store'] is True

        # Should have tool messages from BOTH turn 1 and turn 2
        all_tool_msgs = [m for m in rebuilt if m.get('role') == 'tool']
        assert len(all_tool_msgs) == 2  # tc_1 from turn 1, tc_2 from turn 2

        all_tool_calls = [m for m in rebuilt if m.get('tool_calls')]
        assert len(all_tool_calls) == 2

        # Verify queries are preserved
        queries = []
        for m in rebuilt:
            for tc in m.get('tool_calls', []):
                args = json.loads(tc['function']['arguments'])
                queries.append(args.get('query', ''))
        assert 'Python asyncio' in queries
        assert 'Python asyncio example' in queries

    def test_overhead_measurement(self):
        """Verify overhead measurement is correct across turns."""
        conv_id = 'test_conv_oh'

        # Turn 1 with large tool results
        large_result = "x" * 10000  # 10K chars
        _sim_turn_with_tools(
            conv_id,
            user_msg="Fetch this page",
            tool_calls_and_results=[
                (
                    [_sim_tool_call('tc_1', 'fetch_url', {'url': 'https://example.com'})],
                    [('tc_1', large_result)],
                ),
            ],
            final_text="Here's what I found on the page...",
        )

        # Frontend summary for turn 2
        frontend = _sim_frontend_summary([
            ("Fetch this page", "Here's what I found on the page..."),
        ]) + [{'role': 'user', 'content': 'Next question'}]

        stored = get_messages(conv_id)
        assert stored is not None

        # Measure overhead
        oh = estimate_token_overhead(frontend, stored)

        print(f"\n{'='*60}")
        print("OVERHEAD with 10K tool result:")
        print(f"  Frontend: {oh['frontend_chars']:,} chars ≈ {oh['frontend_est_tokens']} tokens")
        print(f"  Stored:   {oh['stored_chars']:,} chars ≈ {oh['stored_est_tokens']} tokens")
        print(f"  Overhead: +{oh['overhead_chars']:,} chars ≈ +{oh['overhead_est_tokens']} tokens")
        print(f"  Ratio:    {oh['ratio']}x")

        # With a 10K tool result, stored should be much larger
        assert oh['ratio'] > 5.0


@pytest.mark.unit
class TestMessageStructureIntegrity:
    """Test that rebuilt messages maintain valid structure for the LLM API."""

    def test_role_alternation(self):
        """Rebuilt messages should maintain user↔assistant↔tool role structure."""
        conv_id = 'test_roles'
        _sim_turn_with_tools(
            conv_id,
            user_msg="Search for Python",
            tool_calls_and_results=[
                (
                    [_sim_tool_call('tc_1', 'web_search', {'query': 'Python'})],
                    [('tc_1', 'Python is a programming language')],
                ),
            ],
            final_text="Python is...",
        )

        frontend = _sim_frontend_summary([
            ("Search for Python", "Python is..."),
        ]) + [{'role': 'user', 'content': 'Tell me more'}]

        rebuilt, _ = rebuild_messages_with_history(conv_id, frontend)

        # Verify valid role sequence: no consecutive same-role (except tool after assistant)
        for i in range(1, len(rebuilt)):
            prev_role = rebuilt[i-1].get('role')
            curr_role = rebuilt[i].get('role')

            if prev_role == curr_role:
                # Only valid if both are 'tool' (multiple tool results)
                # or assistant→assistant is not possible in our data
                assert curr_role == 'tool', (
                    f"Invalid consecutive {curr_role} at positions {i-1},{i}"
                )

            # assistant with tool_calls should be followed by tool results
            if prev_role == 'assistant' and rebuilt[i-1].get('tool_calls'):
                assert curr_role == 'tool', (
                    f"assistant(tool_calls) at {i-1} should be followed by tool, got {curr_role}"
                )

    def test_tool_call_id_consistency(self):
        """Every tool result should reference a tool_call_id that exists in a preceding assistant message."""
        conv_id = 'test_tc_ids'
        _sim_turn_with_tools(
            conv_id,
            user_msg="Multi-tool test",
            tool_calls_and_results=[
                (
                    [
                        _sim_tool_call('tc_1', 'web_search', {'query': 'q1'}),
                        _sim_tool_call('tc_2', 'fetch_url', {'url': 'https://x.com'}),
                    ],
                    [
                        ('tc_1', 'result 1'),
                        ('tc_2', 'result 2'),
                    ],
                ),
            ],
            final_text="Combined results...",
        )

        frontend = _sim_frontend_summary([
            ("Multi-tool test", "Combined results..."),
        ]) + [{'role': 'user', 'content': 'Next'}]

        rebuilt, _ = rebuild_messages_with_history(conv_id, frontend)

        # Collect all defined tool_call_ids
        defined_ids = set()
        for msg in rebuilt:
            for tc in msg.get('tool_calls', []):
                defined_ids.add(tc['id'])

        # Verify all tool results reference a defined id
        for msg in rebuilt:
            if msg.get('role') == 'tool':
                tc_id = msg.get('tool_call_id')
                assert tc_id in defined_ids, (
                    f"tool result references {tc_id} but it's not in defined ids: {defined_ids}"
                )


@pytest.mark.unit
class TestEdgeCases:
    """Edge cases and error handling."""

    def test_no_tool_calls_turn(self):
        """A turn with no tool calls should work fine (just not save to store)."""
        conv_id = 'test_no_tools'
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there!'},
        ]
        save_messages(conv_id, messages)
        assert get_messages(conv_id) is None  # Not saved (no tool msgs)

    def test_concurrent_saves(self):
        """Multiple threads saving to the same conv_id should be safe."""
        conv_id = 'test_concurrent'
        errors = []

        def _save(n):
            try:
                msgs = [
                    {'role': 'user', 'content': f'Question {n}'},
                    {'role': 'assistant', 'tool_calls': [_sim_tool_call(f'tc_{n}', 'search', {'q': str(n)})]},
                    {'role': 'tool', 'tool_call_id': f'tc_{n}', 'content': f'Result {n}'},
                    {'role': 'assistant', 'content': f'Answer {n}'},
                ]
                save_messages(conv_id, msgs)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_save, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        stored = get_messages(conv_id)
        assert stored is not None  # One of the saves should have succeeded

    def test_empty_conv_id(self):
        """Empty conv_id should be handled gracefully."""
        save_messages('', [{'role': 'tool', 'tool_call_id': 'tc', 'content': 'x'}])
        assert get_messages('') is None

    def test_large_number_of_conversations(self):
        """Store should handle many conversations without issues."""
        for i in range(50):
            msgs = [
                {'role': 'user', 'content': f'Q{i}'},
                {'role': 'assistant', 'tool_calls': [_sim_tool_call(f'tc_{i}', 's', {'q': str(i)})]},
                {'role': 'tool', 'tool_call_id': f'tc_{i}', 'content': f'R{i}'},
                {'role': 'assistant', 'content': f'A{i}'},
            ]
            save_messages(f'conv_{i}', msgs)

        s = get_stats()
        assert s['conversations'] == 50

    def test_rebuild_with_no_system_message(self):
        """Frontend messages without system prompt should work."""
        conv_id = 'test_no_sys'
        msgs = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'tool_calls': [_sim_tool_call('tc_1', 'search', {'q': 'hello'})]},
            {'role': 'tool', 'tool_call_id': 'tc_1', 'content': 'world'},
            {'role': 'assistant', 'content': 'Hi!'},
        ]
        save_messages(conv_id, msgs)

        frontend = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi!'},
            {'role': 'user', 'content': 'Next question'},
        ]
        rebuilt, stats = rebuild_messages_with_history(conv_id, frontend)
        assert stats['used_store'] is True


@pytest.mark.unit
class TestOverheadScenarioMatrix:
    """Comprehensive matrix of overhead scenarios for the report."""

    @pytest.mark.parametrize("num_turns,tools_per_turn,result_size,desc", [
        (1, 1, 500, "Light: 1 turn, 1 search, 500-char result"),
        (1, 3, 1000, "Medium: 1 turn, 3 tools, 1K results"),
        (3, 2, 2000, "Heavy: 3 turns, 2 tools/turn, 2K results"),
        (5, 3, 5000, "Very heavy: 5 turns, 3 tools/turn, 5K results"),
        (3, 1, 20000, "Fetch-heavy: 3 turns, 1 fetch/turn, 20K results"),
        (10, 1, 500, "Long conv: 10 turns, 1 search/turn, 500-char"),
    ])
    def test_overhead_scenario(self, num_turns, tools_per_turn, result_size, desc):
        """Parameterized overhead test across various scenarios."""
        full_messages = [{'role': 'system', 'content': 'sys'}]
        frontend_messages = [{'role': 'system', 'content': 'sys'}]

        for turn in range(num_turns):
            q = f"Question {turn}: " + "x" * 50
            a = f"Answer {turn}: " + "y" * 200

            full_messages.append({'role': 'user', 'content': q})
            frontend_messages.append({'role': 'user', 'content': q})

            tcs = [_sim_tool_call(f'tc_{turn}_{i}', 'tool', {'q': f'q{turn}_{i}'})
                   for i in range(tools_per_turn)]
            full_messages.append({'role': 'assistant', 'tool_calls': tcs})

            for i in range(tools_per_turn):
                full_messages.append({
                    'role': 'tool',
                    'tool_call_id': f'tc_{turn}_{i}',
                    'content': f"Result {turn}.{i}: " + "r" * result_size,
                })

            full_messages.append({'role': 'assistant', 'content': a})
            frontend_messages.append({'role': 'assistant', 'content': a})

        # Add new user question
        full_messages.append({'role': 'user', 'content': 'Follow-up question'})
        frontend_messages.append({'role': 'user', 'content': 'Follow-up question'})

        oh = estimate_token_overhead(frontend_messages, full_messages)

        total_tool_results = num_turns * tools_per_turn
        total_result_chars = total_tool_results * result_size

        print(f"\n  {desc}")
        print(f"    Tool results: {total_tool_results} × {result_size:,} chars = {total_result_chars:,} chars")
        print(f"    Frontend: {oh['frontend_est_tokens']:,} tk | Full: {oh['stored_est_tokens']:,} tk | "
              f"Overhead: +{oh['overhead_est_tokens']:,} tk | Ratio: {oh['ratio']:.1f}x")

        assert oh['ratio'] >= 1.0


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
