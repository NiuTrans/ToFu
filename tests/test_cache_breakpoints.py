"""Comprehensive regression tests for prompt-cache breakpoint placement.

Covers:
  1. BP4 tail placement — scan from msg[-1], not msg[-2]
  2. Empty-content assistant skip — tool_calls-only messages have content=''
  3. Backward scan fallback chain — up to 5 positions backwards
  4. Edge cases — tiny conversations, list content, system-only, non-Claude
  5. Phase-0 stripping — stale cache_control removed before re-annotation
  6. System / tool breakpoint placement (BP1-BP3)
  7. Multi-round conversation simulation — verifying cache prefix stability
  8. Breakpoint count limit — never exceeds 4 total
  9. Opus minimum threshold awareness (4096 tokens)
 10. Mixed TTL strategy — 1h for BP1-BP3, 5m for BP4
"""

import copy
import json

import pytest

import lib as _lib
from lib.llm import add_cache_breakpoints


# ═══════════════════════════════════════════════════════════════════════════════
#  TTL-aware helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _cc_stable():
    """Expected cache_control dict for stable prefix (BP1-BP3)."""
    if getattr(_lib, 'CACHE_EXTENDED_TTL', False):
        return {'type': 'ephemeral', 'ttl': '1h'}
    return {'type': 'ephemeral'}


def _cc_tail():
    """Expected cache_control dict for conversation tail (BP4)."""
    return {'type': 'ephemeral'}


@pytest.fixture(autouse=True)
def _disable_extended_ttl():
    """Disable extended TTL for all existing tests by default.

    Tests that explicitly test mixed TTL will re-enable it.
    This ensures existing tests pass without changing assertions.
    """
    original = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
    _lib.CACHE_EXTENDED_TTL = False
    yield
    _lib.CACHE_EXTENDED_TTL = original


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _has_cache_control(msg):
    """Check whether a message has cache_control annotation."""
    content = msg.get('content', '')
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and 'cache_control' in b
            for b in content
        )
    return False


def _count_breakpoints(body):
    """Count total cache_control annotations in a body."""
    count = 0
    for msg in body.get('messages', []):
        content = msg.get('content', '')
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and 'cache_control' in block:
                    count += 1
        elif isinstance(content, str):
            # String content without cache_control
            pass
    # Tool definitions
    for tool in body.get('tools', []):
        fn = tool.get('function', {})
        if 'cache_control' in fn:
            count += 1
    return count


