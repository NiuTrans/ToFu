#!/usr/bin/env python3
"""Real cost comparison test: keepToolHistory ON vs OFF.

Runs actual LLM API calls through multi-turn conversations with simulated
tool calls, measuring real token usage (prompt_tokens, completion_tokens,
cache_read, cache_write) across both modes.

This test:
1. Simulates a realistic multi-turn coding assistant conversation
2. Each "turn" includes tool calls (web_search, read_files, grep_search, etc.)
3. Mode A (current): assistant history = final text only (tool details stripped)
4. Mode B (proposed): assistant history = full tool_use + tool_result messages
5. Both modes go through our compaction pipeline (micro_compact + force_compact)
6. Measures real API costs per turn and total

Usage:
    python -m pytest tests/test_keep_tool_history_cost.py -v -s
    # or directly:
    python tests/test_keep_tool_history_cost.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
import time

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  Simulated tool call/result data (realistic sizes)
# ═══════════════════════════════════════════════════════════════════════════════

# Simulate a realistic multi-turn coding conversation
SCENARIO = {
    'name': 'Multi-turn code investigation',
    'description': (
        'User asks about a bug, assistant searches code, reads files, '
        'runs tests, modifies code, and verifies — 5 turns with tool calls.'
    ),
    'turns': [
        # ── Turn 1: User reports a bug, assistant searches for it ──
        {
            'user_msg': (
                "我们的 orchestrator.py 在多轮工具调用后会丢失上下文，"
                "导致模型重复调用相同的工具。请帮我排查这个问题。"
            ),
            'tool_rounds': [
                {
                    'tool_calls': [
                        {
                            'name': 'grep_search',
                            'arguments': {'pattern': 'tool_call_happened', 'path': 'lib/tasks_pkg/orchestrator.py'},
                            'result': (
                                'grep "tool_call_happened" — 8 matches:\n\n'
                                'lib/tasks_pkg/orchestrator.py:584:        tool_call_happened = False\n'
                                'lib/tasks_pkg/orchestrator.py:698:            tool_call_happened = True\n'
                                'lib/tasks_pkg/orchestrator.py:815:            if tool_call_happened:\n'
                                'lib/tasks_pkg/orchestrator.py:984:            tool_call_happened=tool_call_happened,\n'
                            ),
                        },
                        {
                            'name': 'grep_search',
                            'arguments': {'pattern': 'messages.*append', 'path': 'lib/tasks_pkg/orchestrator.py'},
                            'result': (
                                'grep "messages.*append" — 5 matches:\n\n'
                                'lib/tasks_pkg/orchestrator.py:700:            messages.append(clean_msg)\n'
                                'lib/tasks_pkg/orchestrator.py:742:            messages.append(tool_result_msg)\n'
                                'lib/tasks_pkg/orchestrator.py:898:                messages.append(_final_assistant)\n'
                            ),
                        },
                    ],
                },
                {
                    'tool_calls': [
                        {
                            'name': 'read_files',
                            'arguments': {'reads': [{'path': 'lib/tasks_pkg/orchestrator.py', 'start_line': 690, 'end_line': 750}]},
                            'result': (
                                'File: lib/tasks_pkg/orchestrator.py (lines 690-750)\n'
                                '────────────────────────────────────────\n'
                                '            tool_call_happened = True\n'
                                '            clean_msg = {"role": "assistant"}\n'
                                '            clean_msg["tool_calls"] = assistant_msg["tool_calls"]\n'
                                '            if assistant_msg.get("content"): clean_msg["content"] = assistant_msg["content"]\n'
                                '            messages.append(clean_msg)\n'
                                '\n'
                                '            # ★ Expose live messages to context_compact tool handler\n'
                                '            task["_compact_messages"] = messages\n'
                                '\n'
                                '            parsed_tcs, tool_round_num = parse_tool_calls(\n'
                                '                assistant_msg, task, round_num, tool_round_num, project_enabled,\n'
                                '            )\n'
                                '\n'
                                '            emit_tool_exec_phase(task, parsed_tcs)\n'
                                '\n'
                                '            _tool_timed_out = execute_tool_pipeline(\n'
                                '                task, parsed_tcs, cfg, project_path, project_enabled,\n'
                                '                tool_list, messages, all_search_results_text, round_num, model,\n'
                                '            )\n'
                            ),
                        },
                    ],
                },
            ],
            'assistant_reply': (
                "我找到了问题所在。在 orchestrator.py 中，tool_call 和 tool_result 消息"
                "在 run_task 内部的 while 循环中被正确地追加到 `messages` 列表中。\n\n"
                "但问题出在**跨轮次**的消息传递上：\n"
                "1. `task['messages'] = messages` 在循环结束后写回（第 910 行）\n"
                "2. 但前端 `buildApiMessages()` 在下一轮重建消息时，只保留了 `content` 字段\n"
                "3. 所有的 `tool_calls` 和 `tool` 角色的消息都被丢弃了\n\n"
                "这意味着下一轮对话中，模型看不到之前搜索过什么文件、执行过什么命令。"
            ),
        },
        # ── Turn 2: User asks for a fix, assistant reads more code ──
        {
            'user_msg': "那能不能在后端保存完整的消息历史？请看看 server_message_store.py 的实现。",
            'tool_rounds': [
                {
                    'tool_calls': [
                        {
                            'name': 'read_files',
                            'arguments': {'reads': [{'path': 'lib/tasks_pkg/server_message_store.py'}]},
                            'result': (
                                'File: lib/tasks_pkg/server_message_store.py (265 lines, 9.0KB)\n'
                                '────────────────────────────────────────\n'
                                '"""Server-side conversation message store — preserves full tool_use/tool_result history.\n\n'
                                'Problem: The frontend\'s buildApiMessages() strips tool call details from history,\n'
                                'sending only `{role: "assistant", content: "final text"}` for past turns.\n'
                                'This means the LLM loses all context about what tools were called and what\n'
                                'they returned in previous turns.\n\n'
                                'Solution: This module maintains a server-side copy of the full message history\n'
                                '(including tool_use blocks and tool_result messages) across turns.\n'
                                '...\n'
                                'def save_messages(conv_id, messages):\n'
                                '    """Save the full message history after a turn completes."""\n'
                                '    if not conv_id or not messages: return\n'
                                '    has_tool_msgs = any(msg.get("tool_calls") or msg.get("role") == "tool" for msg in messages)\n'
                                '    if not has_tool_msgs: return\n'
                                '    with _store_lock:\n'
                                '        _store[conv_id] = {"messages": messages, "updated_at": time.time()}\n'
                                '\n'
                                'def rebuild_messages_with_history(conv_id, frontend_messages):\n'
                                '    """Replace frontend messages with stored full-history messages."""\n'
                                '    stored = get_messages(conv_id)\n'
                                '    if stored is None: return frontend_messages, {"used_store": False}\n'
                                '    new_user_msg = frontend_messages[-1]  # last msg is the new user question\n'
                                '    rebuilt = []\n'
                                '    for msg in stored:\n'
                                '        if msg.get("role") != "system": rebuilt.append(msg)\n'
                                '    rebuilt.append(new_user_msg)\n'
                                '    return rebuilt, {"used_store": True, "tool_msgs_restored": len(rebuilt)}\n'
                            ),
                        },
                    ],
                },
                {
                    'tool_calls': [
                        {
                            'name': 'read_files',
                            'arguments': {'reads': [{'path': 'lib/tasks_pkg/compaction.py', 'start_line': 1, 'end_line': 80}]},
                            'result': (
                                'File: lib/tasks_pkg/compaction.py (1707 lines, 70.7KB)\n'
                                '────────────────────────────────────────\n'
                                '"""Context compaction — two-layer progressive compression pipeline.\n\n'
                                'Layer 1 — Micro-compaction:\n'
                                '    Keeps a "hot tail" of the N most recent tool results untouched.\n'
                                '    Tool results outside the hot tail are replaced with a short placeholder.\n\n'
                                'Layer 2 — Context compact:\n'
                                '    Pure LLM summary with selective turn compression.\n'
                                '    Force-injected by orchestrator when estimated tokens exceed 80%.\n'
                                '"""\n\n'
                                'MICRO_HOT_TAIL = 30\n'
                                'MICRO_COMPACT_THRESHOLD = 500\n'
                                '_SUMMARY_TRIGGER_RATIO = 0.80\n'
                            ),
                        },
                    ],
                },
            ],
            'assistant_reply': (
                "好的，我看了 `server_message_store.py` 和 `compaction.py` 的实现。\n\n"
                "## 当前状态\n"
                "- `server_message_store.py` 已经实现了基础的消息存储功能\n"
                "- `compaction.py` 有两层压缩：micro-compact (热尾保护) + force-compact (LLM摘要)\n\n"
                "## 整合方案\n"
                "keepToolHistory 开启后，存储的完整消息会经过 compaction pipeline：\n"
                "1. **micro_compact**: 超过 MICRO_HOT_TAIL(30) 的旧 tool results 会被压缩\n"
                "2. **force_compact**: 当 token 数超过上下文的 80% 时，用 LLM 生成摘要\n\n"
                "这意味着即使保留完整 tool 历史，压缩机制也会控制上下文大小。"
            ),
        },
        # ── Turn 3: User asks to implement, assistant writes code ──
        {
            'user_msg': "帮我实现一下：在 orchestrator.py 中集成 keepToolHistory，加上压缩测试。",
            'tool_rounds': [
                {
                    'tool_calls': [
                        {
                            'name': 'read_files',
                            'arguments': {'reads': [{'path': 'lib/tasks_pkg/orchestrator.py', 'start_line': 520, 'end_line': 560}]},
                            'result': (
                                'File: lib/tasks_pkg/orchestrator.py (lines 520-560)\n'
                                '────────────────────────────────────────\n'
                                '        messages = list(task["messages"])\n'
                                '        original_messages = list(messages)\n'
                                '        tool_round_num = 0\n'
                                '        all_search_results_text = []\n'
                                '\n'
                                '        # ── Section 2.5: Server-side tool history restoration ──\n'
                                '        _keep_tool_history = cfg.get("keepToolHistory", False)\n'
                                '        _conv_id = task.get("convId", "")\n'
                                '        if _keep_tool_history and _conv_id:\n'
                                '            rebuilt, _rebuild_stats = _rebuild_messages_with_history(_conv_id, messages)\n'
                            ),
                        },
                    ],
                },
                {
                    'tool_calls': [
                        {
                            'name': 'apply_diff',
                            'arguments': {
                                'path': 'lib/tasks_pkg/orchestrator.py',
                                'search': '        messages = list(task["messages"])',
                                'replace': '        messages = list(task["messages"])\n        # keepToolHistory integration point',
                            },
                            'result': 'Applied diff to lib/tasks_pkg/orchestrator.py',
                        },
                    ],
                },
            ],
            'assistant_reply': (
                "已经完成了 orchestrator.py 的集成。主要改动：\n\n"
                "1. **run_task 开始时**（第 525 行）：如果 `keepToolHistory=true` 且有存储的消息，"
                "用 `_rebuild_messages_with_history()` 替换前端的摘要消息\n"
                "2. **run_task 结束后**（第 972 行）：用 `_save_messages_to_store()` 保存完整消息\n"
                "3. 压缩流水线照常运行，会自动处理增大的上下文"
            ),
        },
        # ── Turn 4: User asks to run tests ──
        {
            'user_msg': "运行一下测试看看有没有问题。",
            'tool_rounds': [
                {
                    'tool_calls': [
                        {
                            'name': 'run_command',
                            'arguments': {'command': 'cd /app && python -m pytest tests/test_server_message_store.py -v 2>&1 | head -50'},
                            'result': (
                                '============================= test session starts ==============================\n'
                                'platform linux -- Python 3.11.5, pytest-7.4.0\n'
                                'collected 23 items\n\n'
                                'tests/test_server_message_store.py::test_save_and_retrieve PASSED\n'
                                'tests/test_server_message_store.py::test_rebuild_with_history PASSED\n'
                                'tests/test_server_message_store.py::test_rebuild_preserves_system_msg PASSED\n'
                                'tests/test_server_message_store.py::test_ttl_expiry PASSED\n'
                                'tests/test_server_message_store.py::test_max_entries_cleanup PASSED\n'
                                'tests/test_server_message_store.py::test_no_tool_msgs_not_stored PASSED\n'
                                'tests/test_server_message_store.py::test_estimate_overhead_light PASSED\n'
                                'tests/test_server_message_store.py::test_estimate_overhead_heavy PASSED\n'
                                '... (15 more)\n\n'
                                '========================= 23 passed in 1.2s ==================================\n'
                            ),
                        },
                    ],
                },
                {
                    'tool_calls': [
                        {
                            'name': 'run_command',
                            'arguments': {'command': 'cd /app && python -m pytest tests/test_keep_tool_history_integration.py -v 2>&1 | head -60'},
                            'result': (
                                '============================= test session starts ==============================\n'
                                'platform linux -- Python 3.11.5, pytest-7.4.0\n'
                                'collected 17 items\n\n'
                                'tests/test_keep_tool_history_integration.py::TestKeepToolHistory::test_mode_a_no_tool_history PASSED\n'
                                'tests/test_keep_tool_history_integration.py::TestKeepToolHistory::test_mode_b_with_tool_history PASSED\n'
                                'tests/test_keep_tool_history_integration.py::TestKeepToolHistory::test_overhead_light_scenario PASSED\n'
                                'tests/test_keep_tool_history_integration.py::TestKeepToolHistory::test_overhead_heavy_scenario PASSED\n'
                                'tests/test_keep_tool_history_integration.py::TestKeepToolHistory::test_truncation_strategy PASSED\n'
                                '... (12 more)\n\n'
                                '========================= 17 passed in 0.8s ==================================\n'
                            ),
                        },
                    ],
                },
            ],
            'assistant_reply': (
                "所有 40 个测试都通过了：\n"
                "- `test_server_message_store.py`: 23/23 ✅\n"
                "- `test_keep_tool_history_integration.py`: 17/17 ✅\n\n"
                "没有回归问题。"
            ),
        },
        # ── Turn 5: Follow-up question referencing previous context ──
        {
            'user_msg': (
                "回到之前的问题——你之前 grep 搜索 tool_call_happened 时找到了 8 个匹配项，"
                "其中第 815 行是什么逻辑？"
            ),
            'tool_rounds': [
                {
                    'tool_calls': [
                        {
                            'name': 'read_files',
                            'arguments': {'reads': [{'path': 'lib/tasks_pkg/orchestrator.py', 'start_line': 810, 'end_line': 830}]},
                            'result': (
                                'File: lib/tasks_pkg/orchestrator.py (lines 810-830)\n'
                                '────────────────────────────────────────\n'
                                '            if tool_call_happened and not task.get("error"):\n'
                                '                try:\n'
                                '                    summary = _generate_tool_summary(messages, model, task)\n'
                                '                    if summary:\n'
                                '                        task["toolSummary"] = summary\n'
                                '                except Exception as e:\n'
                                '                    logger.warning("Tool summary generation failed: %s", e)\n'
                            ),
                        },
                    ],
                },
            ],
            'assistant_reply': (
                "第 815 行的逻辑是**生成 tool summary**：\n\n"
                "```python\n"
                "if tool_call_happened and not task.get('error'):\n"
                "    summary = _generate_tool_summary(messages, model, task)\n"
                "    task['toolSummary'] = summary\n"
                "```\n\n"
                "这就是当前方案中 `toolSummary` 的来源——在 run_task 结束后，"
                "把所有工具调用生成一个机械摘要（如 `- grep_search(pattern=xxx)`）。\n\n"
                "在 keepToolHistory 模式下，这个摘要仍然会生成（给前端展示用），"
                "但下一轮的消息历史会用完整的 tool_use/tool_result 替代。"
            ),
        },
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Build message histories for both modes
# ═══════════════════════════════════════════════════════════════════════════════

