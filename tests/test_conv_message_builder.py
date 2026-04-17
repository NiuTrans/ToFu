"""Tests for lib/tasks_pkg/conv_message_builder.py — server-side message building."""

import json

import pytest

from lib.tasks_pkg.conv_message_builder import _transform_messages
from lib.tasks_pkg.conv_message_builder import build_branch_api_messages
from unittest.mock import patch


class TestTransformMessages:
    """Test _transform_messages (server-side equivalent of buildApiMessages)."""

    def test_basic_user_assistant(self):
        raw = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there'},
            {'role': 'user', 'content': 'Follow up'},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 3
        assert result[0] == {'role': 'user', 'content': 'Hello'}
        assert result[1] == {'role': 'assistant', 'content': 'Hi there'}
        assert result[2] == {'role': 'user', 'content': 'Follow up'}

    def test_system_prompt_injection(self):
        raw = [{'role': 'user', 'content': 'Hi'}]
        result = _transform_messages(raw, {'systemPrompt': 'Be helpful.'})
        assert len(result) == 2
        assert result[0] == {'role': 'system', 'content': 'Be helpful.'}
        assert result[1] == {'role': 'user', 'content': 'Hi'}

    def test_empty_system_prompt_not_injected(self):
        raw = [{'role': 'user', 'content': 'Hi'}]
        result = _transform_messages(raw, {'systemPrompt': '  '})
        assert len(result) == 1
        assert result[0]['role'] == 'user'

    def test_strip_notranslate_tags(self):
        raw = [{'role': 'user', 'content': 'Hello <notranslate>code</notranslate> world'}]
        result = _transform_messages(raw, {})
        assert '<notranslate>' not in result[0]['content']
        assert 'Hello code world' == result[0]['content']

    def test_strip_nt_tags(self):
        raw = [{'role': 'user', 'content': 'Hello <nt>code</nt> world'}]
        result = _transform_messages(raw, {})
        assert '<nt>' not in result[0]['content']

    def test_reply_quotes_single(self):
        raw = [{'role': 'user', 'content': 'My reply', 'replyQuotes': ['quoted text']}]
        result = _transform_messages(raw, {})
        assert '[引用]' in result[0]['content']
        assert 'quoted text' in result[0]['content']
        assert 'My reply' in result[0]['content']

    def test_reply_quotes_multiple(self):
        raw = [{'role': 'user', 'content': 'My reply', 'replyQuotes': ['quote1', 'quote2']}]
        result = _transform_messages(raw, {})
        assert '[引用1]' in result[0]['content']
        assert '[引用2]' in result[0]['content']

    def test_legacy_reply_quote(self):
        raw = [{'role': 'user', 'content': 'Reply', 'replyQuote': 'old quote'}]
        result = _transform_messages(raw, {})
        assert '[引用]' in result[0]['content']
        assert 'old quote' in result[0]['content']

    def test_conversation_references(self):
        raw = [{'role': 'user', 'content': 'See this',
                'convRefTexts': [{'id': 'abc', 'title': 'Old Conv', 'text': 'prev discussion'}]}]
        result = _transform_messages(raw, {})
        assert 'REFERENCED_CONVERSATION' in result[0]['content']
        assert 'prev discussion' in result[0]['content']
        assert 'Old Conv' in result[0]['content']

    def test_pdf_text_inline(self):
        raw = [{'role': 'user', 'content': 'Analyze this',
                'pdfTexts': [{'name': 'doc.pdf', 'pages': 5, 'textLength': 1000, 'text': 'PDF body'}]}]
        result = _transform_messages(raw, {})
        assert 'PDF Document: doc.pdf' in result[0]['content']
        assert 'PDF body' in result[0]['content']

    def test_multimodal_images(self):
        raw = [{'role': 'user', 'content': 'What is this?',
                'images': [{'base64': 'abc123', 'mediaType': 'image/png'}]}]
        result = _transform_messages(raw, {})
        content = result[0]['content']
        assert isinstance(content, list)
        assert content[0]['type'] == 'image_url'
        assert 'abc123' in content[0]['image_url']['url']
        assert content[1] == {'type': 'text', 'text': 'What is this?'}

    def test_image_with_url_fallback(self):
        raw = [{'role': 'user', 'content': 'Describe',
                'images': [{'url': '/api/images/test.png'}]}]
        result = _transform_messages(raw, {})
        content = result[0]['content']
        assert isinstance(content, list)
        assert content[0]['image_url']['url'] == '/api/images/test.png'

    def test_image_with_caption(self):
        raw = [{'role': 'user', 'content': 'Read this',
                'images': [{'base64': 'x', 'mediaType': 'image/png',
                            'caption': 'Figure 1', 'pdfPage': 3}]}]
        result = _transform_messages(raw, {})
        content = result[0]['content']
        assert any('[PDF p3: Figure 1]' in b.get('text', '') for b in content if b.get('type') == 'text')

    def test_empty_assistant_uses_tool_summary(self):
        raw = [
            {'role': 'user', 'content': 'Search'},
            {'role': 'assistant', 'content': '', 'toolSummary': 'searched for X'},
        ]
        result = _transform_messages(raw, {})
        assert result[1]['content'] == 'searched for X'

    def test_empty_assistant_uses_tool_rounds_fallback(self):
        raw = [
            {'role': 'user', 'content': 'Search'},
            {'role': 'assistant', 'content': '',
             'toolRounds': [{'toolName': 'web_search', 'query': 'test'}]},
        ]
        result = _transform_messages(raw, {})
        assert 'web_search' in result[1]['content']

    def test_skip_endpoint_planner(self):
        raw = [
            {'role': 'user', 'content': 'Do X'},
            {'role': 'assistant', 'content': 'Plan...', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'Done'},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 2
        assert result[1]['content'] == 'Done'

    def test_skip_endpoint_worker_iteration(self):
        """Worker turns with _epIteration should be filtered out."""
        raw = [
            {'role': 'user', 'content': 'Do X'},
            {'role': 'assistant', 'content': 'Plan...', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'Worker output', '_epIteration': 1},
            {'role': 'user', 'content': 'Feedback', '_isEndpointReview': True},
            {'role': 'assistant', 'content': 'Worker rev2', '_epIteration': 2},
        ]
        result = _transform_messages(raw, {})
        # All endpoint messages filtered → only user(Do X) remains
        assert len(result) == 1
        assert result[0]['content'] == 'Do X'

    def test_skip_endpoint_review(self):
        raw = [
            {'role': 'user', 'content': 'Do X'},
            {'role': 'assistant', 'content': 'Done'},
            {'role': 'user', 'content': 'Feedback', '_isEndpointReview': True},
            {'role': 'assistant', 'content': 'Revised'},
        ]
        result = _transform_messages(raw, {})
        # After merge: user, assistant(Done+Revised)
        assert len(result) == 2

    def test_merge_consecutive_same_role(self):
        raw = [
            {'role': 'user', 'content': 'A'},
            {'role': 'assistant', 'content': 'B', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'C'},
            {'role': 'user', 'content': 'D', '_isEndpointReview': True},
            {'role': 'assistant', 'content': 'E'},
        ]
        result = _transform_messages(raw, {})
        # After filtering: user(A), assistant(C), assistant(E)
        # After merge: user(A), assistant(C\n\nE)
        assert len(result) == 2
        assert 'C' in result[1]['content']
        assert 'E' in result[1]['content']

    def test_trailing_empty_assistant_stripped(self):
        raw = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': '', 'toolRounds': [], 'timestamp': 123},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 1
        assert result[0]['content'] == 'Hello'

    def test_trailing_nonempty_assistant_kept(self):
        raw = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Response'},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 2

    def test_exclude_last(self):
        raw = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'A1'},
            {'role': 'user', 'content': 'Q2'},
        ]
        result = _transform_messages(raw, {}, exclude_last=True)
        assert len(result) == 2
        assert result[-1]['content'] == 'A1'

    def test_metadata_not_leaked(self):
        """Ensure frontend metadata fields don't leak into API messages."""
        raw = [
            {'role': 'user', 'content': 'Hi',
             'timestamp': 123, 'images': [], 'pdfTexts': [],
             'originalContent': 'original', '_translateDone': True},
            {'role': 'assistant', 'content': 'Hello',
             'thinking': 'thoughts', 'translatedContent': 'translated',
             'toolRounds': [], 'usage': {'tokens': 100},
             'model': 'gpt-4o', 'finishReason': 'stop'},
        ]
        result = _transform_messages(raw, {})
        for msg in result:
            assert 'timestamp' not in msg
            assert 'thinking' not in msg
            assert 'translatedContent' not in msg
            assert 'usage' not in msg
            assert 'model' not in msg
            assert 'finishReason' not in msg
            assert 'originalContent' not in msg
            assert '_translateDone' not in msg

    def test_empty_messages(self):
        result = _transform_messages([], {})
        assert result == []

    def test_historical_endpoint_session_collapsed(self):
        """Completed endpoint session should collapse to last worker output for follow-ups."""
        raw = [
            {'role': 'user', 'content': 'Question 1'},
            {'role': 'assistant', 'content': 'Plan for Q1', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'Worker answer to Q1', '_epIteration': 1},
            {'role': 'user', 'content': 'Critic feedback', '_isEndpointReview': True,
             '_epIteration': 1, '_epApproved': True},
            {'role': 'user', 'content': 'Question 2'},
        ]
        result = _transform_messages(raw, {})
        # Should be: user(Q1), assistant(worker answer), user(Q2)
        assert len(result) == 3
        assert result[0]['content'] == 'Question 1'
        assert result[1]['role'] == 'assistant'
        assert result[1]['content'] == 'Worker answer to Q1'
        assert result[2]['content'] == 'Question 2'

    def test_historical_endpoint_multi_iteration_collapsed(self):
        """Multi-iteration endpoint session should use the LAST worker output."""
        raw = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'Plan', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'Worker v1', '_epIteration': 1},
            {'role': 'user', 'content': 'Fix X', '_isEndpointReview': True, '_epIteration': 1},
            {'role': 'assistant', 'content': 'Worker v2 (final)', '_epIteration': 2},
            {'role': 'user', 'content': 'Approved', '_isEndpointReview': True,
             '_epIteration': 2, '_epApproved': True},
            {'role': 'user', 'content': 'Q2'},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 3
        assert result[0]['content'] == 'Q1'
        assert result[1]['content'] == 'Worker v2 (final)'
        assert result[2]['content'] == 'Q2'

    def test_trailing_endpoint_session_skipped(self):
        """Trailing (in-progress) endpoint session should be fully skipped."""
        raw = [
            {'role': 'user', 'content': 'Do something'},
            {'role': 'assistant', 'content': 'Planning...', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'Working...', '_epIteration': 1},
        ]
        result = _transform_messages(raw, {})
        # All endpoint messages filtered, only original user remains
        assert len(result) == 1
        assert result[0]['content'] == 'Do something'

    def test_multiple_historical_endpoint_sessions(self):
        """Multiple completed endpoint sessions each collapse independently."""
        raw = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'Plan1', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'Answer1', '_epIteration': 1},
            {'role': 'user', 'content': 'OK1', '_isEndpointReview': True,
             '_epIteration': 1, '_epApproved': True},
            {'role': 'user', 'content': 'Q2'},
            {'role': 'assistant', 'content': 'Plan2', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'Answer2', '_epIteration': 1},
            {'role': 'user', 'content': 'OK2', '_isEndpointReview': True,
             '_epIteration': 1, '_epApproved': True},
            {'role': 'user', 'content': 'Q3'},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 5
        assert result[0]['content'] == 'Q1'
        assert result[1]['content'] == 'Answer1'
        assert result[2]['content'] == 'Q2'
        assert result[3]['content'] == 'Answer2'
        assert result[4]['content'] == 'Q3'

    def test_historical_endpoint_aborted_during_planning(self):
        """Endpoint session aborted during planning (no worker) → skip entire block."""
        raw = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'Plan...', '_isEndpointPlanner': True},
            # aborted, no worker turn
            {'role': 'user', 'content': 'Q2'},
        ]
        result = _transform_messages(raw, {})
        # Planner-only block is historical, no worker → skip.
        # The two user messages merge.
        assert len(result) == 1
        assert 'Q1' in result[0]['content']
        assert 'Q2' in result[0]['content']

    def test_multimodal_merge(self):
        """Consecutive user messages with mixed content types merge correctly."""
        raw = [
            {'role': 'user', 'content': 'Text only', '_isEndpointReview': False},
            {'role': 'user', 'content': 'More text'},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 1
        assert 'Text only' in result[0]['content']
        assert 'More text' in result[0]['content']


class TestBuildBranchApiMessages:
    """Test build_branch_api_messages — server-side branch message building."""

    def _mock_load(self, messages):
        """Create a mock for _load_messages_from_db that returns given messages."""
        return patch(
            'lib.tasks_pkg.conv_message_builder._load_messages_from_db',
            return_value=messages,
        )

    def test_basic_branch_from_assistant(self):
        """Branch from an assistant message — context is up to the preceding user."""
        conv_messages = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'A1'},
            {'role': 'user', 'content': 'Q2'},
            {'role': 'assistant', 'content': 'A2',
             'branches': [{
                 'title': 'Deep dive',
                 'messages': [
                     {'role': 'user', 'content': 'Tell me more about X'},
                     {'role': 'assistant', 'content': 'Here is more about X'},
                     {'role': 'user', 'content': 'And Y?'},
                     {'role': 'assistant', 'content': ''},  # trailing placeholder
                 ],
             }]},
        ]
        with self._mock_load(conv_messages):
            result = build_branch_api_messages('conv1', 3, 0, {})
        # Context: Q1, A1 (Q2 excluded because branch is on A2 which was triggered by Q2)
        # + decorated branch user + branch assistant + branch user
        # Context ends before Q2 (msgIdx=3 is assistant, walk back: Q2 at idx=2 is user → contextEnd=2)
        assert result is not None
        assert len(result) == 5
        assert result[0]['content'] == 'Q1'
        assert result[1]['content'] == 'A1'
        # First branch user decorated with topic
        assert '[分支话题: Deep dive]' in result[2]['content']
        assert 'Tell me more about X' in result[2]['content']
        assert result[3]['content'] == 'Here is more about X'
        assert 'And Y?' in result[4]['content']

    def test_branch_with_selection_context(self):
        """Branch with parentSelection should include it in the first user message."""
        conv_messages = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'A1 with details',
             'branches': [{
                 'title': 'Clarify',
                 'parentSelection': 'selected text from A1',
                 'messages': [
                     {'role': 'user', 'content': 'Explain this'},
                     {'role': 'assistant', 'content': ''},
                 ],
             }]},
        ]
        with self._mock_load(conv_messages):
            result = build_branch_api_messages('conv1', 1, 0, {})
        assert result is not None
        # Context: empty (branch on msg[1] which is assistant preceded by user at [0],
        #   contextEnd = 0, main_context = messages[:0] = [])
        # + decorated branch user (with topic + selection)
        assert len(result) == 1
        assert '[选中的上下文]' in result[0]['content']
        assert 'selected text from A1' in result[0]['content']
        assert 'Explain this' in result[0]['content']

    def test_branch_with_system_prompt(self):
        """System prompt from config should be injected."""
        conv_messages = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'A1',
             'branches': [{
                 'title': 'Test',
                 'messages': [
                     {'role': 'user', 'content': 'Branch Q'},
                     {'role': 'assistant', 'content': ''},
                 ],
             }]},
        ]
        with self._mock_load(conv_messages):
            result = build_branch_api_messages('conv1', 1, 0, {'systemPrompt': 'Be helpful'})
        assert result[0] == {'role': 'system', 'content': 'Be helpful'}

    def test_branch_invalid_msg_idx(self):
        """Out-of-range msgIdx should return None."""
        conv_messages = [{'role': 'user', 'content': 'Q1'}]
        with self._mock_load(conv_messages):
            result = build_branch_api_messages('conv1', 5, 0, {})
        assert result is None

    def test_branch_invalid_branch_idx(self):
        """Out-of-range branchIdx should return None."""
        conv_messages = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'A1', 'branches': []},
        ]
        with self._mock_load(conv_messages):
            result = build_branch_api_messages('conv1', 1, 0, {})
        assert result is None

    def test_branch_no_branches_field(self):
        """Message without branches field should return None."""
        conv_messages = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'A1'},
        ]
        with self._mock_load(conv_messages):
            result = build_branch_api_messages('conv1', 1, 0, {})
        assert result is None

    def test_branch_conv_not_found(self):
        """Missing conversation should return None."""
        with self._mock_load(None):
            result = build_branch_api_messages('nonexistent', 0, 0, {})
        assert result is None

    def test_branch_context_includes_endpoint_collapse(self):
        """Main context with endpoint sessions should be collapsed before branch."""
        conv_messages = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'Plan', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'Worker answer', '_epIteration': 1},
            {'role': 'user', 'content': 'OK', '_isEndpointReview': True,
             '_epIteration': 1, '_epApproved': True},
            {'role': 'user', 'content': 'Q2'},
            {'role': 'assistant', 'content': 'Normal A2',
             'branches': [{
                 'title': 'Branch',
                 'messages': [
                     {'role': 'user', 'content': 'Branch Q'},
                     {'role': 'assistant', 'content': ''},
                 ],
             }]},
        ]
        with self._mock_load(conv_messages):
            result = build_branch_api_messages('conv1', 5, 0, {})
        # Context should be: Q1, collapsed worker answer, Q2
        # (endpoint planner/review skipped, worker kept as normal assistant)
        # Then branch user
        # contextEnd for msg[5] (assistant): walk back from 4, user at 4 → contextEnd=4
        # main_context = messages[:4] = [Q1, planner, worker, review]
        # After _transform_messages collapses: Q1, worker_answer
        # But Q2 is at index 4, not included in main_context[:4]
        # Actually: contextEnd starts at msgIdx=5, walks back: idx=4 is user → contextEnd=4
        # main_context = messages[:4] = [Q1, planner(ep), worker(ep), review(ep)]
        # After transform: Q1, worker_answer (endpoint collapsed)
        # Then branch user: [分支话题: Branch] Branch Q
        assert result is not None
        assert any('Q1' in str(m.get('content', '')) for m in result)
        assert any('Worker answer' in str(m.get('content', '')) for m in result)
        assert any('Branch Q' in str(m.get('content', '')) for m in result)

    def test_branch_from_user_message(self):
        """Branch from a user message — context is up to that index."""
        conv_messages = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'A1'},
            {'role': 'user', 'content': 'Q2',
             'branches': [{
                 'title': 'Sidebar',
                 'messages': [
                     {'role': 'user', 'content': 'Side question'},
                     {'role': 'assistant', 'content': ''},
                 ],
             }]},
        ]
        with self._mock_load(conv_messages):
            result = build_branch_api_messages('conv1', 2, 0, {})
        # contextEnd = msgIdx = 2 (user message, not assistant)
        # main_context = messages[:2] = [Q1, A1]
        assert result is not None
        assert len(result) == 3
        assert result[0]['content'] == 'Q1'
        assert result[1]['content'] == 'A1'
        assert '[分支话题: Sidebar]' in result[2]['content']

    def test_branch_multi_turn(self):
        """Multi-turn branch conversation preserves full alternation."""
        conv_messages = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'A1',
             'branches': [{
                 'title': 'Deep',
                 'messages': [
                     {'role': 'user', 'content': 'Branch Q1'},
                     {'role': 'assistant', 'content': 'Branch A1'},
                     {'role': 'user', 'content': 'Branch Q2'},
                     {'role': 'assistant', 'content': 'Branch A2'},
                     {'role': 'user', 'content': 'Branch Q3'},
                     {'role': 'assistant', 'content': ''},  # placeholder
                 ],
             }]},
        ]
        with self._mock_load(conv_messages):
            result = build_branch_api_messages('conv1', 1, 0, {})
        # No main context (branch on msg[1], which is assistant at idx 1,
        # walk back: idx 0 is user → contextEnd=0, main_context=[])
        # Branch: 5 messages (6 minus placeholder)
        assert result is not None
        assert len(result) == 5
        assert result[0]['role'] == 'user'
        assert result[1]['role'] == 'assistant'
        assert result[2]['role'] == 'user'
        assert result[3]['role'] == 'assistant'
        assert result[4]['role'] == 'user'

    def test_branch_reply_quotes_handled(self):
        """Reply quotes in branch messages should be processed by _transform_messages."""
        conv_messages = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'A1',
             'branches': [{
                 'title': 'Test',
                 'messages': [
                     {'role': 'user', 'content': 'About this',
                      'replyQuotes': ['quoted from A1']},
                     {'role': 'assistant', 'content': ''},
                 ],
             }]},
        ]
        with self._mock_load(conv_messages):
            result = build_branch_api_messages('conv1', 1, 0, {})
        assert result is not None
        # The branch user message should have the quote prepended
        user_msg = result[0]
        assert '[引用]' in user_msg['content']
        assert 'quoted from A1' in user_msg['content']