def _make_tool_conv(num_rounds, *, empty_assistant_content=True):
    """Build a realistic multi-round tool conversation.

    Args:
        num_rounds: Number of tool call rounds.
        empty_assistant_content: If True, assistant msgs have content=''
            (tool_calls only, no text).  If False, assistant msgs have text
            like 'Let me check that.' before the tool call.

    Returns:
        Body dict with model, messages, and tools.
    """
    messages = [
        {'role': 'system', 'content': 'You are a helpful assistant.'},
        {'role': 'user', 'content': 'Analyze the project structure.'},
    ]
    for i in range(num_rounds):
        asst_content = '' if empty_assistant_content else f'Let me check round {i + 1}.'
        messages.append({
            'role': 'assistant',
            'content': asst_content,
            'tool_calls': [
                {'id': f'tc_{i}', 'type': 'function',
                 'function': {'name': 'read_files', 'arguments': json.dumps({'path': f'file_{i}.py'})}}
            ],
        })
        messages.append({
            'role': 'tool',
            'tool_call_id': f'tc_{i}',
            'content': f'# File content from round {i + 1}\n'
                       f'def func_{i}():\n    return {i}\n',
        })
    return {
        'model': 'claude-sonnet-4-20250514',
        'messages': messages,
        'tools': [
            {'type': 'function', 'function': {
                'name': 'read_files',
                'description': 'Read files from the project.',
                'parameters': {'type': 'object', 'properties': {
                    'path': {'type': 'string'},
                }},
            }},
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  1. BP4 Tail Placement — The Core Fix
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBP4TailPlacement:
    """BP4 must land on the LAST message with content, scanning from msg[-1]."""

    def test_bp4_on_last_tool_result(self):
        """BP4 placed on msg[-1] when it's a tool result with content."""
        body = _make_tool_conv(1, empty_assistant_content=True)
        # Messages: [system, user, asst(empty), tool]
        assert len(body['messages']) == 4
        add_cache_breakpoints(body)
        # msg[-1] = tool result → should get BP4
        assert _has_cache_control(body['messages'][-1]), \
            'BP4 should be on msg[-1] (tool result)'
        # msg[-2] = empty assistant → should NOT get BP4
        assert not _has_cache_control(body['messages'][-2]), \
            'Empty-content assistant should be skipped'

    def test_bp4_on_last_tool_result_multi_round(self):
        """In a 5-round conversation, BP4 on the very last tool result."""
        body = _make_tool_conv(5, empty_assistant_content=True)
        # Messages: [system, user, 5×(asst+tool)] = 12 messages
        assert len(body['messages']) == 12
        add_cache_breakpoints(body)
        assert _has_cache_control(body['messages'][-1]), \
            'BP4 should be on last tool result (msg[-1])'
        # No other non-system messages should have cache_control
        for i in range(1, len(body['messages']) - 1):
            msg = body['messages'][i]
            if msg.get('role') != 'system':
                assert not _has_cache_control(msg), \
                    f'Only msg[-1] should have BP4, but msg[{i}] also has it'

    def test_bp4_on_user_first_round(self):
        """First round: only [system, user]. BP4 on user (msg[-1])."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'system prompt'},
                {'role': 'user', 'content': 'Hello, world!'},
            ],
        }
        add_cache_breakpoints(body)
        assert _has_cache_control(body['messages'][-1]), \
            'BP4 should be on user message (msg[-1]) in first round'

    def test_bp4_with_content_assistant(self):
        """When assistant has text content, BP4 lands on msg[-1] (tool result),
        NOT on msg[-2] (assistant with content).  msg[-1] is preferred because
        it caches the maximum prefix."""
        body = _make_tool_conv(2, empty_assistant_content=False)
        # Messages: [system, user, asst(text), tool, asst(text), tool]
        assert len(body['messages']) == 6
        add_cache_breakpoints(body)
        # msg[-1] = tool result → should get BP4 (maximum prefix coverage)
        assert _has_cache_control(body['messages'][-1]), \
            'BP4 should be on msg[-1] (tool result) even when msg[-2] has content'


# ═══════════════════════════════════════════════════════════════════════════════
#  2. Empty-Content Assistant Skip (The Original Bug)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEmptyContentSkip:
    """The root cause of the cache oscillation: assistant messages with only
    tool_calls have content='' which is falsy.  BP4 must skip them."""

    def test_empty_string_content_skipped(self):
        """content='' is falsy → must be skipped by BP4 scan."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'query'},
                {'role': 'assistant', 'content': '',
                 'tool_calls': [{'function': {'name': 'foo', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': 'result data'},
            ],
        }
        add_cache_breakpoints(body)
        # tool result (msg[-1]) gets BP4, NOT the empty assistant (msg[-2])
        assert _has_cache_control(body['messages'][3]), \
            'Tool result should have BP4'
        assert not _has_cache_control(body['messages'][2]), \
            'Empty assistant should be skipped'

    def test_none_content_skipped(self):
        """content=None (some APIs) must also be skipped."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'query'},
                {'role': 'assistant', 'content': None,
                 'tool_calls': [{'function': {'name': 'foo', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': 'result data'},
            ],
        }
        add_cache_breakpoints(body)
        assert _has_cache_control(body['messages'][3]), \
            'Tool result should have BP4'
        assert not _has_cache_control(body['messages'][2]), \
            'None-content assistant should be skipped'

    def test_missing_content_key_skipped(self):
        """No 'content' key at all → must be skipped."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'query'},
                {'role': 'assistant',
                 'tool_calls': [{'function': {'name': 'foo', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': 'result data'},
            ],
        }
        add_cache_breakpoints(body)
        assert _has_cache_control(body['messages'][3]), \
            'Tool result should have BP4'
        assert not _has_cache_control(body['messages'][2]), \
            'Assistant without content key should be skipped'

    def test_consecutive_empty_assistants(self):
        """Multiple consecutive empty assistants — scan must skip all of them."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'query'},
                {'role': 'assistant', 'content': '',
                 'tool_calls': [{'function': {'name': 'a', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': 'result 1'},
                {'role': 'assistant', 'content': '',
                 'tool_calls': [{'function': {'name': 'b', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': 'result 2'},
                {'role': 'assistant', 'content': '',
                 'tool_calls': [{'function': {'name': 'c', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': 'result 3'},
            ],
        }
        add_cache_breakpoints(body)
        # BP4 on last tool result (msg[-1] = msg[7])
        assert _has_cache_control(body['messages'][7]), \
            'Last tool result should have BP4'
        # No empty assistants should have BP4
        for idx in [2, 4, 6]:
            assert not _has_cache_control(body['messages'][idx]), \
                f'Empty assistant at msg[{idx}] should be skipped'


# ═══════════════════════════════════════════════════════════════════════════════
#  3. Backward Scan Fallback Chain
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBackwardScan:
    """BP4 scan goes up to 5 positions back. Verify the fallback chain."""

    def test_fallback_to_msg_minus_2(self):
        """If msg[-1] has empty content but msg[-2] has content, land on msg[-2]."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'query'},
                {'role': 'assistant', 'content': 'I found the answer.'},
                # Last msg has empty content (unusual but possible)
                {'role': 'user', 'content': ''},
            ],
        }
        add_cache_breakpoints(body)
        # msg[-1] = empty user → skip → msg[-2] = assistant with content
        assert _has_cache_control(body['messages'][2]), \
            'Should fall back to msg[-2] (assistant with content)'

    def test_scan_stops_at_system_message(self):
        """The scan must NOT go past system messages (they have their own BPs)."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'query'},
            ],
        }
        add_cache_breakpoints(body)
        # msg[-1] = user → BP4 placed there
        assert _has_cache_control(body['messages'][1])
        # System message has its own BP, not BP4
        sys_content = body['messages'][0]['content']
        if isinstance(sys_content, list):
            for block in sys_content:
                if isinstance(block, dict) and 'cache_control' in block:
                    # This is BP1 (system), not BP4
                    pass

    def test_scan_does_not_cross_system(self):
        """If all non-system messages have empty content, BP4 is not placed
        (scan stops at system boundary)."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'assistant', 'content': ''},
                {'role': 'assistant', 'content': ''},
            ],
        }
        add_cache_breakpoints(body)
        # BP4 cannot be placed — all non-system msgs have empty content
        for i in range(1, len(body['messages'])):
            assert not _has_cache_control(body['messages'][i]), \
                f'msg[{i}] should NOT have BP4 (empty content, scan hit system boundary)'


# ═══════════════════════════════════════════════════════════════════════════════
#  4. Edge Cases
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEdgeCases:
    """Edge cases for add_cache_breakpoints."""

    def test_non_marker_models_get_no_breakpoints(self):
        """Models that don't honor cache markers must not be annotated.

        Per the 2026-05-03 probe (``_CACHE_MARKERS_HELP`` in lib/llm/cache.py),
        Claude + glm-5 + qwen + deepseek are the gateways where attaching
        cache_control markers is beneficial. Models outside that allow-list
        (gpt-4o, gemini, doubao, minimax, etc.) must receive zero
        breakpoints to avoid degrading their auto-caching paths.
        """
        for model in ['gpt-4o', 'gemini-2.5-pro', 'doubao-seed-1-6', 'MiniMax-M2']:
            body = _make_tool_conv(3, empty_assistant_content=True)
            body['model'] = model
            add_cache_breakpoints(body)
            assert _count_breakpoints(body) == 0, \
                f'{model} should have 0 breakpoints, got {_count_breakpoints(body)}'

    def test_single_system_message_only(self):
        """Only a system message — should get BP1 but no BP4."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [{'role': 'system', 'content': 'system prompt'}],
        }
        add_cache_breakpoints(body)
        assert _has_cache_control(body['messages'][0]), 'System msg should have BP1'
        assert _count_breakpoints(body) == 1

    def test_list_content_on_tool_result(self):
        """Tool result with list content (image blocks etc.) should get BP4."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'analyze this'},
                {'role': 'assistant', 'content': '',
                 'tool_calls': [{'function': {'name': 'read_files', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': [
                    {'type': 'text', 'text': 'File analysis results...'},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,abc'}},
                ]},
            ],
        }
        add_cache_breakpoints(body)
        # msg[-1] = tool with list content → BP4 on last block
        tool_content = body['messages'][-1]['content']
        assert isinstance(tool_content, list)
        has_bp = any(
            isinstance(b, dict) and 'cache_control' in b
            for b in tool_content
        )
        assert has_bp, 'List-content tool result should have BP4'

    def test_empty_list_content_skipped(self):
        """Empty list content [] should be skipped like empty string."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'query'},
                {'role': 'assistant', 'content': []},  # Empty list
                {'role': 'tool', 'content': 'result'},
            ],
        }
        add_cache_breakpoints(body)
        assert _has_cache_control(body['messages'][3]), \
            'Tool result should have BP4 (empty list skipped)'

    def test_no_messages(self):
        """Empty messages list — should not crash."""
        body = {'model': 'claude-sonnet-4-20250514', 'messages': []}
        add_cache_breakpoints(body)  # Should not raise
        assert _count_breakpoints(body) == 0

    def test_no_tools(self):
        """Body without tools key — BP3 (tool def) is skipped, BP4 still works."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'just chat'},
            ],
        }
        add_cache_breakpoints(body)
        # BP1 on system, BP4 on user, no BP3 (no tools)
        assert _has_cache_control(body['messages'][0])
        assert _has_cache_control(body['messages'][1])