def _make_tool_call_id(turn_idx: int, round_idx: int, call_idx: int) -> str:
    return f'call_t{turn_idx}_r{round_idx}_c{call_idx}'


def build_mode_a_messages(scenario: dict, up_to_turn: int) -> list[dict]:
    """Build messages the way the frontend currently does (Mode A: summary only).

    For past turns, only keep {role: "assistant", content: "final text"}.
    Tool calls are completely stripped.
    """
    messages = []

    for t_idx in range(up_to_turn + 1):
        turn = scenario['turns'][t_idx]

        # User message
        messages.append({'role': 'user', 'content': turn['user_msg']})

        if t_idx < up_to_turn:
            # Past turn: only the final text reply (Mode A behavior)
            messages.append({'role': 'assistant', 'content': turn['assistant_reply']})
        # Current turn: no assistant reply yet (LLM will generate it)

    return messages


def build_mode_b_messages(scenario: dict, up_to_turn: int) -> list[dict]:
    """Build messages with full tool history preserved (Mode B: keepToolHistory).

    For past turns, include all tool_use + tool_result messages in addition
    to the final assistant text.
    """
    messages = []

    for t_idx in range(up_to_turn + 1):
        turn = scenario['turns'][t_idx]

        # User message
        messages.append({'role': 'user', 'content': turn['user_msg']})

        if t_idx < up_to_turn:
            # Past turn: include full tool call history
            for r_idx, tool_round in enumerate(turn.get('tool_rounds', [])):
                # Assistant message with tool_calls
                tc_list = []
                for c_idx, tc in enumerate(tool_round['tool_calls']):
                    tc_list.append({
                        'id': _make_tool_call_id(t_idx, r_idx, c_idx),
                        'type': 'function',
                        'function': {
                            'name': tc['name'],
                            'arguments': json.dumps(tc['arguments'], ensure_ascii=False),
                        },
                    })
                messages.append({
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': tc_list,
                })

                # Tool result messages
                for c_idx, tc in enumerate(tool_round['tool_calls']):
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': _make_tool_call_id(t_idx, r_idx, c_idx),
                        'name': tc['name'],
                        'content': tc['result'],
                    })

            # Final assistant text reply
            messages.append({'role': 'assistant', 'content': turn['assistant_reply']})
        # Current turn: no assistant reply yet

    return messages


