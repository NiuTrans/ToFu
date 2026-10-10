"""Tests for ask_human batch questions[] mode.

Batch mode lets the model ask 2-8 questions as a single form (one user
interaction).  The backend normalizes the array, blocks on one guidance
request, and parses the JSON-array response back into per-question answers.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lib.tasks_pkg.handlers.misc._human import (
    _handle_ask_human,
    _handle_ask_human_batch,
    _normalize_batch_questions,
)

pytestmark = pytest.mark.unit

_GUIDANCE_MODULE = 'lib.tasks_pkg.human_guidance'


def _make_task(**overrides):
    task = {
        'id': 'test-task-0001',
        'convId': 'conv-test',
        '_attended': True,
        'toolRounds': [],
        'messages': [],
    }
    task.update(overrides)
    return task


def _make_round_entry():
    return {'toolCallId': 'tc-1', 'toolName': 'ask_human', 'query': 'test'}


class TestNormalizeBatchQuestions:
    def test_valid_questions(self):
        qs = [
            {'question': 'What color?', 'response_type': 'choice',
             'options': [{'label': 'Red'}, {'label': 'Blue'}]},
            {'question': 'What size?', 'response_type': 'free_text'},
        ]
        result = _normalize_batch_questions(qs)
        assert len(result) == 2
        assert result[0]['question'] == 'What color?'
        assert result[0]['response_type'] == 'choice'
        assert len(result[0]['options']) == 2
        assert result[1]['response_type'] == 'free_text'

    def test_drops_non_dict(self):
        qs = [
            'not a dict',
            {'question': 'Valid?', 'response_type': 'free_text'},
        ]
        result = _normalize_batch_questions(qs)
        assert len(result) == 1

    def test_drops_empty_question(self):
        qs = [
            {'question': '', 'response_type': 'free_text'},
            {'question': 'Valid?', 'response_type': 'free_text'},
        ]
        result = _normalize_batch_questions(qs)
        assert len(result) == 1

    def test_coerces_options_from_string(self):
        qs = [
            {'question': 'Pick?', 'response_type': 'choice',
             'options': '[{"label": "A"}]'},
            {'question': 'Other?', 'response_type': 'free_text'},
        ]
        result = _normalize_batch_questions(qs)
        assert len(result) == 2
        assert result[0]['options'] == [{'label': 'A'}]

    def test_invalid_response_type_defaults_free_text(self):
        qs = [
            {'question': 'Q1?', 'response_type': 'bogus'},
            {'question': 'Q2?', 'response_type': 'free_text'},
        ]
        result = _normalize_batch_questions(qs)
        assert result[0]['response_type'] == 'free_text'


class TestBatchHandler:
    def test_batch_mode_unattended(self):
        task = _make_task(_attended=False)
        fn_args = {
            'questions': [
                {'question': 'Q1?', 'response_type': 'free_text'},
                {'question': 'Q2?', 'response_type': 'free_text'},
            ],
        }
        round_entry = _make_round_entry()
        tc_id, content, _ = _handle_ask_human(
            task, None, 'ask_human', 'tc-1', fn_args, 1, round_entry,
            None, None, False)
        assert 'no interactive session' in content.lower()
        assert round_entry['status'] == 'unanswerable'

    def test_batch_mode_falls_back_to_single(self):
        """Fewer than 2 valid questions falls through to single-question path."""
        task = _make_task(_attended=False)
        fn_args = {
            'question': '',
            'questions': [
                {'question': 'Only one?', 'response_type': 'free_text'},
            ],
        }
        round_entry = _make_round_entry()
        tc_id, content, _ = _handle_ask_human(
            task, None, 'ask_human', 'tc-1', fn_args, 1, round_entry,
            None, None, False)
        # Falls through to single-question path — empty question errors
        assert 'question' in content.lower()

    @pytest.mark.parametrize('questions, expected', [
        ([{'question': 'Only valid?'}, {'question': ''}], 'no interactive session'),
        ([{'question': ''}, None], 'question parameter is required'),
    ])
    def test_filtered_batch_falls_back_without_recursion(self, questions, expected):
        import copy

        fn_args = {'questions': questions}
        original = copy.deepcopy(fn_args)
        _, content, _ = _handle_ask_human(
            _make_task(_attended=False), None, 'ask_human', 'tc-1', fn_args,
            1, _make_round_entry(), None, None, False)
        assert expected in content.lower()
        assert fn_args == original

    def test_batch_mode_aborted(self):
        task = _make_task(aborted=True)
        fn_args = {
            'questions': [
                {'question': 'Q1?', 'response_type': 'free_text'},
                {'question': 'Q2?', 'response_type': 'free_text'},
            ],
        }
        round_entry = _make_round_entry()
        with patch(
            f'{_GUIDANCE_MODULE}.request_human_guidance',
            return_value=None,
        ):
            tc_id, content, _ = _handle_ask_human_batch(
                task, None, 'ask_human', 'tc-1', fn_args, 1, round_entry,
                None, None, False, fn_args['questions'])
        assert 'aborted' in content.lower()

    def test_batch_mode_with_json_response(self):
        task = _make_task()
        fn_args = {
            'questions': [
                {'question': 'Color?', 'response_type': 'choice',
                 'options': [{'label': 'Red'}, {'label': 'Blue'}]},
                {'question': 'Size?', 'response_type': 'free_text'},
            ],
        }
        round_entry = _make_round_entry()
        import json
        response = json.dumps([
            {'index': 0, 'answer': 'Red'},
            {'index': 1, 'answer': 'Large'},
        ])
        with patch(
            f'{_GUIDANCE_MODULE}.request_human_guidance',
            return_value=response,
        ):
            tc_id, content, _ = _handle_ask_human_batch(
                task, None, 'ask_human', 'tc-1', fn_args, 1, round_entry,
                None, None, False, fn_args['questions'])
        assert 'Red' in content
        assert 'Large' in content
        assert 'Q1:' in content
        assert 'A1:' in content
        assert 'Q2:' in content
        assert 'A2:' in content

    def test_batch_mode_with_plain_text_response(self):
        """Non-JSON response is applied to all questions."""
        task = _make_task()
        fn_args = {
            'questions': [
                {'question': 'Q1?', 'response_type': 'free_text'},
                {'question': 'Q2?', 'response_type': 'free_text'},
            ],
        }
        round_entry = _make_round_entry()
        with patch(
            f'{_GUIDANCE_MODULE}.request_human_guidance',
            return_value='same answer for all',
        ):
            tc_id, content, _ = _handle_ask_human_batch(
                task, None, 'ask_human', 'tc-1', fn_args, 1, round_entry,
                None, None, False, fn_args['questions'])
        assert 'same answer for all' in content


class TestBatchSchema:
    def test_schema_has_questions_field(self):
        from lib.tools.human_guidance import ASK_HUMAN_TOOL
        props = ASK_HUMAN_TOOL['function']['parameters']['properties']
        assert 'questions' in props
        assert props['questions']['minItems'] == 2
        assert props['questions']['maxItems'] == 8

    def test_schema_batch_items_have_question_and_type(self):
        from lib.tools.human_guidance import ASK_HUMAN_TOOL
        items = ASK_HUMAN_TOOL['function']['parameters']['properties'][
            'questions']['items']
        assert 'question' in items['properties']
        assert 'response_type' in items['properties']
        assert 'question' in items['required']
        assert 'response_type' in items['required']


@pytest.mark.parametrize('batch', [False, True])
def test_visible_question_is_already_answerable(monkeypatch, batch):
    from lib.tasks_pkg import human_guidance
    from lib.tasks_pkg.human_gate_registry import OwnedHumanGateRegistry
    from lib.tasks_pkg.handlers.misc import _human as handler
    registry = OwnedHumanGateRegistry(capacity=1)
    monkeypatch.setattr(human_guidance, 'human_gate_registry', registry)
    published = []
    def emit(task, event):
        if event['type'] == 'human_guidance_request':
            published.append(event)
            assert human_guidance.resolve_human_guidance(
                event['guidanceId'], 'instant', owner_user_id=1)
    monkeypatch.setattr(handler, 'append_event', emit)
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *a, **kw: None)
    args = {'questions': [{'question': 'Q1?'}, {'question': 'Q2?'}]} if batch else {'question': 'Q?'}
    _handle_ask_human(_make_task(_userId=1), None, 'ask_human', 'tc-1', args,
                      1, _make_round_entry(), None, None, False)
    assert len(published) == 1
    assert len(registry) == 0
