"""End-to-end test: confirm anomaly dump fires inside _stream_chat_once
for a real _empty_stop scenario (gateway returns finish=stop with empty
content + chunks > 0, like the bug we're chasing).

Mocks requests.post to return a canned SSE stream so we don't need network.
"""
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.pop('LLM_DEBUG_RAW_SSE', None)

_TMP = tempfile.mkdtemp(prefix='raw_sse_e2e_')
os.chdir(_TMP)

from lib.llm import stream as _llm_stream  # noqa: E402


class _FakeResp:
    """Minimal stand-in for requests.Response with iter_lines()."""

    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code
        self.headers = {'M-TraceId': 'fake-trace-resp'}
        self.encoding = 'utf-8'
        self.text = ''

    def iter_lines(self, decode_unicode=True):
        for ln in self._lines:
            yield ln

    def close(self):
        pass


def test_empty_stop_triggers_anomaly_dump():
    # Simulated gateway behavior: starts the stream, sends one chunk
    # with empty delta, then finish_reason=stop and [DONE].
    # Real Claude Opus would have content/thinking; this gateway response
    # is exactly what we saw in the bug report.
    canned = [
        'data: {"id":"chatcmpl-fake","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
        '',
        'data: {"id":"chatcmpl-fake","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":0}}',
        '',
        'data: [DONE]',
    ]

    captured = {}

    def fake_post(url, headers=None, json=None, stream=None, timeout=None,
                  proxies=None):
        captured['body'] = json
        return _FakeResp(canned)

    # Patch requests.post in lib.llm.stream module
    orig_post = _llm_stream.requests.post
    _llm_stream.requests.post = fake_post
    try:
        body = {
            'model': 'aws.claude-opus-4.7',
            'temperature': 1.0,
            'max_tokens': 4096,
            'stream': True,
            'messages': [{'role': 'user', 'content': 'hi'}],
        }
        msg, finish_reason, usage = _llm_stream._stream_chat_once(
            body, log_prefix='[test]', api_key='fake', base_url='http://x',
        )
    finally:
        _llm_stream.requests.post = orig_post

    # Sanity on what the function returned
    assert finish_reason == 'stop', f'finish_reason={finish_reason}'
    assert msg.get('content', '') == ''
    assert usage.get('_empty_stop') is True, f'usage={usage}'
    assert usage.get('_stream_anomaly') is True

    # Now the important bit: anomaly file must have been written
    log_path = pathlib.Path('logs/raw_sse_anomaly.log')
    assert log_path.exists(), 'anomaly log file was NOT written'
    text = log_path.read_text(encoding='utf-8')
    assert 'reason=empty_stop' in text, text[:500]
    assert 'aws.claude-opus-4.7' in text
    # The full raw SSE we sent should be visible — proves we kept the bytes
    assert '"finish_reason":"stop"' in text
    assert '[DONE]' in text
    print(f'  ok: empty_stop path triggered anomaly dump '
          f'({log_path.stat().st_size} bytes)')
    print(f'\n--- excerpt of {log_path} ---')
    print(text[:1200])


if __name__ == '__main__':
    print(f'Running e2e test in {_TMP}')
    test_empty_stop_triggers_anomaly_dump()
    print('\nE2E TEST PASSED')