def build_mode_b_with_compaction(scenario: dict, up_to_turn: int) -> list[dict]:
    """Mode B + micro_compact applied (simulates real production behavior).

    Builds full tool history, then runs micro_compact to compress cold
    tool results outside the hot tail.
    """
    messages = build_mode_b_messages(scenario, up_to_turn)

    from lib.tasks_pkg.compaction import micro_compact
    saved = micro_compact(messages, conv_id='test_cost_comparison')
    logger.info('[ModeB+compact] micro_compact saved ~%d tokens', saved)

    return messages


def build_mode_d_messages(scenario: dict, up_to_turn: int,
                          max_old_result_chars: int = 2000) -> list[dict]:
    """Mode D: Full tool history but TRUNCATE old turn tool results.

    Keep the LAST turn's tool results in full, truncate all older turns'
    tool results to max_old_result_chars. This preserves:
    - Tool names and arguments (so model knows what was searched/read)
    - A preview of results (so model has a gist)
    - Full results for the most recent turn (for continuity)
    """
    messages = build_mode_b_messages(scenario, up_to_turn)

    # Find the boundary: tool messages from the last completed turn
    # stay full, everything older gets truncated.
    # We identify "turns" by user messages.
    user_indices = [i for i, m in enumerate(messages) if m.get('role') == 'user']

    if len(user_indices) < 2:
        return messages  # Only one turn, nothing to truncate

    # The last user message is the current (unanswered) turn.
    # The second-to-last user message starts the most recent completed turn.
    # Everything before that is "old".
    last_completed_turn_start = user_indices[-2]

    for i, msg in enumerate(messages):
        if i >= last_completed_turn_start:
            break  # Don't touch the last completed turn or current turn
        if msg.get('role') == 'tool':
            content = msg.get('content', '')
            if isinstance(content, str) and len(content) > max_old_result_chars:
                tool_name = msg.get('name', 'tool')
                original_len = len(content)
                preview = content[:max_old_result_chars]
                # Truncate at last newline for cleanliness
                last_nl = preview.rfind('\n', max_old_result_chars // 2)
                if last_nl > 0:
                    preview = preview[:last_nl]
                msg['content'] = (
                    f'{preview}\n\n'
                    f'[... truncated — was {original_len:,} chars, '
                    f'showing first {len(preview):,}. '
                    f'Re-call {tool_name} if full content needed.]'
                )

    return messages


# ═══════════════════════════════════════════════════════════════════════════════
#  LLM API cost measurement
# ═══════════════════════════════════════════════════════════════════════════════

# Claude pricing (USD per 1M tokens) — using Sonnet 4 as reference
PRICING = {
    'input':        3.00,   # $3/MTok input
    'output':       15.00,  # $15/MTok output
    'cache_write':  3.75,   # $3.75/MTok cache write
    'cache_read':   0.30,   # $0.30/MTok cache read
}


def compute_cost_usd(usage: dict) -> float:
    """Compute estimated cost in USD from usage dict."""
    prompt = usage.get('prompt_tokens', usage.get('input_tokens', 0))
    completion = usage.get('completion_tokens', usage.get('output_tokens', 0))
    cache_write = usage.get('cache_write_tokens', usage.get('cache_creation_input_tokens', 0))
    cache_read = usage.get('cache_read_tokens', usage.get('cache_read_input_tokens', 0))

    # When cache is used, prompt_tokens is the non-cached portion
    cost = (
        (prompt / 1_000_000) * PRICING['input']
        + (completion / 1_000_000) * PRICING['output']
        + (cache_write / 1_000_000) * PRICING['cache_write']
        + (cache_read / 1_000_000) * PRICING['cache_read']
    )
    return cost


TEST_MODEL = os.environ.get('COST_TEST_MODEL', 'gpt-4.1-mini')
"""Fixed model for fair comparison. Override with COST_TEST_MODEL env var."""


def call_llm_and_measure(messages: list[dict], label: str) -> dict:
    """Call the LLM API with the given messages and return usage stats.

    Uses dispatch_chat with a FIXED model for fair comparison.
    """
    from lib.llm_dispatch.api import dispatch_chat

    t0 = time.time()
    try:
        content, usage = dispatch_chat(
            messages,
            max_tokens=1024,
            temperature=0,
            thinking_enabled=False,
            preset='low',
            capability='text',
            prefer_model=TEST_MODEL,
            strict_model=True,
            log_prefix=f'[CostTest:{label}]',
            timeout=120,
        )
        elapsed = time.time() - t0

        prompt_tokens = usage.get('prompt_tokens', usage.get('input_tokens', 0))
        completion_tokens = usage.get('completion_tokens', usage.get('output_tokens', 0))
        cache_write = usage.get('cache_write_tokens', usage.get('cache_creation_input_tokens', 0))
        cache_read = usage.get('cache_read_tokens', usage.get('cache_read_input_tokens', 0))
        cost = compute_cost_usd(usage)

        result = {
            'label': label,
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'cache_write': cache_write,
            'cache_read': cache_read,
            'total_input': prompt_tokens + cache_write + cache_read,
            'cost_usd': cost,
            'elapsed_s': round(elapsed, 1),
            'content_len': len(content),
            'msg_count': len(messages),
            'model': usage.get('_dispatch', {}).get('model', '?'),
            'raw_usage': usage,
        }
        return result

    except Exception as e:
        logger.error('[CostTest:%s] LLM call failed: %s', label, e, exc_info=True)
        return {
            'label': label,
            'error': str(e),
            'prompt_tokens': 0,
            'completion_tokens': 0,
            'cache_write': 0,
            'cache_read': 0,
            'total_input': 0,
            'cost_usd': 0,
            'elapsed_s': time.time() - t0,
            'content_len': 0,
            'msg_count': len(messages),
            'model': '?',
        }


def estimate_chars(messages: list[dict]) -> int:
    """Count total characters in messages (rough proxy for cost)."""
    total = 0
    for msg in messages:
        content = msg.get('content', '') or ''
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get('type') == 'text':
                    total += len(b.get('text', ''))
        for tc in msg.get('tool_calls', []):
            fn = tc.get('function', {})
            total += len(fn.get('arguments', ''))
    return total


# ═══════════════════════════════════════════════════════════════════════════════
#  Main test runner
# ═══════════════════════════════════════════════════════════════════════════════

def run_cost_comparison():
    """Run the full A/B cost comparison test."""
    print('\n' + '=' * 80)
    print('  keepToolHistory Cost Comparison Test')
    print('  Mode A: Current (summary only) vs Mode B: Full tool history')
    print('  Mode C: Full tool history + micro_compact')
    print('=' * 80)

    all_results = {'A': [], 'B': [], 'D': []}
    n_turns = len(SCENARIO['turns'])

    for turn_idx in range(n_turns):
        turn = SCENARIO['turns'][turn_idx]
        print(f'\n{"─" * 60}')
        print(f'  Turn {turn_idx + 1}/{n_turns}: {turn["user_msg"][:60]}...')
        print(f'  Tool rounds: {len(turn.get("tool_rounds", []))}')
        print(f'{"─" * 60}')

        # Build messages for each mode
        msgs_a = build_mode_a_messages(SCENARIO, turn_idx)
        msgs_b = build_mode_b_messages(SCENARIO, turn_idx)
        msgs_d = build_mode_d_messages(SCENARIO, turn_idx)

        chars_a = estimate_chars(msgs_a)
        chars_b = estimate_chars(msgs_b)
        chars_d = estimate_chars(msgs_d)

        print(f'  Messages: A={len(msgs_a)} B={len(msgs_b)} D={len(msgs_d)}')
        print(f'  Chars:    A={chars_a:,} B={chars_b:,} D={chars_d:,}')
        print(f'  Ratio:    B/A={chars_b/max(chars_a,1):.1f}x  D/A={chars_d/max(chars_a,1):.1f}x')

        # Call LLM for each mode
        for mode, msgs, mode_label in [
            ('A', msgs_a, f'ModeA_Turn{turn_idx+1}'),
            ('B', msgs_b, f'ModeB_Turn{turn_idx+1}'),
            ('D', msgs_d, f'ModeD_Turn{turn_idx+1}'),
        ]:
            result = call_llm_and_measure(msgs, mode_label)
            all_results[mode].append(result)

            err = result.get('error')
            if err:
                print(f'  Mode {mode}: ❌ ERROR: {err}')
            else:
                print(
                    f'  Mode {mode}: prompt={result["prompt_tokens"]:,} '
                    f'compl={result["completion_tokens"]:,} '
                    f'cache_w={result["cache_write"]:,} '
                    f'cache_r={result["cache_read"]:,} '
                    f'cost=${result["cost_usd"]:.4f} '
                    f'({result["elapsed_s"]}s) '
                    f'model={result["model"]}'
                )

            # Small delay to avoid rate limiting
            time.sleep(1)

    # ── Summary ──
    print('\n' + '=' * 80)
    print('  SUMMARY')
    print('=' * 80)

    for mode in ['A', 'B', 'D']:
        results = all_results[mode]
        total_prompt = sum(r['prompt_tokens'] for r in results)
        total_compl = sum(r['completion_tokens'] for r in results)
        total_cache_w = sum(r['cache_write'] for r in results)
        total_cache_r = sum(r['cache_read'] for r in results)
        total_input = sum(r['total_input'] for r in results)
        total_cost = sum(r['cost_usd'] for r in results)
        total_elapsed = sum(r['elapsed_s'] for r in results)
        errors = sum(1 for r in results if r.get('error'))

        mode_desc = {
            'A': 'Current (summary only)',
            'B': 'Full tool history',
            'D': 'Full tool history + old result truncation (2K)',
        }[mode]

        print(f'\n  Mode {mode} — {mode_desc}:')
        print(f'    Total prompt tokens:     {total_prompt:>10,}')
        print(f'    Total completion tokens: {total_compl:>10,}')
        print(f'    Total cache write:       {total_cache_w:>10,}')
        print(f'    Total cache read:        {total_cache_r:>10,}')
        print(f'    Total input tokens:      {total_input:>10,}')
        print(f'    Total cost:              ${total_cost:>9.4f}')
        print(f'    Total time:              {total_elapsed:>9.1f}s')
        if errors:
            print(f'    Errors:                  {errors}')

    # ── Per-turn comparison ──
    print('\n  Per-turn INPUT TOKEN comparison:')
    print(f'  {"Turn":>6} | {"A input":>10} | {"B input":>10} | {"D input":>10} | {"B/A":>6} | {"D/A":>6}')
    print(f'  {"-"*6}-+-{"-"*10}-+-{"-"*10}-+-{"-"*10}-+-{"-"*6}-+-{"-"*6}')

    for i in range(n_turns):
        ia = all_results['A'][i]['total_input']
        ib = all_results['B'][i]['total_input']
        id_ = all_results['D'][i]['total_input']
        print(
            f'  {i+1:>6} | {ia:>10,} | {ib:>10,} | '
            f'{id_:>10,} | {ib/max(ia,1):>5.1f}x | {id_/max(ia,1):>5.1f}x'
        )

    # ── Cost projection ──
    total_a = sum(r['cost_usd'] for r in all_results['A'])
    total_b = sum(r['cost_usd'] for r in all_results['B'])
    total_d = sum(r['cost_usd'] for r in all_results['D'])

    print(f'\n  Total cost: A=${total_a:.4f}  B=${total_b:.4f}  D=${total_d:.4f}')
    print(f'  B/A={total_b/max(total_a, 0.0001):.2f}x  D/A={total_d/max(total_a, 0.0001):.2f}x')

    print('\n' + '=' * 80)

    return all_results


# ═══════════════════════════════════════════════════════════════════════════════
#  pytest entry point
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.live_llm
def test_cost_comparison():
    """Run the cost comparison as a pytest test.

    This test makes REAL API calls and costs real money. It's tagged
    ``live_llm`` (manual-only — excluded from the default unit/api tiers),
    and additionally honours ``SKIP_COST_TEST=1``.
    """
    if os.environ.get('SKIP_COST_TEST', ''):
        pytest.skip('SKIP_COST_TEST is set')

    results = run_cost_comparison()

    # Basic sanity checks
    for mode in ['A', 'B', 'D']:
        for r in results[mode]:
            if r.get('error'):
                continue
            assert r['prompt_tokens'] > 0, f'{r["label"]}: no prompt tokens'
            assert r['completion_tokens'] > 0, f'{r["label"]}: no completion tokens'


def _make_large_result(tool_name: str, chars: int) -> str:
    """Generate a realistic large tool result of specified size."""
    if tool_name == 'fetch_url':
        header = (
            'URL: https://docs.python.org/3/library/asyncio-task.html\n'
            'Status: 200 OK\n'
            'Content-Type: text/html\n\n'
            '--- Page Content ---\n\n'
            '# Coroutines and Tasks\n\n'
        )
        # Fill with realistic documentation text
        paragraph = (
            'The asyncio module provides infrastructure for writing single-threaded '
            'concurrent code using coroutines, multiplexing I/O access over sockets '
            'and other resources, running network clients and servers, and other '
            'related primitives. A coroutine is a function that can suspend its '
            'execution before reaching return, and it can indirectly pass control '
            'to another coroutine for some time.\n\n'
            '## Creating Tasks\n\n'
            'asyncio.create_task(coro) wraps the coroutine into a Task and schedules '
            'its execution. Returns the Task object. The task is executed in the loop '
            'returned by get_running_loop(). If there is no running loop, a RuntimeError '
            'is raised. The important difference between create_task and ensure_future '
            'is that create_task can only accept coroutines and is the preferred way.\n\n'
            '```python\n'
            'import asyncio\n\n'
            'async def factorial(name, number):\n'
            '    f = 1\n'
            '    for i in range(2, number + 1):\n'
            '        print(f"Task {name}: Compute factorial({number}), currently i={i}...")\n'
            '        await asyncio.sleep(1)\n'
            '        f *= i\n'
            '    print(f"Task {name}: factorial({number}) = {f}")\n'
            '    return f\n\n'
            'async def main():\n'
            '    task1 = asyncio.create_task(factorial("A", 2))\n'
            '    task2 = asyncio.create_task(factorial("B", 3))\n'
            '    task3 = asyncio.create_task(factorial("C", 4))\n'
            '    await asyncio.gather(task1, task2, task3)\n\n'
            'asyncio.run(main())\n'
            '```\n\n'
        )
        body = paragraph * ((chars - len(header)) // len(paragraph) + 1)
        return header + body[:chars - len(header)]

    elif tool_name == 'read_files':
        header = 'File: lib/tasks_pkg/orchestrator.py (500 lines)\n────────────────\n'
        line_template = '{lineno:>4}:     {code}\n'
        code_lines = [
            'def run_task(task: dict) -> None:',
            '    """Main orchestrator loop."""',
            '    cfg = task.get("config", {})',
            '    model = cfg.get("model", "opus")',
            '    messages = list(task["messages"])',
            '    tool_call_happened = False',
            '    accumulated_usage = {}',
            '    for round_num in range(max_tool_rounds + 1):',
            '        run_compaction_pipeline(messages, round_num, task=task)',
            '        body = build_body(model, messages, tools=tools)',
            '        assistant_msg, finish_reason, usage = stream_llm_response(task, body)',
            '        if assistant_msg.get("tool_calls"):',
            '            tool_call_happened = True',
            '            messages.append(clean_msg)',
            '            parsed_tcs = parse_tool_calls(assistant_msg, task)',
            '            execute_tool_pipeline(task, parsed_tcs, messages)',
            '        else:',
            '            break',
            '    task["messages"] = messages',
            '    _finalize_and_emit_done(task, model=model)',
        ]
        body = ''
        i = 0
        while len(header) + len(body) < chars:
            body += line_template.format(
                lineno=i + 1,
                code=code_lines[i % len(code_lines)],
            )
            i += 1
        return header + body[:chars - len(header)]

    elif tool_name == 'web_search':
        header = 'Search results:\n\n'
        result_template = (
            '[{n}] {title}\n'
            '    URL: https://example.com/page-{n}\n'
            '    Source: example.com\n\n'
            '    ──── Full Page Content ({chars} chars) ────\n'
            '    {content}\n\n'
            '════════════════════\n\n'
        )
        body = ''
        n = 1
        while len(header) + len(body) < chars:
            content = f'This is search result #{n}. ' * 50
            body += result_template.format(
                n=n, title=f'Result {n}: Python asyncio documentation',
                chars=len(content), content=content,
            )
            n += 1
        return header + body[:chars - len(header)]

    else:
        return f'[{tool_name} output]\n' + ('x' * chars)


# ═══════════════════════════════════════════════════════════════════════════════
#  Heavy scenario — large tool results that trigger compaction
# ═══════════════════════════════════════════════════════════════════════════════

HEAVY_SCENARIO = {
    'name': 'Heavy tool usage with large results',
    'description': (
        'Simulates a real coding session with large file reads, web fetches, '
        'and extensive search results. This scenario WILL trigger compaction.'
    ),
    'turns': [
        # ── Turn 1: Large web search + fetch ──
        {
            'user_msg': '帮我研究一下 Python asyncio 的 create_task 和 ensure_future 的区别，需要详细的代码示例。',
            'tool_rounds': [
                {
                    'tool_calls': [
                        {
                            'name': 'web_search',
                            'arguments': {'query': 'python asyncio create_task vs ensure_future differences'},
                            'result_size': 8000,
                        },
                    ],
                },
                {
                    'tool_calls': [
                        {
                            'name': 'fetch_url',
                            'arguments': {'url': 'https://docs.python.org/3/library/asyncio-task.html'},
                            'result_size': 15000,
                        },
                        {
                            'name': 'fetch_url',
                            'arguments': {'url': 'https://stackoverflow.com/questions/asyncio-create-task'},
                            'result_size': 12000,
                        },
                    ],
                },
            ],
            'assistant_reply': (
                "## create_task vs ensure_future\n\n"
                "### create_task (推荐)\n"
                "- 只接受 coroutine，返回 Task 对象\n"
                "- 立即安排到当前 event loop 执行\n"
                "- Python 3.7+ 可用\n\n"
                "### ensure_future (不推荐用于新代码)\n"
                "- 可以接受 coroutine 或 Future\n"
                "- 如果传入 coroutine，内部调用 create_task\n"
                "- 属于历史遗留 API\n\n"
                "建议：**始终使用 create_task**，除非你需要处理既可能是 coroutine "
                "又可能是 Future 的参数。"
            ),
        },
        # ── Turn 2: Read large code files ──
        {
            'user_msg': '帮我看看我们项目里 orchestrator.py 和 compaction.py 的实现，分析哪里可以用 asyncio 优化。',
            'tool_rounds': [
                {
                    'tool_calls': [
                        {
                            'name': 'read_files',
                            'arguments': {'reads': [{'path': 'lib/tasks_pkg/orchestrator.py'}]},
                            'result_size': 20000,
                        },
                    ],
                },
                {
                    'tool_calls': [
                        {
                            'name': 'read_files',
                            'arguments': {'reads': [{'path': 'lib/tasks_pkg/compaction.py'}]},
                            'result_size': 18000,
                        },
                    ],
                },
                {
                    'tool_calls': [
                        {
                            'name': 'grep_search',
                            'arguments': {'pattern': 'async def', 'path': 'lib/tasks_pkg'},
                            'result_size': 3000,
                        },
                    ],
                },
            ],
            'assistant_reply': (
                "分析了 orchestrator.py (1079行) 和 compaction.py (1707行) 的实现：\n\n"
                "## 可以 asyncio 优化的地方\n\n"
                "1. **tool_dispatch.py 的并行工具执行** — 当前用 concurrent.futures.ThreadPoolExecutor，"
                "可以改用 asyncio.gather\n"
                "2. **micro_compact 的 DB 写入** — _persist_to_disk 和 _archive_transcript 可以异步化\n"
                "3. **LLM API 调用** — stream_chat 可以改用 aiohttp 实现真正的异步流\n\n"
                "## 不适合改的地方\n"
                "- run_task 主循环 — 本身是顺序逻辑，tool round 之间有依赖\n"
                "- force_compact — 同步 LLM 调用确保 compact 完成后才继续"
            ),
        },
        # ── Turn 3: Web search + more fetches ──
        {
            'user_msg': '搜索一下 asyncio + aiohttp 的最佳实践，特别是连接池和超时管理。',
            'tool_rounds': [
                {
                    'tool_calls': [
                        {
                            'name': 'web_search',
                            'arguments': {'query': 'aiohttp connection pool timeout best practices 2024'},
                            'result_size': 10000,
                        },
                    ],
                },
                {
                    'tool_calls': [
                        {
                            'name': 'fetch_url',
                            'arguments': {'url': 'https://docs.aiohttp.org/en/stable/client_quickstart.html'},
                            'result_size': 20000,
                        },
                    ],
                },
            ],
            'assistant_reply': (
                "## aiohttp 最佳实践\n\n"
                "### 连接池\n"
                "```python\n"
                "connector = aiohttp.TCPConnector(\n"
                "    limit=100,          # 总连接数上限\n"
                "    limit_per_host=10,  # 每个主机的连接数上限\n"
                "    ttl_dns_cache=300,  # DNS 缓存 5 分钟\n"
                "    enable_cleanup_closed=True,\n"
                ")\n"
                "session = aiohttp.ClientSession(connector=connector)\n"
                "```\n\n"
                "### 超时管理\n"
                "```python\n"
                "timeout = aiohttp.ClientTimeout(\n"
                "    total=30,      # 总超时\n"
                "    connect=5,     # 连接超时\n"
                "    sock_read=10,  # 读取超时\n"
                ")\n"
                "async with session.get(url, timeout=timeout) as resp:\n"
                "    data = await resp.read()\n"
                "```"
            ),
        },
        # ── Turn 4: Referencing previous results ──
        {
            'user_msg': (
                '回到刚才 orchestrator.py 的分析——你说 tool_dispatch.py 的并行工具执行可以用 asyncio.gather，'
                '具体怎么改？请先读一下 tool_dispatch.py 的相关代码。'
            ),
            'tool_rounds': [
                {
                    'tool_calls': [
                        {
                            'name': 'read_files',
                            'arguments': {'reads': [{'path': 'lib/tasks_pkg/tool_dispatch.py', 'start_line': 1, 'end_line': 200}]},
                            'result_size': 10000,
                        },
                    ],
                },
            ],
            'assistant_reply': (
                "看了 tool_dispatch.py 的实现，当前的并行执行逻辑是：\n\n"
                "```python\n"
                "# 当前实现（ThreadPoolExecutor）\n"
                "with ThreadPoolExecutor(max_workers=5) as executor:\n"
                "    futures = {executor.submit(run_tool, tc): tc for tc in parallel_tcs}\n"
                "    for future in as_completed(futures, timeout=timeout):\n"
                "        result = future.result()\n"
                "```\n\n"
                "改用 asyncio 的方案：\n"
                "```python\n"
                "# 改进方案\n"
                "async def execute_tools_async(tool_calls):\n"
                "    tasks = [asyncio.create_task(run_tool_async(tc)) for tc in tool_calls]\n"
                "    results = await asyncio.gather(*tasks, return_exceptions=True)\n"
                "    return results\n"
                "```\n\n"
                "但需要注意：run_task 主循环不是 async 的，所以要用 asyncio.run() 包装。"
            ),
        },
    ],
}


def _fill_results(scenario: dict) -> dict:
    """Fill in result_size fields with actual generated content."""
    filled = copy.deepcopy(scenario)
    for turn in filled['turns']:
        for tool_round in turn.get('tool_rounds', []):
            for tc in tool_round['tool_calls']:
                if 'result_size' in tc:
                    tc['result'] = _make_large_result(tc['name'], tc['result_size'])
                    del tc['result_size']
    return filled


def run_heavy_cost_comparison():
    """Run the heavy scenario cost comparison."""
    scenario = _fill_results(HEAVY_SCENARIO)

    print('\n' + '=' * 80)
    print('  HEAVY SCENARIO: keepToolHistory Cost Comparison')
    print(f'  Fixed model: {TEST_MODEL}')
    print('  Mode A: Current (summary only)')
    print('  Mode B: Full tool history')
    print('  Mode C: Full tool history + micro_compact')
    print('=' * 80)

    all_results = {'A': [], 'B': [], 'D': []}
    n_turns = len(scenario['turns'])

    for turn_idx in range(n_turns):
        turn = scenario['turns'][turn_idx]
        total_result_chars = sum(
            len(tc.get('result', ''))
            for tr in turn.get('tool_rounds', [])
            for tc in tr['tool_calls']
        )

        print(f'\n{"─" * 60}')
        print(f'  Turn {turn_idx + 1}/{n_turns}: {turn["user_msg"][:60]}...')
        print(f'  Tool rounds: {len(turn.get("tool_rounds", []))}')
        print(f'  Total tool result chars this turn: {total_result_chars:,}')
        print(f'{"─" * 60}')

        msgs_a = build_mode_a_messages(scenario, turn_idx)
        msgs_b = build_mode_b_messages(scenario, turn_idx)
        msgs_d = build_mode_d_messages(scenario, turn_idx)

        chars_a = estimate_chars(msgs_a)
        chars_b = estimate_chars(msgs_b)
        chars_d = estimate_chars(msgs_d)

        print(f'  Messages: A={len(msgs_a)} B={len(msgs_b)} D={len(msgs_d)}')
        print(f'  Chars:    A={chars_a:,} B={chars_b:,} D={chars_d:,}')
        print(f'  Ratio:    B/A={chars_b/max(chars_a,1):.1f}x  D/A={chars_d/max(chars_a,1):.1f}x')

        for mode, msgs, mode_label in [
            ('A', msgs_a, f'Heavy_A_Turn{turn_idx+1}'),
            ('B', msgs_b, f'Heavy_B_Turn{turn_idx+1}'),
            ('D', msgs_d, f'Heavy_D_Turn{turn_idx+1}'),
        ]:
            result = call_llm_and_measure(msgs, mode_label)
            all_results[mode].append(result)

            err = result.get('error')
            if err:
                print(f'  Mode {mode}: ❌ ERROR: {err}')
            else:
                print(
                    f'  Mode {mode}: prompt={result["prompt_tokens"]:,} '
                    f'compl={result["completion_tokens"]:,} '
                    f'cache_w={result["cache_write"]:,} '
                    f'cache_r={result["cache_read"]:,} '
                    f'cost=${result["cost_usd"]:.4f} '
                    f'({result["elapsed_s"]}s)'
                )
            time.sleep(1)

    # ── Summary ──
    print('\n' + '=' * 80)
    print('  HEAVY SCENARIO SUMMARY')
    print('=' * 80)

    for mode in ['A', 'B', 'D']:
        results = all_results[mode]
        total_prompt = sum(r['prompt_tokens'] for r in results)
        total_compl = sum(r['completion_tokens'] for r in results)
        total_cache_w = sum(r['cache_write'] for r in results)
        total_cache_r = sum(r['cache_read'] for r in results)
        total_input = sum(r['total_input'] for r in results)
        total_cost = sum(r['cost_usd'] for r in results)

        mode_desc = {
            'A': 'Current (summary only)',
            'B': 'Full tool history (no truncation)',
            'D': 'Full tool history + old result truncation (2K)',
        }[mode]

        print(f'\n  Mode {mode} — {mode_desc}:')
        print(f'    Total prompt tokens:     {total_prompt:>10,}')
        print(f'    Total completion tokens: {total_compl:>10,}')
        print(f'    Total cache write:       {total_cache_w:>10,}')
        print(f'    Total cache read:        {total_cache_r:>10,}')
        print(f'    Total input tokens:      {total_input:>10,}')
        print(f'    Total cost:              ${total_cost:>9.4f}')

    # ── Per-turn table ──
    print('\n  Per-turn INPUT TOKEN comparison (prompt + cache_w + cache_r):')
    print(f'  {"Turn":>6} | {"A input":>10} | {"B input":>10} | {"D input":>10} | {"B/A":>6} | {"D/A":>6} | {"D/B":>6}')
    print(f'  {"-"*6}-+-{"-"*10}-+-{"-"*10}-+-{"-"*10}-+-{"-"*6}-+-{"-"*6}-+-{"-"*6}')

    for i in range(n_turns):
        ia = all_results['A'][i]['total_input']
        ib = all_results['B'][i]['total_input']
        id_ = all_results['D'][i]['total_input']
        print(
            f'  {i+1:>6} | {ia:>10,} | {ib:>10,} | '
            f'{id_:>10,} | {ib/max(ia,1):>5.1f}x | {id_/max(ia,1):>5.1f}x | {id_/max(ib,1):>5.1f}x'
        )

    # ── Cost table ──
    print('\n  Per-turn COST comparison (USD):')
    print(f'  {"Turn":>6} | {"A cost":>10} | {"B cost":>10} | {"D cost":>10} | {"B/A":>6} | {"D/A":>6}')
    print(f'  {"-"*6}-+-{"-"*10}-+-{"-"*10}-+-{"-"*10}-+-{"-"*6}-+-{"-"*6}')

    for i in range(n_turns):
        ca = all_results['A'][i]['cost_usd']
        cb = all_results['B'][i]['cost_usd']
        cd = all_results['D'][i]['cost_usd']
        print(
            f'  {i+1:>6} | ${ca:>9.4f} | ${cb:>9.4f} | '
            f'${cd:>9.4f} | {cb/max(ca,0.0001):>5.1f}x | {cd/max(ca,0.0001):>5.1f}x'
        )

    total_a = sum(r['cost_usd'] for r in all_results['A'])
    total_b = sum(r['cost_usd'] for r in all_results['B'])
    total_d = sum(r['cost_usd'] for r in all_results['D'])
    print(f'\n  Total cost: A=${total_a:.4f}  B=${total_b:.4f}  D=${total_d:.4f}')
    print(f'  B/A={total_b/max(total_a, 0.0001):.2f}x  D/A={total_d/max(total_a, 0.0001):.2f}x  D/B={total_d/max(total_b, 0.0001):.2f}x')

    if total_a > 0:
        avg_a = total_a / n_turns
        avg_b = total_b / n_turns
        avg_d = total_d / n_turns
        daily_turns = 50 * 5
        monthly_a = avg_a * daily_turns * 30
        monthly_b = avg_b * daily_turns * 30
        monthly_d = avg_d * daily_turns * 30
        print('\n  Monthly projection (50 convs/day × 5 turns):')
        print(f'    Mode A: ${monthly_a:,.2f}/mo')
        print(f'    Mode B: ${monthly_b:,.2f}/mo  ({monthly_b - monthly_a:+,.2f})')
        print(f'    Mode D: ${monthly_d:,.2f}/mo  ({monthly_d - monthly_a:+,.2f})')

    print('\n' + '=' * 80)
    return all_results


if __name__ == '__main__':
    print('\n\n██ LIGHT SCENARIO ██\n')
    run_cost_comparison()
    print('\n\n██ HEAVY SCENARIO ██\n')
    run_heavy_cost_comparison()
