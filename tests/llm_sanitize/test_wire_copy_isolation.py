"""API projection owns mutable carriers and preserves Python copy semantics."""
from __future__ import annotations

import copy

import pytest
from hypothesis import given, strategies as st

from lib.llm_sanitize import _strip_non_api_fields

pytestmark = pytest.mark.unit

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda values: st.lists(values, max_size=8) | st.dictionaries(st.text(), values, max_size=8),
    max_leaves=40)


@given(json_values)
def test_projection_matches_deepcopy_for_nested_wire_content(content):
    message = {'role': 'user', 'content': [content], 'frontendMetadata': 'discard'}
    projected = _strip_non_api_fields([message])
    assert projected == [{'role': 'user', 'content': copy.deepcopy([content])}]
    projected[0]['content'].append('changed')
    assert message['content'] == [content]


def test_repeated_references_and_cycles_keep_isolation():
    shared = {'text': 'original'}
    content = [shared, shared]
    content.append(content)
    projected = _strip_non_api_fields([{'role': 'user', 'content': content}])[0]['content']
    assert projected[0] is projected[1]
    assert projected[2] is projected
    projected[0]['text'] = 'changed'
    assert shared['text'] == 'original'


def test_container_subclass_and_custom_copy_hooks_remain_authoritative():
    class CustomList(list):
        def __deepcopy__(self, memo):
            return ['custom-copy']

    carrier = {'content': [CustomList(['source']), (1, 2)], 'role': 'user'}
    assert _strip_non_api_fields([carrier])[0]['content'] == [['custom-copy'], (1, 2)]
    assert carrier['content'][0] == ['source']


def test_fields_remain_independently_copied():
    shared = [{'text': 'original'}]
    projected = _strip_non_api_fields([{'role': 'assistant', 'content': shared,
                                       'reasoning_details': shared}])[0]
    projected['content'][0]['text'] = 'changed'
    assert projected['reasoning_details'][0]['text'] == 'original'
    assert shared[0]['text'] == 'original'
