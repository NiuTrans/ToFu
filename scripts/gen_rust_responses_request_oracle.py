#!/usr/bin/env python3
"""Derive native core request fixtures from the canonical Python converter."""
import argparse
import copy
import json
from pathlib import Path

from lib.llm.responses_outbound._to_responses import openai_body_to_responses

TARGET = Path(__file__).resolve().parents[1] / 'packages/tofu-runtime/tests/fixtures/python_responses_request.json'


def generate():
    phase = [{'type': 'message', 'role': 'assistant', 'phase': p,
              'id': p, 'content': [{'type': 'output_text', 'text': t, 'annotations': []}]}
             for p, t in [('commentary', 'plan'), ('final_answer', 'answer')]]
    long_name = '工具' * 40
    cases = [
        ('text', {'messages': [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': '你好😀'}]}),
        ('image', {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'look'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA', 'detail': 'low'}}]}]}),
        ('tools', {'tools': [{'type': 'function', 'function': {'name': long_name, 'description': 'read', 'parameters': {'type': 'object'}, 'strict': True}}], 'tool_choice': {'type': 'function', 'function': {'name': long_name}}, 'messages': [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call', 'type': 'function', 'function': {'name': long_name, 'arguments': '{}'}}]}, {'role': 'tool', 'tool_call_id': 'call', 'content': 'result'}]}),
        ('tool_image', {'messages': [{'role': 'tool', 'tool_call_id': 'call', 'content': [{'type': 'text', 'text': 'image'}, {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/image', 'detail': 'original'}}]}]}),
        ('schema', {'response_format': {'type': 'json_schema', 'json_schema': {'name': 'answer', 'schema': {'type': 'object'}, 'strict': True, 'description': 'answer'}}}),
        ('reasoning', {'reasoning_effort': 'high', 'reasoning': {'summary': 'concise'}}),
        ('phase_concat', {'messages': [{'role': 'assistant', 'content': 'plananswer', '_responses_items': phase}]}),
        ('phase_newline', {'messages': [{'role': 'assistant', 'content': 'plan\nanswer', '_responses_items': phase}]}),
        ('phase_edited', {'messages': [{'role': 'assistant', 'content': 'edited', '_responses_items': phase}]}),
        ('encrypted', {'messages': [{'role': 'assistant', 'content': 'answer', '_responses_items': [{'type': 'reasoning', 'id': 'r', 'encrypted_content': 'opaque'}]}]}),
    ]
    result = []
    for name, values in cases:
        replay = name == 'encrypted'
        body = {'model': 'fixture', 'max_tokens': 128, 'messages': [], **values}
        oracle = copy.deepcopy(body)
        oracle['_route_inference_profile'] = {'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort', 'supported_efforts': ['high']}, 'features': {'responses_reasoning_replay': 'supported' if replay else 'unsupported'}}
        output, reverse = openai_body_to_responses(oracle, stream=True)
        output.pop('_conversion_report', None)
        result.append({'name': name, 'body': body, 'replay': replay, 'output': output, 'reverse': reverse})
    for name, values in cases:
        for replay in (False, True):
            if name == "encrypted" and not replay:
                continue
            body = {'model': 'fixture', 'max_tokens': 128, 'temperature': 0.4, 'top_p': 0.8, 'messages': [], **values}
            oracle = copy.deepcopy(body)
            oracle['_route_inference_profile'] = {'responses': {'feature_profile': 'codex'}, 'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort', 'supported_efforts': ['high']}, 'features': {'responses_reasoning_replay': 'supported' if replay else 'unsupported'}}
            output, reverse = openai_body_to_responses(oracle, stream=True, profile='codex')
            output.pop('_conversion_report', None)
            result.append({'name': 'codex_'+name+('_replay' if replay else ''), 'profile': 'codex', 'body': body, 'replay': replay, 'output': output, 'reverse': reverse})
    return json.dumps(result, ensure_ascii=False, indent=2) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected = generate()
    if args.check:
        if TARGET.read_text() != expected:
            raise SystemExit('Responses request oracle drift')
    else:
        TARGET.write_text(expected)