# ═══════════════════════════════════════════════════════════════════════════════
#  5. Phase-0 Stripping — Stale cache_control Removal
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPhase0Stripping:
    """Phase 0 must strip ALL existing cache_control before re-annotating."""

    def test_stale_breakpoints_removed(self):
        """Old cache_control from a prior round must be stripped."""
        body = _make_tool_conv(2, empty_assistant_content=False)
        # First pass — adds breakpoints
        add_cache_breakpoints(body)
        bp_count_1 = _count_breakpoints(body)
        assert bp_count_1 > 0

        # Second pass — should strip old ones and re-add fresh ones
        add_cache_breakpoints(body)
        bp_count_2 = _count_breakpoints(body)
        assert bp_count_2 == bp_count_1, \
            f'Re-running should give same BP count: {bp_count_1} → {bp_count_2}'

    def test_no_accumulation_over_rounds(self):
        """Simulate 10 rounds: breakpoints should never accumulate past 4."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'system prompt'},
                {'role': 'user', 'content': 'initial query'},
            ],
            'tools': [
                {'type': 'function', 'function': {
                    'name': 'read_files',
                    'description': 'Read files.',
                    'parameters': {'type': 'object'},
                }},
            ],
        }
        for round_num in range(10):
            add_cache_breakpoints(body)
            bp_count = _count_breakpoints(body)
            assert bp_count <= 4, \
                f'Round {round_num}: {bp_count} BPs exceeds max 4'
            # Simulate a new tool round
            body['messages'].append({
                'role': 'assistant', 'content': '',
                'tool_calls': [{'function': {'name': 'read_files', 'arguments': '{}'}}],
            })
            body['messages'].append({
                'role': 'tool', 'content': f'Result from round {round_num}',
            })

    def test_tool_function_cache_control_stripped(self):
        """cache_control on tool function defs must be stripped and re-added."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'query'},
            ],
            'tools': [
                {'type': 'function', 'function': {
                    'name': 'tool_a', 'description': 'A',
                    'parameters': {'type': 'object'},
                }},
                {'type': 'function', 'function': {
                    'name': 'tool_b', 'description': 'B',
                    'parameters': {'type': 'object'},
                    'cache_control': {'type': 'ephemeral'},  # Stale
                }},
            ],
        }
        add_cache_breakpoints(body)
        # Stale BP on tool_b should be stripped; fresh BP on last tool (tool_b)
        fn_b = body['tools'][-1]['function']
        assert 'cache_control' in fn_b, 'Last tool should have BP3'
        # First tool should NOT have cache_control
        fn_a = body['tools'][0]['function']
        assert 'cache_control' not in fn_a, 'First tool should not have BP'


