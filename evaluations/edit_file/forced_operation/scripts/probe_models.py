"""Smoke-probe candidate models through Tofu's own /v1/chat/completions route.

One tiny forced-tool request per model; prints reachability, latency, usage keys.
Never prints credentials (the local route is open-mode; no key is sent).
"""
import json
import sys
import time
import urllib.request

URL = 'http://127.0.0.1:15000/v1/chat/completions'
TOOL = {'type': 'function', 'function': {
    'name': 'record', 'description': 'Record one word.',
    'parameters': {'type': 'object', 'properties': {'word': {'type': 'string'}},
                   'required': ['word'], 'additionalProperties': False}}}


def probe(model, tool_choice):
    body = {'model': model, 'stream': False, 'messages': [
        {'role': 'user', 'content': 'Call the record tool with the word "apple". Do nothing else.'}],
        'tools': [TOOL], 'tool_choice': tool_choice, 'timeout_s': 300}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=320) as r:
            payload = json.loads(r.read())
            status = r.status
    except urllib.error.HTTPError as e:
        return {'model': model, 'http': e.code, 'err': e.read()[:400].decode('utf-8', 'replace'),
                's': round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001 - probe reports any transport failure
        return {'model': model, 'err': repr(e)[:300], 's': round(time.time() - t0, 1)}
    msg = payload['choices'][0]['message']
    return {'model': model, 'http': status, 's': round(time.time() - t0, 1),
            'tool_calls': [(c['function']['name'], c['function']['arguments'])
                           for c in (msg.get('tool_calls') or [])],
            'content': (msg.get('content') or '')[:80],
            'has_reasoning': bool(msg.get('reasoning_content')),
            'finish': payload['choices'][0].get('finish_reason'),
            'usage': payload.get('usage')}


if __name__ == '__main__':
    choice = sys.argv[1] if len(sys.argv) > 1 else 'required'
    for m in sys.argv[2:]:
        print(json.dumps(probe(m, choice), ensure_ascii=False), flush=True)
