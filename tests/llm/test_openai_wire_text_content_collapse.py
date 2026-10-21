"""OpenAI wire: text-only content part lists collapse to bare strings.

Strict OpenAI-compatible gateways (sankuai AIGC validation stage, probed
2026-08-31 with MiniMax-M2) hard-reject list-form message content with
HTTP 400 — even a single ``{'type': 'text', 'text': ...}`` part — and only
accept bare strings. ``prepare_request`` therefore collapses text-only part
lists at the single serialization boundary. Parts carrying ``cache_control``
or non-text keys keep the list form, and the caller's canonical body dict
(reused on transport retries) must stay untouched.
"""

from lib.llm._sse_core import prepare_request


def _plan(messages, api_protocol='openai'):
    body = {
        'model': 'MiniMax-M2',
        'messages': messages,
        'stream': True,
    }
    return body, prepare_request(
        body,
        api_key='unit-test-key',
        base_url='https://example.invalid/v1',
        api_protocol=api_protocol,
    )


class TestTextOnlyCollapse:
    def test_single_text_part_collapses(self):
        _, plan = _plan([
            {'role': 'user',
             'content': [{'type': 'text', 'text': 'hello'}]},
        ])
        assert plan.body['messages'][0]['content'] == 'hello'

    def test_multiple_text_parts_join(self):
        _, plan = _plan([
            {'role': 'system',
             'content': [{'type': 'text', 'text': 'a'},
                         {'type': 'text', 'text': 'b'}]},
        ])
        assert plan.body['messages'][0]['content'] == 'ab'

    def test_string_content_untouched(self):
        _, plan = _plan([{'role': 'user', 'content': 'hi'}])
        assert plan.body['messages'][0]['content'] == 'hi'

    def test_cache_control_parts_keep_list_form(self):
        parts = [{'type': 'text', 'text': 'a',
                  'cache_control': {'type': 'ephemeral'}}]
        _, plan = _plan([{'role': 'user', 'content': parts}])
        assert plan.body['messages'][0]['content'] == parts

    def test_multimodal_parts_keep_list_form(self):
        parts = [{'type': 'text', 'text': 'look'},
                 {'type': 'image_url', 'image_url': {'url': 'data:...'}}]
        _, plan = _plan([{'role': 'user', 'content': parts}])
        assert plan.body['messages'][0]['content'] == parts

    def test_canonical_body_not_mutated(self):
        parts = [{'type': 'text', 'text': 'a'}]
        body, plan = _plan([{'role': 'user', 'content': parts}])
        assert plan.body['messages'][0]['content'] == 'a'
        assert body['messages'][0]['content'] == parts, (
            'the caller canonical dict is reused on retries; the collapse '
            'must be wire-only')

    def test_anthropic_protocol_not_collapsed(self):
        parts = [{'type': 'text', 'text': 'a'}]
        _, plan = _plan([{'role': 'user', 'content': parts}],
                        api_protocol='anthropic')
        content = plan.body['messages'][0]['content']
        assert isinstance(content, list), (
            'the Anthropic translator speaks blocks natively; collapsing '
            'there would be a no-op at best and is out of scope')