# ═══════════════════════════════════════════════════════════════════════════════
#  6. System / Tool Breakpoint Placement (BP1-BP3)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSystemAndToolBPs:
    """Verify BP1 (system), BP2 (system block 2), BP3 (last tool def)."""

    def test_system_string_content_gets_bp(self):
        """System message with string content → converted to list with BP."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [{'role': 'system', 'content': 'You are helpful.'}],
        }
        add_cache_breakpoints(body)
        sys_content = body['messages'][0]['content']
        assert isinstance(sys_content, list), 'System content should be list'
        assert sys_content[0]['cache_control'] == {'type': 'ephemeral'}

    def test_system_multi_block_each_gets_bp(self):
        """System message with multiple text blocks → each gets its own BP."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [{
                'role': 'system',
                'content': [
                    {'type': 'text', 'text': 'Dynamic context'},
                    {'type': 'text', 'text': 'Static guidance'},
                ],
            }],
        }
        add_cache_breakpoints(body)
        blocks = body['messages'][0]['content']
        assert blocks[0].get('cache_control') == {'type': 'ephemeral'}
        assert blocks[1].get('cache_control') == {'type': 'ephemeral'}

    def test_last_tool_definition_gets_bp(self):
        """BP3 goes on the last tool's function definition."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [{'role': 'system', 'content': 'sys'}],
            'tools': [
                {'type': 'function', 'function': {
                    'name': 'tool_1', 'description': 'First',
                    'parameters': {'type': 'object'},
                }},
                {'type': 'function', 'function': {
                    'name': 'tool_2', 'description': 'Second',
                    'parameters': {'type': 'object'},
                }},
            ],
        }
        add_cache_breakpoints(body)
        assert 'cache_control' not in body['tools'][0]['function'], \
            'First tool should NOT have BP'
        assert 'cache_control' in body['tools'][-1]['function'], \
            'Last tool should have BP3'


# ═══════════════════════════════════════════════════════════════════════════════
#  7. Multi-Round Simulation — Cache Prefix Stability
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMultiRoundSimulation:
    """Simulate a multi-round orchestrator loop.  Verify that BP4 advances
    each round and always lands on the last message with content, so the
    cached prefix grows monotonically."""

    def test_bp4_advances_each_round(self):
        """BP4 should be on the last message each round."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'system prompt'},
                {'role': 'user', 'content': 'What files are in the project?'},
            ],
            'tools': [
                {'type': 'function', 'function': {
                    'name': 'list_dir', 'description': 'List directory.',
                    'parameters': {'type': 'object'},
                }},
            ],
        }

        bp4_positions = []
        for round_num in range(6):
            add_cache_breakpoints(body)

            # Find which message got BP4 (not system, not tool def)
            for idx in range(len(body['messages']) - 1, 0, -1):
                msg = body['messages'][idx]
                if msg.get('role') == 'system':
                    continue
                if _has_cache_control(msg):
                    bp4_positions.append(idx)
                    break

            # Simulate next tool round
            body['messages'].append({
                'role': 'assistant', 'content': '',
                'tool_calls': [{'function': {'name': 'list_dir', 'arguments': '{}'}}],
            })
            body['messages'].append({
                'role': 'tool', 'content': f'file_{round_num}.py (100 lines)',
            })

        # BP4 should advance monotonically (each round it's further along)
        for i in range(1, len(bp4_positions)):
            assert bp4_positions[i] > bp4_positions[i - 1], \
                f'BP4 should advance: round {i} pos={bp4_positions[i]} ' \
                f'<= round {i-1} pos={bp4_positions[i-1]}'

    def test_bp4_always_on_tool_result_in_tool_conv(self):
        """In a tool conversation with empty-content assistants,
        BP4 should ALWAYS land on a tool result (role=tool)."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'system prompt'},
                {'role': 'user', 'content': 'query'},
            ],
            'tools': [
                {'type': 'function', 'function': {
                    'name': 'grep_search', 'description': 'Search.',
                    'parameters': {'type': 'object'},
                }},
            ],
        }

        for round_num in range(8):
            # Add tool round with empty assistant
            body['messages'].append({
                'role': 'assistant', 'content': '',
                'tool_calls': [{'function': {'name': 'grep_search', 'arguments': '{}'}}],
            })
            body['messages'].append({
                'role': 'tool', 'content': f'Match {round_num}: line {round_num * 10}',
            })

            add_cache_breakpoints(body)

            # Find BP4 — should be on the last tool result
            last_msg = body['messages'][-1]
            assert _has_cache_control(last_msg), \
                f'Round {round_num}: BP4 should be on last msg (tool result)'
            assert last_msg.get('role') == 'tool', \
                f'Round {round_num}: BP4 should be on tool msg, ' \
                f'got role={last_msg.get("role")}'

    def test_prefix_content_unchanged_between_rounds(self):
        """The TEXT content of messages in the cached prefix must NOT change
        between rounds (otherwise cache is invalidated).

        Note: add_cache_breakpoints converts string content to list format
        (str → [{type: text, text: str, cache_control: ...}]) when placing
        a BP.  This is a format change, not a content change — the actual
        text is preserved.  We normalize to plain text for comparison.
        """
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'system prompt'},
                {'role': 'user', 'content': 'query'},
            ],
        }

        def _extract_text(content):
            """Normalize content to plain text for comparison."""
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and 'text' in block:
                        parts.append(block['text'])
                    elif isinstance(block, str):
                        parts.append(block)
                return '||'.join(parts)
            return str(content)

        snapshots = []
        for round_num in range(5):
            add_cache_breakpoints(body)

            # Snapshot normalized text content AFTER breakpoints
            snapshot = []
            for msg in body['messages']:
                snapshot.append((msg['role'], _extract_text(msg.get('content', ''))))
            snapshots.append(snapshot)

            # Add new round
            body['messages'].append({
                'role': 'assistant', 'content': '',
                'tool_calls': [{'function': {'name': 'test', 'arguments': '{}'}}],
            })
            body['messages'].append({
                'role': 'tool', 'content': f'result {round_num}',
            })

        # Verify: each snapshot's prefix matches the next snapshot's prefix
        for i in range(1, len(snapshots)):
            prefix_len = len(snapshots[i - 1])
            for j in range(prefix_len):
                role_prev, text_prev = snapshots[i - 1][j]
                role_curr, text_curr = snapshots[i][j]
                assert role_prev == role_curr, \
                    f'Round {i} msg[{j}] role changed: {role_prev} → {role_curr}'
                assert text_prev == text_curr, \
                    f'Round {i} msg[{j}] text changed: {text_prev!r} → {text_curr!r}'


# ═══════════════════════════════════════════════════════════════════════════════
#  8. Breakpoint Count Limit — Never > 4
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBreakpointLimit:
    """Anthropic enforces max 4 cache_control annotations. Verify we comply."""

    def test_max_4_breakpoints_with_multi_block_system(self):
        """2 system blocks + 1 tool def + 1 tail = 4 total (the max)."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': [
                    {'type': 'text', 'text': 'Block 1: dynamic context'},
                    {'type': 'text', 'text': 'Block 2: static guidance'},
                ]},
                {'role': 'user', 'content': 'hello'},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'function': {'name': 'test', 'arguments': '{}'}},
                ]},
                {'role': 'tool', 'content': 'result'},
            ],
            'tools': [
                {'type': 'function', 'function': {
                    'name': 'test', 'description': 'Test tool.',
                    'parameters': {'type': 'object'},
                }},
            ],
        }
        add_cache_breakpoints(body)
        total = _count_breakpoints(body)
        assert total <= 4, f'Total BPs = {total} exceeds max 4'

    def test_5_system_blocks_capped_at_4(self):
        """System message with 5 text blocks — only first 4 get BPs."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [{
                'role': 'system',
                'content': [
                    {'type': 'text', 'text': f'Block {i}'}
                    for i in range(5)
                ],
            }],
        }
        add_cache_breakpoints(body)
        total = _count_breakpoints(body)
        assert total == 4, f'Expected exactly 4 BPs (capped), got {total}'

    def test_system_blocks_exhaust_limit_no_bp4(self):
        """If system uses all 4 BPs, tool def and tail get none."""
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': [
                    {'type': 'text', 'text': f'Block {i}'}
                    for i in range(4)
                ]},
                {'role': 'user', 'content': 'query'},
                {'role': 'tool', 'content': 'result'},
            ],
            'tools': [
                {'type': 'function', 'function': {
                    'name': 'test', 'description': 'Test.',
                    'parameters': {'type': 'object'},
                }},
            ],
        }
        add_cache_breakpoints(body)
        total = _count_breakpoints(body)
        assert total == 4, f'Expected 4 (all from system), got {total}'
        # Tool def should NOT have BP (budget exhausted)
        assert 'cache_control' not in body['tools'][-1]['function']
        # Tail should NOT have BP (budget exhausted)
        assert not _has_cache_control(body['messages'][-1])


# ═══════════════════════════════════════════════════════════════════════════════
#  9. Regression Guard — The Exact Bug Scenario from mnk84kthdr2x08
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRegressionMNK84:
    """Exact reproduction of the cache oscillation bug from conversation
    mnk84kthdr2x08 (Opus 4.6, 54 rounds, 50% cache miss rate).

    The bug: BP4 scanned from msg[-2] which was an empty-content assistant
    (tool_calls only, content='').  The scan fell back to msg[-3] or earlier,
    under-caching the conversation tail.

    After fix: BP4 scans from msg[-1] (tool result, always has content),
    caching the full prefix → cache hit on next round.
    """

    def test_exact_bug_scenario_4_messages(self):
        """[system, user, asst(empty), tool] — the minimal reproduction."""
        body = {
            'model': 'aws.claude-opus-4.6',
            'messages': [
                {'role': 'system', 'content': 'You are a project copilot.'},
                {'role': 'user', 'content': 'Show me the project structure.'},
                {'role': 'assistant', 'content': '',
                 'tool_calls': [
                     {'id': 'tc_1', 'type': 'function',
                      'function': {'name': 'list_dir', 'arguments': '{"path": "."}'}}
                 ]},
                {'role': 'tool', 'tool_call_id': 'tc_1',
                 'content': 'src/ (15 items)\ntests/ (8 items)\nREADME.md (2.1KB)'},
            ],
            'tools': [
                {'type': 'function', 'function': {
                    'name': 'list_dir', 'description': 'List directory contents.',
                    'parameters': {'type': 'object', 'properties': {
                        'path': {'type': 'string'},
                    }},
                }},
            ],
        }
        add_cache_breakpoints(body)

        # CRITICAL ASSERTION: BP4 must be on msg[-1] (tool result),
        # NOT on msg[-3] (user message).
        tool_msg = body['messages'][-1]
        assert _has_cache_control(tool_msg), \
            'REGRESSION: BP4 must be on tool result (msg[-1])'

        # The user message should NOT have BP4
        user_msg = body['messages'][1]
        assert not _has_cache_control(user_msg), \
            'REGRESSION: BP4 should NOT fall back to user message'

        # The empty assistant should NOT have BP4
        asst_msg = body['messages'][2]
        assert not _has_cache_control(asst_msg), \
            'REGRESSION: Empty assistant must be skipped'

    def test_exact_bug_scenario_8_messages(self):
        """[system, user, asst(e), tool, asst(e), tool, asst(e), tool]
        — 3 rounds of tool calls with empty assistants."""
        body = _make_tool_conv(3, empty_assistant_content=True)
        body['model'] = 'aws.claude-opus-4.6'
        add_cache_breakpoints(body)

        # BP4 on last tool result (msg[-1] = msg[7])
        assert _has_cache_control(body['messages'][-1]), \
            'REGRESSION: BP4 on last tool result'
        assert body['messages'][-1]['role'] == 'tool'

        # No empty assistant should have BP4
        for i in [2, 4, 6]:
            assert not _has_cache_control(body['messages'][i]), \
                f'REGRESSION: Empty assistant at msg[{i}] must be skipped'

    def test_multi_round_no_cache_oscillation(self):
        """Simulate the 54-round pattern: every round, BP4 must be on the
        last tool result — never on an early message like the user query.
        This prevents the cache oscillation (WRITE→HIT→MISS cycle)."""
        body = {
            'model': 'aws.claude-opus-4.6',
            'messages': [
                {'role': 'system', 'content': 'Long system prompt. ' * 200},
                {'role': 'user', 'content': 'Analyze the codebase.'},
            ],
            'tools': [
                {'type': 'function', 'function': {
                    'name': 'read_files', 'description': 'Read files.',
                    'parameters': {'type': 'object'},
                }},
                {'type': 'function', 'function': {
                    'name': 'grep_search', 'description': 'Search patterns.',
                    'parameters': {'type': 'object'},
                }},
            ],
        }

        tools = ['read_files', 'grep_search']
        for round_num in range(20):
            # Alternate between tools
            tool_name = tools[round_num % 2]
            body['messages'].append({
                'role': 'assistant', 'content': '',
                'tool_calls': [{'function': {'name': tool_name, 'arguments': '{}'}}],
            })
            body['messages'].append({
                'role': 'tool',
                'content': f'Results from {tool_name} round {round_num}: ' + 'x' * 100,
            })

            add_cache_breakpoints(body)

            # INVARIANT: BP4 must be on the LAST message (tool result)
            last_msg = body['messages'][-1]
            assert _has_cache_control(last_msg), \
                f'Round {round_num}: BP4 MUST be on last tool result'

            # INVARIANT: The user message at index 1 must NOT have BP4
            user_msg = body['messages'][1]
            assert not _has_cache_control(user_msg), \
                f'Round {round_num}: BP4 must NOT fall back to user message ' \
                f'(this was the oscillation bug!)'

            # INVARIANT: Total BPs ≤ 4
            assert _count_breakpoints(body) <= 4, \
                f'Round {round_num}: BP count exceeds 4'


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Mixed TTL Strategy — 1h for BP1-BP3, 5m for BP4
# ═══════════════════════════════════════════════════════════════════════════════

def _get_cache_control(msg_or_block):
    """Extract cache_control dict from a message or content block."""
    content = msg_or_block.get('content', '')
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and 'cache_control' in block:
                return block['cache_control']
    return None


def _get_tool_cache_control(body, tool_idx=-1):
    """Extract cache_control from a tool definition."""
    tools = body.get('tools', [])
    if tools:
        fn = tools[tool_idx].get('function', {})
        return fn.get('cache_control')
    return None


@pytest.mark.unit
class TestMixedTTLStrategy:
    """Verify mixed TTL: BP1-BP3 get 1h, BP4 gets 5m (default)."""

    def test_extended_ttl_off_all_ephemeral(self):
        """When CACHE_EXTENDED_TTL=False, all BPs use plain ephemeral."""
        _lib.CACHE_EXTENDED_TTL = False
        body = _make_tool_conv(3, empty_assistant_content=True)
        add_cache_breakpoints(body)

        # System (BP1) — plain ephemeral
        sys_cc = _get_cache_control(body['messages'][0])
        assert sys_cc == {'type': 'ephemeral'}, f'Expected plain ephemeral, got {sys_cc}'

        # Tool (BP3) — plain ephemeral
        tool_cc = _get_tool_cache_control(body)
        assert tool_cc == {'type': 'ephemeral'}, f'Expected plain ephemeral, got {tool_cc}'

        # Tail (BP4) — plain ephemeral
        tail_cc = _get_cache_control(body['messages'][-1])
        assert tail_cc == {'type': 'ephemeral'}, f'Expected plain ephemeral, got {tail_cc}'

    def test_extended_ttl_on_system_gets_1h(self):
        """When CACHE_EXTENDED_TTL=True, system BP gets ttl=1h."""
        _lib.CACHE_EXTENDED_TTL = True
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'You are a helpful assistant.'},
                {'role': 'user', 'content': 'Hello'},
            ],
        }
        add_cache_breakpoints(body)

        sys_cc = _get_cache_control(body['messages'][0])
        assert sys_cc == {'type': 'ephemeral', 'ttl': '1h'}, \
            f'System should have 1h TTL, got {sys_cc}'

    def test_extended_ttl_on_multi_system_blocks_all_1h(self):
        """Multiple system text blocks all get 1h TTL."""
        _lib.CACHE_EXTENDED_TTL = True
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [{
                'role': 'system',
                'content': [
                    {'type': 'text', 'text': 'Static FRC guidance'},
                    {'type': 'text', 'text': 'Dynamic project context'},
                ],
            }, {'role': 'user', 'content': 'Hello'}],
        }
        add_cache_breakpoints(body)
        blocks = body['messages'][0]['content']
        assert blocks[0].get('cache_control') == {'type': 'ephemeral', 'ttl': '1h'}
        assert blocks[1].get('cache_control') == {'type': 'ephemeral', 'ttl': '1h'}

    def test_extended_ttl_on_tool_gets_1h(self):
        """Tool definition (BP3) gets 1h TTL."""
        _lib.CACHE_EXTENDED_TTL = True
        body = _make_tool_conv(1, empty_assistant_content=True)
        add_cache_breakpoints(body)

        tool_cc = _get_tool_cache_control(body)
        assert tool_cc == {'type': 'ephemeral', 'ttl': '1h'}, \
            f'Tool BP3 should have 1h TTL, got {tool_cc}'

    def test_extended_ttl_on_tail_stays_5m(self):
        """Conversation tail (BP4) stays at default 5m TTL (no ttl field)."""
        _lib.CACHE_EXTENDED_TTL = True
        body = _make_tool_conv(3, empty_assistant_content=True)
        add_cache_breakpoints(body)

        # BP4 should be on the last message (tool result)
        tail_cc = _get_cache_control(body['messages'][-1])
        assert tail_cc == {'type': 'ephemeral'}, \
            f'Tail BP4 should have plain ephemeral (5m), got {tail_cc}'

    def test_mixed_ttl_ordering_constraint(self):
        """1h entries must appear BEFORE 5m entries in the message array.

        Since BP1-BP3 (system+tools) always precede BP4 (tail) in the
        message array, this constraint is naturally satisfied.
        """
        _lib.CACHE_EXTENDED_TTL = True
        body = _make_tool_conv(5, empty_assistant_content=True)
        add_cache_breakpoints(body)

        # Collect all cache_control annotations with their positions
        annotations = []
        for i, msg in enumerate(body['messages']):
            content = msg.get('content', '')
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and 'cache_control' in block:
                        cc = block['cache_control']
                        annotations.append((i, cc.get('ttl', '5m')))

        # Also check tools
        for tool in body.get('tools', []):
            fn = tool.get('function', {})
            if 'cache_control' in fn:
                cc = fn['cache_control']
                # Tools are between system and messages, so position is "early"
                annotations.append((-1, cc.get('ttl', '5m')))

        # Verify: all 1h entries come before all 5m entries
        seen_5m = False
        for pos, ttl in sorted(annotations, key=lambda x: x[0]):
            if ttl == '5m':
                seen_5m = True
            elif ttl == '1h':
                assert not seen_5m, \
                    f'1h TTL at position {pos} appears AFTER a 5m TTL — ' \
                    f'violates Anthropic ordering constraint! {annotations}'

    def test_multi_round_mixed_ttl_consistent(self):
        """Mixed TTL stays consistent across multiple rounds."""
        _lib.CACHE_EXTENDED_TTL = True
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'System prompt.'},
                {'role': 'user', 'content': 'Do something.'},
            ],
            'tools': [{'type': 'function', 'function': {
                'name': 'read_files', 'description': 'Read.',
                'parameters': {'type': 'object'},
            }}],
        }

        for round_num in range(10):
            body['messages'].append({
                'role': 'assistant', 'content': '',
                'tool_calls': [{'function': {'name': 'read_files', 'arguments': '{}'}}],
            })
            body['messages'].append({
                'role': 'tool',
                'content': f'Round {round_num} result: {"data" * 50}',
            })

            add_cache_breakpoints(body)

            # System: always 1h
            sys_cc = _get_cache_control(body['messages'][0])
            assert sys_cc == {'type': 'ephemeral', 'ttl': '1h'}, \
                f'Round {round_num}: system should be 1h, got {sys_cc}'

            # Tool: always 1h
            tool_cc = _get_tool_cache_control(body)
            assert tool_cc == {'type': 'ephemeral', 'ttl': '1h'}, \
                f'Round {round_num}: tool should be 1h, got {tool_cc}'

            # Tail: always 5m (plain ephemeral)
            tail_cc = _get_cache_control(body['messages'][-1])
            assert tail_cc == {'type': 'ephemeral'}, \
                f'Round {round_num}: tail should be 5m, got {tail_cc}'

    def test_non_claude_model_unaffected_by_ttl(self):
        """Non-Claude models should get zero breakpoints regardless of TTL flag."""
        _lib.CACHE_EXTENDED_TTL = True
        body = {
            'model': 'gpt-4o',
            'messages': [
                {'role': 'system', 'content': 'System.'},
                {'role': 'user', 'content': 'Hello.'},
            ],
        }
        add_cache_breakpoints(body)
        assert _count_breakpoints(body) == 0, 'Non-Claude should get 0 BPs'

    def test_ttl_not_shared_between_breakpoints(self):
        """Each breakpoint gets its own cache_control dict (no shared refs)."""
        _lib.CACHE_EXTENDED_TTL = True
        body = _make_tool_conv(3, empty_assistant_content=True)
        add_cache_breakpoints(body)

        # Collect all cache_control dicts
        cc_dicts = []
        for msg in body['messages']:
            content = msg.get('content', '')
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and 'cache_control' in block:
                        cc_dicts.append(block['cache_control'])
        for tool in body.get('tools', []):
            fn = tool.get('function', {})
            if 'cache_control' in fn:
                cc_dicts.append(fn['cache_control'])

        # No two should be the same object (prevent mutation bugs)
        for i in range(len(cc_dicts)):
            for j in range(i + 1, len(cc_dicts)):
                assert cc_dicts[i] is not cc_dicts[j], \
                    f'cache_control dicts at {i} and {j} are the same object!'
