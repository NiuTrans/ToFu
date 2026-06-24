"""Tests for server-side message store — full tool history preservation.

Validates:
  1. Basic store/retrieve/clear operations
  2. Message rebuilding (frontend summary → full history)
  3. Token overhead estimation
  4. Multi-turn simulation showing information loss vs preservation
  5. Edge cases (no tools, expired entries, etc.)
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.tasks_pkg.server_message_store import (
    clear,
    estimate_token_overhead,
    get_messages,
    get_stats,
    rebuild_messages_with_history,
    save_messages,
    _store,
    _store_lock,
)


# ═══════════════════════════════════════════════════════════
#  Helpers — build realistic multi-turn conversation data
# ═══════════════════════════════════════════════════════════

def _make_tool_call(tc_id, name, args_dict):
    """Build a tool_call entry like the orchestrator does."""
    return {
        'id': tc_id,
        'type': 'function',
        'function': {
            'name': name,
            'arguments': json.dumps(args_dict),
        },
    }


def _make_tool_result(tc_id, content):
    """Build a tool result message."""
    return {
        'role': 'tool',
        'tool_call_id': tc_id,
        'content': content,
    }


def _make_assistant_with_tools(content, tool_calls):
    """Build an assistant message that includes tool_calls."""
    msg = {'role': 'assistant', 'tool_calls': tool_calls}
    if content:
        msg['content'] = content
    return msg


def _make_assistant_text(content):
    """Build a plain assistant text message."""
    return {'role': 'assistant', 'content': content}


def _make_user(content):
    """Build a user message."""
    return {'role': 'user', 'content': content}


def _make_system(content):
    """Build a system message."""
    return {'role': 'system', 'content': content}


def _build_full_history_turn1():
    """Simulate turn 1: user asks about Python, model searches then answers.

    Full history (as backend's orchestrator sees it):
      system → user → assistant(tool_use: web_search) → tool(result) → assistant(text)
    """
    return [
        _make_system("You are a helpful assistant."),
        _make_user("What is the GIL in Python?"),
        _make_assistant_with_tools(
            "Let me search for information about the GIL.",
            [_make_tool_call('tc_001', 'web_search', {'query': 'Python GIL explained'})],
        ),
        _make_tool_result('tc_001',
            "The Global Interpreter Lock (GIL) is a mutex that protects access to Python objects, "
            "preventing multiple threads from executing Python bytecodes at once. This lock is "
            "necessary mainly because CPython's memory management is not thread-safe. The GIL "
            "allows only one thread to execute in the interpreter at any time, which means "
            "CPU-bound multi-threaded programs may not see performance gains. However, I/O-bound "
            "programs can still benefit from threading because the GIL is released during I/O "
            "operations. PEP 703 proposes making the GIL optional in CPython 3.13+."),
        _make_assistant_text(
            "The GIL (Global Interpreter Lock) is a mutex in CPython that allows only one thread "
            "to execute Python bytecodes at a time. It exists because CPython's memory management "
            "is not thread-safe. While it limits CPU-bound multi-threading performance, I/O-bound "
            "programs can still benefit from threading since the GIL is released during I/O "
            "operations. PEP 703 proposes making the GIL optional starting from CPython 3.13+."),
    ]


def _build_frontend_summary_turn1():
    """What the frontend sends for turn 1's assistant message: just the final text.

    Frontend's buildApiMessages() produces:
      system → user → assistant(content=final_text)
    """
    return [
        _make_system("You are a helpful assistant."),
        _make_user("What is the GIL in Python?"),
        _make_assistant_text(
            "The GIL (Global Interpreter Lock) is a mutex in CPython that allows only one thread "
            "to execute Python bytecodes at a time. It exists because CPython's memory management "
            "is not thread-safe. While it limits CPU-bound multi-threading performance, I/O-bound "
            "programs can still benefit from threading since the GIL is released during I/O "
            "operations. PEP 703 proposes making the GIL optional starting from CPython 3.13+."),
    ]


def _build_full_history_turn2():
    """Simulate turn 2: user asks follow-up, model does 2 tool calls.

    Full history extends turn 1 with:
      user → assistant(tool_use: web_search, fetch_url) → tool(result1) → tool(result2) → assistant(text)
    """
    turn1 = _build_full_history_turn1()
    turn2_additions = [
        _make_user("How does asyncio compare to threading for I/O-bound tasks?"),
        _make_assistant_with_tools(
            "Let me research this comparison.",
            [
                _make_tool_call('tc_002', 'web_search', {'query': 'Python asyncio vs threading I/O performance'}),
                _make_tool_call('tc_003', 'fetch_url', {'url': 'https://docs.python.org/3/library/asyncio.html'}),
            ],
        ),
        _make_tool_result('tc_002',
            "Asyncio uses a single-threaded event loop with coroutines, while threading uses OS "
            "threads. For I/O-bound tasks, asyncio is generally more efficient because: 1) No "
            "thread creation/context switching overhead, 2) Lower memory usage (coroutines are "
            "lightweight), 3) No GIL contention. However, threading is simpler to reason about "
            "for many developers. Asyncio shines when handling thousands of concurrent connections "
            "(like web servers), while threading is sufficient for moderate concurrency."),
        _make_tool_result('tc_003',
            "asyncio is a library to write concurrent code using the async/await syntax. It is "
            "used as a foundation for multiple Python asynchronous frameworks. asyncio provides "
            "high-level APIs for: running Python coroutines concurrently, performing network I/O, "
            "distributing tasks via queues, synchronizing concurrent code. Additionally, there are "
            "low-level APIs for library and framework developers. Key concepts: event loop, "
            "coroutines, tasks, futures, transports and protocols. [50KB of documentation...]"),
        _make_assistant_text(
            "Here's how asyncio compares to threading for I/O-bound tasks:\n\n"
            "**Threading**: Uses OS threads, each with its own stack. Simple to use but has GIL "
            "contention and higher memory overhead per thread.\n\n"
            "**Asyncio**: Uses a single-threaded event loop with lightweight coroutines. More "
            "efficient for high-concurrency I/O scenarios (thousands of connections) because:\n"
            "- No thread creation overhead\n"
            "- ~100x lower memory per concurrent task\n"
            "- No GIL contention\n\n"
            "For moderate concurrency (10-100 I/O operations), threading works fine. For high "
            "concurrency (1000+), asyncio is significantly better."),
    ]
    return turn1 + turn2_additions


def _build_frontend_summary_turn2():
    """What the frontend sends for turns 1+2: summary-only assistant messages."""
    turn1_summary = _build_frontend_summary_turn1()
    turn2_additions = [
        _make_user("How does asyncio compare to threading for I/O-bound tasks?"),
        _make_assistant_text(
            "Here's how asyncio compares to threading for I/O-bound tasks:\n\n"
            "**Threading**: Uses OS threads, each with its own stack. Simple to use but has GIL "
            "contention and higher memory overhead per thread.\n\n"
            "**Asyncio**: Uses a single-threaded event loop with lightweight coroutines. More "
            "efficient for high-concurrency I/O scenarios (thousands of connections) because:\n"
            "- No thread creation overhead\n"
            "- ~100x lower memory per concurrent task\n"
            "- No GIL contention\n\n"
            "For moderate concurrency (10-100 I/O operations), threading works fine. For high "
            "concurrency (1000+), asyncio is significantly better."),
    ]
    return turn1_summary + turn2_additions


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
#  Test: Basic store operations
# ═══════════════════════════════════════════════════════════

class TestBasicOperations:
    def test_save_and_retrieve(self):
        msgs = _build_full_history_turn1()
        save_messages('conv_001', msgs)
        retrieved = get_messages('conv_001')
        assert retrieved is not None
        assert len(retrieved) == len(msgs)
        # Verify tool_calls preserved
        tool_msgs = [m for m in retrieved if m.get('tool_calls')]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]['tool_calls'][0]['function']['name'] == 'web_search'

    def test_retrieve_nonexistent(self):
        assert get_messages('nonexistent') is None

    def test_clear(self):
        save_messages('conv_001', _build_full_history_turn1())
        assert get_messages('conv_001') is not None
        clear('conv_001')
        assert get_messages('conv_001') is None

    def test_no_tool_messages_not_saved(self):
        """Messages without tool calls should not be saved (no benefit)."""
        msgs = [
            _make_system("Hi"),
            _make_user("Hello"),
            _make_assistant_text("Hi there!"),
        ]
        save_messages('conv_no_tools', msgs)
        assert get_messages('conv_no_tools') is None

    def test_stats(self):
        save_messages('conv_001', _build_full_history_turn1())
        save_messages('conv_002', _build_full_history_turn2())
        stats = get_stats()
        assert stats['conversations'] == 2
        assert stats['total_messages'] == len(_build_full_history_turn1()) + len(_build_full_history_turn2())

    def test_expiry(self):
        """Entries older than _MAX_AGE_S should be auto-cleaned on get."""
        msgs = _build_full_history_turn1()
        save_messages('conv_old', msgs)
        # Manually age the entry
        with _store_lock:
            _store['conv_old']['updated_at'] = time.time() - 8000  # > 7200s
        assert get_messages('conv_old') is None


# ═══════════════════════════════════════════════════════════
#  Test: Message rebuilding
# ═══════════════════════════════════════════════════════════

class TestMessageRebuilding:
    def test_rebuild_replaces_summary_with_full_history(self):
        """Core test: frontend sends summaries, we rebuild with full tool history."""
        # Save turn 1's full history
        save_messages('conv_001', _build_full_history_turn1())

        # Frontend sends turn 2's request: summary of turn 1 + new user message
        frontend_msgs = _build_frontend_summary_turn1() + [
            _make_user("How does asyncio compare to threading?"),
        ]

        rebuilt, stats = rebuild_messages_with_history('conv_001', frontend_msgs)

        assert stats['used_store'] is True
        assert stats['new_user_msg_found'] is True
        assert stats['tool_msgs_restored'] > 0

        # Rebuilt should have more messages than frontend (tool_use + tool_result added)
        assert len(rebuilt) > len(frontend_msgs)

        # Verify tool_calls are present in rebuilt
        tool_call_msgs = [m for m in rebuilt if m.get('tool_calls')]
        assert len(tool_call_msgs) >= 1

        # Verify tool result messages are present
        tool_result_msgs = [m for m in rebuilt if m.get('role') == 'tool']
        assert len(tool_result_msgs) >= 1

        # Last message should be the new user question
        assert rebuilt[-1]['role'] == 'user'
        assert 'asyncio' in rebuilt[-1]['content']

    def test_rebuild_preserves_system_from_frontend(self):
        """System prompt should come from frontend (may have been updated)."""
        save_messages('conv_001', _build_full_history_turn1())

        frontend_msgs = [
            _make_system("Updated system prompt!"),
            _make_user("What is the GIL?"),
            _make_assistant_text("GIL is..."),
            _make_user("Tell me more"),
        ]
        rebuilt, stats = rebuild_messages_with_history('conv_001', frontend_msgs)

        assert stats['used_store'] is True
        # System message should be the frontend's updated version
        assert rebuilt[0]['role'] == 'system'
        assert rebuilt[0]['content'] == "Updated system prompt!"

    def test_rebuild_no_store_returns_frontend(self):
        """When no stored messages exist, return frontend messages unchanged."""
        frontend_msgs = _build_frontend_summary_turn1() + [_make_user("Next question")]
        rebuilt, stats = rebuild_messages_with_history('conv_new', frontend_msgs)

        assert stats['used_store'] is False
        assert rebuilt is frontend_msgs  # Same object (no copy needed)

    def test_rebuild_no_user_message_falls_back(self):
        """If frontend messages don't end with a user message, fall back."""
        save_messages('conv_001', _build_full_history_turn1())

        frontend_msgs = [_make_assistant_text("Hmm")]
        rebuilt, stats = rebuild_messages_with_history('conv_001', frontend_msgs)

        assert stats['used_store'] is False


# ═══════════════════════════════════════════════════════════
#  Test: Token overhead estimation
# ═══════════════════════════════════════════════════════════

class TestTokenOverhead:
    def test_single_turn_overhead(self):
        """After 1 turn with 1 tool call: measure overhead."""
        frontend = _build_frontend_summary_turn1()
        full = _build_full_history_turn1()

        overhead = estimate_token_overhead(frontend, full)

        print(f"\n{'='*60}")
        print("TURN 1 (1 web_search):")
        print(f"  Frontend (summary):  {overhead['frontend_chars']:,} chars ≈ {overhead['frontend_est_tokens']:,} tokens")
        print(f"  Full (with tools):   {overhead['stored_chars']:,} chars ≈ {overhead['stored_est_tokens']:,} tokens")
        print(f"  Overhead:            {overhead['overhead_chars']:,} chars ≈ {overhead['overhead_est_tokens']:,} tokens")
        print(f"  Ratio:               {overhead['ratio']}x")

        assert overhead['stored_chars'] > overhead['frontend_chars']
        assert overhead['ratio'] > 1.0

    def test_two_turn_overhead(self):
        """After 2 turns with 3 total tool calls: measure overhead."""
        frontend = _build_frontend_summary_turn2()
        full = _build_full_history_turn2()

        overhead = estimate_token_overhead(frontend, full)

        print(f"\n{'='*60}")
        print("TURN 2 (3 tool calls total: 2 web_search + 1 fetch_url):")
        print(f"  Frontend (summary):  {overhead['frontend_chars']:,} chars ≈ {overhead['frontend_est_tokens']:,} tokens")
        print(f"  Full (with tools):   {overhead['stored_chars']:,} chars ≈ {overhead['stored_est_tokens']:,} tokens")
        print(f"  Overhead:            {overhead['overhead_chars']:,} chars ≈ {overhead['overhead_est_tokens']:,} tokens")
        print(f"  Ratio:               {overhead['ratio']}x")

        assert overhead['ratio'] > 1.0

    def test_five_turn_heavy_tools(self):
        """Simulate 5 turns with increasingly heavy tool usage.

        This models a real research conversation where the user asks
        progressively deeper questions, and the model uses multiple tools
        per turn.
        """
        # Build a realistic 5-turn conversation
        full_messages = [_make_system("You are a helpful research assistant.")]
        frontend_messages = [_make_system("You are a helpful research assistant.")]

        # Simulate 5 turns of increasingly complex tool usage
        tool_calls_per_turn = [1, 2, 3, 2, 4]  # escalating then tapering
        tool_result_sizes = [500, 1000, 2000, 1500, 3000]  # chars per result

        for turn_idx in range(5):
            question = f"Research question #{turn_idx + 1} about advanced topic {turn_idx}"
            final_answer = f"Based on my research, here's the answer to question #{turn_idx + 1}. " * 5

            # User message (same in both)
            full_messages.append(_make_user(question))
            frontend_messages.append(_make_user(question))

            # Full: assistant with tool calls + results + final text
            tcs = []
            for tc_idx in range(tool_calls_per_turn[turn_idx]):
                tc_id = f'tc_{turn_idx}_{tc_idx}'
                tool_name = ['web_search', 'fetch_url', 'read_file', 'grep_search'][tc_idx % 4]
                tcs.append(_make_tool_call(tc_id, tool_name, {'query': f'query_{turn_idx}_{tc_idx}'}))

            full_messages.append(_make_assistant_with_tools(
                f"Let me research question #{turn_idx + 1}.",
                tcs,
            ))

            for tc_idx in range(tool_calls_per_turn[turn_idx]):
                tc_id = f'tc_{turn_idx}_{tc_idx}'
                result_text = f"Tool result content for query {turn_idx}.{tc_idx}. " * (tool_result_sizes[turn_idx] // 50)
                full_messages.append(_make_tool_result(tc_id, result_text))

            full_messages.append(_make_assistant_text(final_answer))

            # Frontend: just the final text
            frontend_messages.append(_make_assistant_text(final_answer))

        # Now simulate the user asking turn 6
        new_question = "Final synthesis question combining all previous research"
        full_messages_for_turn6 = list(full_messages) + [_make_user(new_question)]
        frontend_messages_for_turn6 = list(frontend_messages) + [_make_user(new_question)]

        overhead = estimate_token_overhead(frontend_messages_for_turn6, full_messages_for_turn6)

        print(f"\n{'='*60}")
        print(f"5-TURN HEAVY TOOL USAGE ({sum(tool_calls_per_turn)} total tool calls):")
        print(f"  Frontend messages:   {len(frontend_messages_for_turn6)}")
        print(f"  Full messages:       {len(full_messages_for_turn6)}")
        print(f"  Frontend (summary):  {overhead['frontend_chars']:,} chars ≈ {overhead['frontend_est_tokens']:,} tokens")
        print(f"  Full (with tools):   {overhead['stored_chars']:,} chars ≈ {overhead['stored_est_tokens']:,} tokens")
        print(f"  Overhead:            +{overhead['overhead_chars']:,} chars ≈ +{overhead['overhead_est_tokens']:,} tokens")
        print(f"  Ratio:               {overhead['ratio']}x")

        # With tool results typically 500-3000 chars each, expect ~2-5x overhead
        assert overhead['ratio'] >= 1.5

    def test_realistic_web_research_session(self):
        """Simulate a realistic web research session with large fetch_url results.

        This is the most realistic scenario: the model fetches web pages that
        return 10K-50K chars each. This is where the overhead really hits.
        """
        full_messages = [_make_system("You are a research assistant.")]
        frontend_messages = [_make_system("You are a research assistant.")]

        # Turn 1: Simple search
        full_messages.extend([
            _make_user("What are the latest developments in quantum computing?"),
            _make_assistant_with_tools("Let me search for the latest news.", [
                _make_tool_call('tc_1', 'web_search', {'query': 'quantum computing latest 2026'}),
            ]),
            _make_tool_result('tc_1', "Search results:\n" + ("Result text. " * 100)),  # ~1.3K
            _make_assistant_with_tools("Let me fetch the top article.", [
                _make_tool_call('tc_2', 'fetch_url', {'url': 'https://example.com/quantum2026'}),
            ]),
            _make_tool_result('tc_2', "Full article content: " + ("x" * 20000)),  # 20K chars!
            _make_assistant_text("Based on the latest research, quantum computing has made significant progress..."),
        ])
        frontend_messages.extend([
            _make_user("What are the latest developments in quantum computing?"),
            _make_assistant_text("Based on the latest research, quantum computing has made significant progress..."),
        ])

        # Turn 2: Follow-up with multiple fetches
        full_messages.extend([
            _make_user("Compare Google and IBM's approaches"),
            _make_assistant_with_tools("Let me research both.", [
                _make_tool_call('tc_3', 'web_search', {'query': 'Google quantum computing approach 2026'}),
                _make_tool_call('tc_4', 'web_search', {'query': 'IBM quantum computing approach 2026'}),
            ]),
            _make_tool_result('tc_3', "Google results: " + ("g" * 1500)),
            _make_tool_result('tc_4', "IBM results: " + ("i" * 1500)),
            _make_assistant_with_tools("Let me get details from both.", [
                _make_tool_call('tc_5', 'fetch_url', {'url': 'https://ai.google/quantum'}),
                _make_tool_call('tc_6', 'fetch_url', {'url': 'https://research.ibm.com/quantum'}),
            ]),
            _make_tool_result('tc_5', "Google quantum page: " + ("g" * 30000)),  # 30K
            _make_tool_result('tc_6', "IBM quantum page: " + ("i" * 25000)),    # 25K
            _make_assistant_text("Comparing Google and IBM's quantum computing approaches..."),
        ])
        frontend_messages.extend([
            _make_user("Compare Google and IBM's approaches"),
            _make_assistant_text("Comparing Google and IBM's quantum computing approaches..."),
        ])

        # Turn 3: new user question
        new_q = "What about Microsoft's topological qubit approach?"
        full_msgs_turn3 = list(full_messages) + [_make_user(new_q)]
        frontend_msgs_turn3 = list(frontend_messages) + [_make_user(new_q)]

        overhead = estimate_token_overhead(frontend_msgs_turn3, full_msgs_turn3)

        print(f"\n{'='*60}")
        print("REALISTIC WEB RESEARCH (2 turns, 6 tool calls, ~80K tool content):")
        print(f"  Frontend messages:   {len(frontend_msgs_turn3)}")
        print(f"  Full messages:       {len(full_msgs_turn3)}")
        print(f"  Frontend (summary):  {overhead['frontend_chars']:,} chars ≈ {overhead['frontend_est_tokens']:,} tokens")
        print(f"  Full (with tools):   {overhead['stored_chars']:,} chars ≈ {overhead['stored_est_tokens']:,} tokens")
        print(f"  Overhead:            +{overhead['overhead_chars']:,} chars ≈ +{overhead['overhead_est_tokens']:,} tokens")
        print(f"  Ratio:               {overhead['ratio']}x")

        # With 80K of tool content, this should be a massive overhead
        assert overhead['ratio'] >= 3.0

    def test_code_project_session(self):
        """Simulate a code project session: read_file + grep + apply_diff.

        Code tools produce moderate-sized results (file contents, search matches).
        """
        full_messages = [_make_system("You are a code assistant.")]
        frontend_messages = [_make_system("You are a code assistant.")]

        # Turn 1: Read and understand code
        file_content = """class UserService:
    def __init__(self, db):
        self.db = db

    def get_user(self, user_id):
        return self.db.query("SELECT * FROM users WHERE id = ?", user_id)

    def create_user(self, name, email):
        return self.db.execute("INSERT INTO users (name, email) VALUES (?, ?)", name, email)

    def update_user(self, user_id, **kwargs):
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [user_id]
        return self.db.execute(f"UPDATE users SET {sets} WHERE id = ?", *values)

    def delete_user(self, user_id):
        return self.db.execute("DELETE FROM users WHERE id = ?", user_id)
""" * 3  # ~3K chars (repeated for size)

        full_messages.extend([
            _make_user("Help me add input validation to the UserService class"),
            _make_assistant_with_tools("Let me look at the code first.", [
                _make_tool_call('tc_1', 'read_file', {'path': 'src/services/user_service.py'}),
                _make_tool_call('tc_2', 'grep_search', {'pattern': 'class.*Service', 'path': 'src/'}),
            ]),
            _make_tool_result('tc_1', file_content),
            _make_tool_result('tc_2',
                "src/services/user_service.py:1: class UserService:\n"
                "src/services/auth_service.py:1: class AuthService:\n"
                "src/services/email_service.py:1: class EmailService:\n"),
            _make_assistant_with_tools("I see the code. Let me add validation.", [
                _make_tool_call('tc_3', 'apply_diff', {
                    'path': 'src/services/user_service.py',
                    'search': 'def create_user(self, name, email):',
                    'replace': 'def create_user(self, name, email):\n        if not name or not email:\n            raise ValueError("name and email are required")',
                }),
            ]),
            _make_tool_result('tc_3', "✅ Applied diff to src/services/user_service.py"),
            _make_assistant_text("I've added input validation to the `create_user` method..."),
        ])
        frontend_messages.extend([
            _make_user("Help me add input validation to the UserService class"),
            _make_assistant_text("I've added input validation to the `create_user` method..."),
        ])

        # Turn 2: More changes
        full_messages.extend([
            _make_user("Now add validation to update_user and delete_user too"),
            _make_assistant_with_tools("Let me re-read the file and apply changes.", [
                _make_tool_call('tc_4', 'read_file', {'path': 'src/services/user_service.py'}),
            ]),
            _make_tool_result('tc_4', file_content),  # re-read
            _make_assistant_with_tools("Applying changes.", [
                _make_tool_call('tc_5', 'apply_diff', {
                    'path': 'src/services/user_service.py',
                    'search': 'def update_user',
                    'replace': 'def update_user(validated)',
                }),
                _make_tool_call('tc_6', 'apply_diff', {
                    'path': 'src/services/user_service.py',
                    'search': 'def delete_user',
                    'replace': 'def delete_user(validated)',
                }),
            ]),
            _make_tool_result('tc_5', "✅ Applied diff to src/services/user_service.py"),
            _make_tool_result('tc_6', "✅ Applied diff to src/services/user_service.py"),
            _make_assistant_text("I've added validation to both update_user and delete_user methods."),
        ])
        frontend_messages.extend([
            _make_user("Now add validation to update_user and delete_user too"),
            _make_assistant_text("I've added validation to both update_user and delete_user methods."),
        ])

        # Turn 3
        new_q = "Now write unit tests for all three methods"
        full_msgs_turn3 = list(full_messages) + [_make_user(new_q)]
        frontend_msgs_turn3 = list(frontend_messages) + [_make_user(new_q)]

        overhead = estimate_token_overhead(frontend_msgs_turn3, full_msgs_turn3)

        print(f"\n{'='*60}")
        print("CODE PROJECT SESSION (2 turns, 6 tool calls, file reads):")
        print(f"  Frontend messages:   {len(frontend_msgs_turn3)}")
        print(f"  Full messages:       {len(full_msgs_turn3)}")
        print(f"  Frontend (summary):  {overhead['frontend_chars']:,} chars ≈ {overhead['frontend_est_tokens']:,} tokens")
        print(f"  Full (with tools):   {overhead['stored_chars']:,} chars ≈ {overhead['stored_est_tokens']:,} tokens")
        print(f"  Overhead:            +{overhead['overhead_chars']:,} chars ≈ +{overhead['overhead_est_tokens']:,} tokens")
        print(f"  Ratio:               {overhead['ratio']}x")


# ═══════════════════════════════════════════════════════════
#  Test: Information loss analysis
# ═══════════════════════════════════════════════════════════

class TestInformationLoss:
    """Measure what information the LLM loses when using frontend summaries."""

    def test_lost_tool_names_and_args(self):
        """In summary mode, the model doesn't know WHAT tools it called."""
        frontend = _build_frontend_summary_turn2()
        full = _build_full_history_turn2()

        # Count tool calls visible to the model
        frontend_tool_calls = sum(
            len(m.get('tool_calls', []))
            for m in frontend
        )
        full_tool_calls = sum(
            len(m.get('tool_calls', []))
            for m in full
        )

        print(f"\n{'='*60}")
        print("INFORMATION LOSS — Tool Call Visibility:")
        print(f"  Frontend: model sees {frontend_tool_calls} tool calls")
        print(f"  Full:     model sees {full_tool_calls} tool calls")
        print(f"  Lost:     {full_tool_calls - frontend_tool_calls} tool calls hidden from model")

        assert frontend_tool_calls == 0
        assert full_tool_calls > 0

    def test_lost_tool_results(self):
        """In summary mode, the model doesn't see WHAT the tools returned."""
        frontend = _build_frontend_summary_turn2()
        full = _build_full_history_turn2()

        frontend_tool_content = sum(
            len(m.get('content', ''))
            for m in frontend
            if m.get('role') == 'tool'
        )
        full_tool_content = sum(
            len(m.get('content', ''))
            for m in full
            if m.get('role') == 'tool'
        )

        print(f"\n{'='*60}")
        print("INFORMATION LOSS — Tool Result Content:")
        print(f"  Frontend: {frontend_tool_content:,} chars of tool results")
        print(f"  Full:     {full_tool_content:,} chars of tool results")
        print(f"  Lost:     {full_tool_content - frontend_tool_content:,} chars of context")

        assert frontend_tool_content == 0
        assert full_tool_content > 0

    def test_duplicate_search_risk(self):
        """Without tool history, the model may repeat the same searches.

        In full-history mode, the model can see it already searched for
        'Python GIL' and won't search again. In summary mode, it has no
        way to know what was already searched.
        """
        full = _build_full_history_turn1()

        # Extract all search queries from full history
        searched_queries = []
        for msg in full:
            for tc in msg.get('tool_calls', []):
                fn = tc.get('function', {})
                if fn.get('name') == 'web_search':
                    args = json.loads(fn.get('arguments', '{}'))
                    searched_queries.append(args.get('query', ''))

        print(f"\n{'='*60}")
        print("DUPLICATE SEARCH RISK:")
        print(f"  Queries visible in full history: {searched_queries}")
        print("  In summary mode: model has NO visibility into past searches")
        print(f"  Risk: model may re-search '{searched_queries[0]}' on follow-up turns")

        assert len(searched_queries) > 0


# ═══════════════════════════════════════════════════════════
#  Test: End-to-end integration with orchestrator data flow
# ═══════════════════════════════════════════════════════════

class TestOrchestratorIntegration:
    """Test that the store correctly integrates with orchestrator's message format."""

    def test_round_trip_save_rebuild(self):
        """Save after turn 1, rebuild for turn 2, save after turn 2, rebuild for turn 3."""
        conv_id = 'conv_roundtrip'

        # After turn 1: save full messages
        turn1_full = _build_full_history_turn1()
        save_messages(conv_id, turn1_full)

        # Turn 2 starts: frontend sends summary + new question
        frontend_turn2 = _build_frontend_summary_turn1() + [
            _make_user("How does asyncio compare to threading?"),
        ]
        rebuilt, stats = rebuild_messages_with_history(conv_id, frontend_turn2)

        # Verify rebuilt has tool history from turn 1
        assert stats['used_store'] is True
        tool_msgs = [m for m in rebuilt if m.get('role') == 'tool']
        assert len(tool_msgs) >= 1

        # After turn 2 completes: save the extended messages
        # (Simulate orchestrator extending the rebuilt messages with turn 2's tool calls)
        turn2_full = list(rebuilt)  # starts with rebuilt messages
        turn2_full.pop()  # remove the user question (it's already in rebuilt)
        turn2_full.append(_make_user("How does asyncio compare to threading?"))
        turn2_full.extend([
            _make_assistant_with_tools("Researching...", [
                _make_tool_call('tc_10', 'web_search', {'query': 'asyncio vs threading'}),
            ]),
            _make_tool_result('tc_10', "Asyncio uses event loop, threading uses OS threads..."),
            _make_assistant_text("Asyncio is better for I/O-bound tasks with high concurrency."),
        ])
        save_messages(conv_id, turn2_full)

        # Turn 3 starts: frontend sends summary of turns 1+2 + new question
        frontend_turn3 = _build_frontend_summary_turn2() + [
            _make_user("Show me a code example"),
        ]
        rebuilt3, stats3 = rebuild_messages_with_history(conv_id, frontend_turn3)

        # Now should have tool history from BOTH turns 1 and 2
        assert stats3['used_store'] is True
        all_tool_msgs = [m for m in rebuilt3 if m.get('role') == 'tool']
        assert len(all_tool_msgs) >= 2  # At least 1 from turn 1 + 1 from turn 2

        print(f"\n{'='*60}")
        print("ROUND TRIP:")
        print(f"  Turn 2 rebuilt: {len(rebuilt)} messages (stats: {stats})")
        print(f"  Turn 3 rebuilt: {len(rebuilt3)} messages (stats: {stats3})")
        print(f"  Tool messages preserved across 2 turns: {len(all_tool_msgs)}")

    def test_mixed_tool_and_text_turns(self):
        """Some turns have tools, some don't. Store should handle gracefully."""
        conv_id = 'conv_mixed'

        # Turn 1: with tools
        turn1 = _build_full_history_turn1()
        save_messages(conv_id, turn1)

        # Turn 2: no tools (just conversation)
        turn2_frontend = _build_frontend_summary_turn1() + [
            _make_user("Can you explain that more simply?"),
        ]
        rebuilt2, stats2 = rebuild_messages_with_history(conv_id, turn2_frontend)
        assert stats2['used_store'] is True

        # After turn 2: the full messages don't have tool calls for turn 2
        turn2_full = list(rebuilt2[:-1]) + [  # all but last user msg
            _make_user("Can you explain that more simply?"),
            _make_assistant_text("Simply put, the GIL is like a traffic cop..."),
        ]
        # Still has tool messages from turn 1, so save should work
        save_messages(conv_id, turn2_full)
        assert get_messages(conv_id) is not None


# ═══════════════════════════════════════════════════════════
#  Test: Comprehensive overhead comparison report
# ═══════════════════════════════════════════════════════════

class TestOverheadReport:
    """Generate a comprehensive report comparing overhead across scenarios."""

    def test_full_overhead_report(self):
        """Print a complete comparison table."""
        scenarios = [
            ("1 turn, 1 search",
             _build_frontend_summary_turn1(),
             _build_full_history_turn1()),
            ("2 turns, 3 searches + 1 fetch",
             _build_frontend_summary_turn2(),
             _build_full_history_turn2()),
        ]

        # Build more scenarios
        # Scenario 3: 10 tool calls with varying result sizes
        full_10tc = [_make_system("sys")]
        front_10tc = [_make_system("sys")]
        for i in range(5):
            q = f"Question {i}"
            a = f"Answer {i} " * 20
            full_10tc.append(_make_user(q))
            front_10tc.append(_make_user(q))

            tcs = [_make_tool_call(f'tc_{i}_{j}', 'web_search', {'query': f'q{i}_{j}'})
                   for j in range(2)]
            full_10tc.append(_make_assistant_with_tools(f"Searching for {q}...", tcs))
            for j in range(2):
                full_10tc.append(_make_tool_result(f'tc_{i}_{j}', f"Result {i}.{j} " * (200 * (i + 1))))

            full_10tc.append(_make_assistant_text(a))
            front_10tc.append(_make_assistant_text(a))

        scenarios.append(("5 turns, 10 searches (growing results)", front_10tc, full_10tc))

        # Scenario 4: Heavy fetch (3 turns, each with 20K fetch)
        full_heavy = [_make_system("sys")]
        front_heavy = [_make_system("sys")]
        for i in range(3):
            q = f"Research topic {i}"
            a = f"Research answer {i} " * 30
            full_heavy.append(_make_user(q))
            front_heavy.append(_make_user(q))

            full_heavy.append(_make_assistant_with_tools("Searching...", [
                _make_tool_call(f'tc_h_{i}', 'fetch_url', {'url': f'https://example.com/page{i}'}),
            ]))
            full_heavy.append(_make_tool_result(f'tc_h_{i}', "Page content: " + ("x" * 20000)))

            full_heavy.append(_make_assistant_text(a))
            front_heavy.append(_make_assistant_text(a))

        scenarios.append(("3 turns, 3 fetch_url (20K each)", front_heavy, full_heavy))

        print(f"\n\n{'='*80}")
        print("  COMPREHENSIVE OVERHEAD COMPARISON REPORT")
        print(f"{'='*80}")
        print(f"{'Scenario':<45} {'Frontend':>10} {'Full':>10} {'Overhead':>10} {'Ratio':>8}")
        print(f"{'-'*45} {'-'*10} {'-'*10} {'-'*10} {'-'*8}")

        for name, frontend, full in scenarios:
            oh = estimate_token_overhead(frontend, full)
            print(f"{name:<45} {oh['frontend_est_tokens']:>8}tk {oh['stored_est_tokens']:>8}tk "
                  f"+{oh['overhead_est_tokens']:>7}tk {oh['ratio']:>6.1f}x")

        print(f"\n{'='*80}")
        print("Notes:")
        print("  - Token estimates use ~4 chars/token approximation")
        print("  - Real-world tool results (fetch_url) can be 10K-100K chars each")
        print("  - Overhead grows linearly with # of tool calls and result sizes")
        print("  - Benefits: model avoids duplicate searches, can reference past results")
        print("  - Risk: context window exhaustion on long conversations")
        print("  - Mitigation: truncate old tool results, use compaction for full history")
        print(f"{'='*80}\n")


# ═══════════════════════════════════════════════════════════
#  Test: Truncation strategies for managing overhead
# ═══════════════════════════════════════════════════════════

class TestTruncationStrategies:
    """Test strategies to reduce overhead while preserving key information."""

    @staticmethod
    def _truncate_old_tool_results(messages, max_result_chars=2000, preserve_last_n_turns=1):
        """Truncate tool results from older turns to manage context size.

        Strategy: Keep full tool results for the most recent N turns,
        truncate older ones to max_result_chars with a summary note.
        """
        # Find turn boundaries (user messages)
        user_indices = [i for i, m in enumerate(messages) if m.get('role') == 'user']
        if len(user_indices) <= preserve_last_n_turns:
            return messages  # Not enough turns to truncate

        # Messages before the last N user messages are "old"
        cutoff_idx = user_indices[-preserve_last_n_turns]

        truncated = []
        trunc_count = 0
        for i, msg in enumerate(messages):
            if i < cutoff_idx and msg.get('role') == 'tool':
                content = msg.get('content', '')
                if isinstance(content, str) and len(content) > max_result_chars:
                    truncated_content = (
                        content[:max_result_chars] +
                        f'\n\n[... truncated, originally {len(content):,} chars]'
                    )
                    truncated.append({**msg, 'content': truncated_content})
                    trunc_count += 1
                else:
                    truncated.append(msg)
            else:
                truncated.append(msg)
        return truncated

    def test_truncation_reduces_overhead(self):
        """Verify that truncating old tool results significantly reduces overhead."""
        full = _build_full_history_turn2()

        # Add another turn with a new user message
        full_with_new = list(full) + [_make_user("New question")]

        # Truncate old results (keep last 1 turn full)
        truncated = self._truncate_old_tool_results(full_with_new, max_result_chars=200)

        frontend = _build_frontend_summary_turn2() + [_make_user("New question")]

        oh_full = estimate_token_overhead(frontend, full_with_new)
        oh_truncated = estimate_token_overhead(frontend, truncated)

        print(f"\n{'='*60}")
        print("TRUNCATION STRATEGY (max_result_chars=200, preserve_last_1_turn):")
        print(f"  Frontend (summary):     {oh_full['frontend_est_tokens']:,} tokens")
        print(f"  Full (no truncation):   {oh_full['stored_est_tokens']:,} tokens ({oh_full['ratio']:.1f}x)")
        print(f"  Full (with truncation): {oh_truncated['stored_est_tokens']:,} tokens ({oh_truncated['ratio']:.1f}x)")
        print(f"  Savings from truncation: {oh_full['stored_est_tokens'] - oh_truncated['stored_est_tokens']:,} tokens")

        # Truncated should be smaller than full but still larger than frontend
        assert oh_truncated['stored_chars'] < oh_full['stored_chars']
        assert oh_truncated['stored_chars'] > oh_full['frontend_chars']

    def test_tool_name_only_strategy(self):
        """Alternative: keep tool names/args but strip results entirely for old turns.

        This preserves "what was done" without the full results.
        """
        full = _build_full_history_turn2()

        # Strategy: replace old tool results with just a marker
        stripped = []
        user_indices = [i for i, m in enumerate(full) if m.get('role') == 'user']
        cutoff = user_indices[-1] if user_indices else len(full)

        for i, msg in enumerate(full):
            if i < cutoff and msg.get('role') == 'tool':
                tc_id = msg.get('tool_call_id', '?')
                stripped.append({
                    'role': 'tool',
                    'tool_call_id': tc_id,
                    'content': '[Result available but omitted for context savings]',
                })
            else:
                stripped.append(msg)

        frontend = _build_frontend_summary_turn2()
        oh_full = estimate_token_overhead(frontend, full)
        oh_stripped = estimate_token_overhead(frontend, stripped)

        print(f"\n{'='*60}")
        print("TOOL-NAME-ONLY STRATEGY (strip old results, keep names/args):")
        print(f"  Frontend (summary):     {oh_full['frontend_est_tokens']:,} tokens")
        print(f"  Full (all results):     {oh_full['stored_est_tokens']:,} tokens ({oh_full['ratio']:.1f}x)")
        print(f"  Names-only (stripped):  {oh_stripped['stored_est_tokens']:,} tokens ({oh_stripped['ratio']:.1f}x)")

        # Names-only should be much closer to frontend size
        assert oh_stripped['stored_chars'] < oh_full['stored_chars']


class TestTruncationIdempotency:
    """The real module-level _truncate_old_tool_results must be a FIXPOINT.

    Root cause of the "two consecutive turns full cache_write, no error"
    bug (conv mqovcjp2, 2026-06-22): the truncator runs at the START of
    every turn against the server-stored messages (live dict refs). The
    old skip guard only recognised the micro-compact '[...compacted...]'
    marker — NOT its own '[... truncated — was N chars ...]' note. So an
    already-truncated old tool result got re-truncated every turn and the
    embedded char count drifted, silently rewriting bytes inside the cached
    prompt prefix → PREFIX MUTATION → whole body re-billed uncached.
    """

    def _big_old_history(self):
        """Two completed turns + a new user msg, with a fat OLD tool result."""
        from lib.tasks_pkg.server_message_store import _OLD_RESULT_MAX_CHARS
        big = 'X' * (_OLD_RESULT_MAX_CHARS * 4)
        return [
            _make_system("sys"),
            _make_user("turn 1 question"),
            _make_assistant_with_tools(
                "searching",
                [_make_tool_call('tc_old', 'web_search', {'q': 'foo'})]),
            _make_tool_result('tc_old', big),
            _make_assistant_text("turn 1 answer"),
            _make_user("turn 2 question"),       # second-to-last user → old boundary
            _make_assistant_text("turn 2 answer"),
            _make_user("turn 3 NEW question"),   # current turn
        ]

    def test_truncates_once_then_is_fixpoint(self):
        """First pass truncates the fat old result; a second pass is a NO-OP
        and leaves the bytes byte-identical (the cache-critical property)."""
        from lib.tasks_pkg.server_message_store import _truncate_old_tool_results

        msgs = self._big_old_history()

        n1 = _truncate_old_tool_results(msgs)
        assert n1 == 1, "first pass should truncate the one fat old result"
        after_first = msgs[3]['content']
        assert 'truncated —' in after_first

        # Re-run on the SAME (already-truncated) list — must NOT touch it again.
        n2 = _truncate_old_tool_results(msgs)
        assert n2 == 0, "second pass must be a no-op (idempotent)"
        assert msgs[3]['content'] == after_first, \
            "re-truncation rewrote prefix bytes — would cause a cache miss"

    def test_repeated_passes_never_drift(self):
        """Five passes (simulating five turns) keep the old result stable."""
        from lib.tasks_pkg.server_message_store import _truncate_old_tool_results

        msgs = self._big_old_history()
        _truncate_old_tool_results(msgs)
        stable = msgs[3]['content']
        for _ in range(5):
            _truncate_old_tool_results(msgs)
            assert msgs[3]['content'] == stable

    def test_already_compacted_marker_still_skipped(self):
        """The pre-existing '[...compacted...]' guard must still hold."""
        from lib.tasks_pkg.server_message_store import (
            _OLD_RESULT_MAX_CHARS, _truncate_old_tool_results,
        )
        compacted = '[' + 'compacted ' + ('Y' * (_OLD_RESULT_MAX_CHARS * 3))
        msgs = [
            _make_system("sys"),
            _make_user("turn 1"),
            _make_tool_result('tc_c', compacted),
            _make_assistant_text("a1"),
            _make_user("turn 2"),
            _make_assistant_text("a2"),
            _make_user("turn 3 NEW"),
        ]
        assert _truncate_old_tool_results(msgs) == 0


class TestTruncationCachePrefixGate:
    """The truncator must NOT mutate a tool result that is still inside the
    Anthropic-cached prefix, even when it is already past the turn boundary.

    Root cause (conv mqq5strz, 2026-06-23): a large tool result enters the
    list at FULL size in the round it ran (→ cached). The next round a new
    user message advances ``old_boundary`` past it, so the truncator would
    truncate it FOR THE FIRST TIME — while it is STILL inside
    ``messages[0:get_cache_prefix_count(conv)]``. That first-time edit
    rewrites already-cached bytes → PREFIX MUTATION → whole body re-billed
    every round (cache_read pinned, cache_write climbing). The idempotency
    marker does nothing here because it is the FIRST truncation. Fix: skip
    any index inside the cached prefix; truncate later once it scrolls out.
    """

    def _seed_cache_state(self, conv_id, message_count, read_tokens=60000):
        """Make get_cache_prefix_count(conv_id) return message_count-2."""
        from lib.tasks_pkg import cache_tracking as ct
        with ct._cache_lock:
            st = ct.CacheState()
            st.message_count = message_count
            st.last_cache_read_tokens = read_tokens
            st.call_count = 1
            ct._cache_states[conv_id] = st

    def teardown_method(self, _m):
        from lib.tasks_pkg import cache_tracking as ct
        with ct._cache_lock:
            ct._cache_states.pop('cp_gate_conv', None)

    def _history_with_fat_old_result(self):
        from lib.tasks_pkg.server_message_store import _OLD_RESULT_MAX_CHARS
        big = 'X' * (_OLD_RESULT_MAX_CHARS * 4)
        msgs = [
            _make_system("sys"),                                    # 0
            _make_user("turn 1 question"),                          # 1
            _make_assistant_with_tools(                             # 2
                "searching",
                [_make_tool_call('tc_old', 'web_search', {'q': 'foo'})]),
            _make_tool_result('tc_old', big),                       # 3  ← fat old result
            _make_assistant_text("turn 1 answer"),                  # 4
            _make_user("turn 2 question"),                          # 5  ← old_boundary
            _make_assistant_text("turn 2 answer"),                  # 6
            _make_user("turn 3 NEW question"),                      # 7  ← current
        ]
        return msgs

    def test_skips_result_inside_cache_prefix(self):
        """With a cache prefix covering index 3, the fat old result (idx 3)
        must be left byte-identical even though it is past old_boundary (5)."""
        from lib.tasks_pkg.server_message_store import _truncate_old_tool_results

        msgs = self._history_with_fat_old_result()
        before = msgs[3]['content']
        # prefix_n = message_count-2 = 6 → indices 0..5 are cached; idx 3 is in.
        self._seed_cache_state('cp_gate_conv', message_count=8)

        n = _truncate_old_tool_results(msgs, conv_id='cp_gate_conv')
        assert n == 0, "result inside cache prefix must not be truncated"
        assert msgs[3]['content'] == before, \
            "truncating inside the cached prefix rewrote bytes → cache miss"

    def test_truncates_once_result_leaves_prefix(self):
        """When the cache prefix no longer covers the fat result, it IS
        truncated (the deferred, cache-safe truncation)."""
        from lib.tasks_pkg.server_message_store import _truncate_old_tool_results

        msgs = self._history_with_fat_old_result()
        # prefix_n = message_count-2 = 2 → only indices 0,1 cached; idx 3 is OUT.
        self._seed_cache_state('cp_gate_conv', message_count=4)

        n = _truncate_old_tool_results(msgs, conv_id='cp_gate_conv')
        assert n == 1, "result outside the prefix should be truncated"
        assert 'truncated —' in msgs[3]['content']

    def test_no_conv_id_falls_back_to_turn_boundary(self):
        """Without conv_id (no cache state), behaviour is unchanged: the fat
        old result past the boundary is truncated as before."""
        from lib.tasks_pkg.server_message_store import _truncate_old_tool_results

        msgs = self._history_with_fat_old_result()
        n = _truncate_old_tool_results(msgs)  # no conv_id
        assert n == 1
        assert 'truncated —' in msgs[3]['content']


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
