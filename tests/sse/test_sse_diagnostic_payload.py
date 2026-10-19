"""Decoded SSE diagnostics measure original data and retain only safe evidence."""
import json
import time

import pytest

from lib.llm import diagnostics
from lib.llm._sse_core import SSEAccumulator

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('padding', ['', '  '])
def test_default_accumulator_diagnostics_measure_original_payload(tmp_path, monkeypatch, padding):
    monkeypatch.setattr(diagnostics, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(diagnostics, '_RAW_SSE_FILTER', '')
    dumper = diagnostics.RawSSEDumper('model', 'trace', {})
    acc = SSEAccumulator({}, 'trace', dumper, None, time.monotonic())
    payload = padding + json.dumps({
        'choices': [{'delta': {'content': '私密-CANARY-BODY'}}],
        'api_key': 'CANARY-CREDENTIAL',
        'metadata': {'arguments': 'CANARY-TOOL-FRAGMENT'},
    }, ensure_ascii=False)
    acc.feed_payload(payload)
    acc.feed_payload('[DONE]')
    expected = f'data: <omitted payload chars={len(payload)}>'
    assert list(dumper._ring) == [expected, 'data: [DONE]']
    dumper.dump_anomaly('test')
    text = (tmp_path / 'raw_sse_anomaly.log').read_text()
    assert expected in text
    assert 'CANARY' not in text
    assert not (tmp_path / 'raw_sse.log').exists()


def test_decoded_opt_in_payload_keeps_original_evidence_with_byte_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(diagnostics, '_RAW_SSE_FILTER', '1')
    dumper = diagnostics.RawSSEDumper('model', 'trace', {})
    dumper.start()
    try:
        for fragment in ('CANARY-', 'TOOL-FRAGMENT'):
            dumper.payload(json.dumps({
                'choices': [{'delta': {'content': 'visible text', 'tool_calls': [{
                    'function': {'arguments': fragment},
                }]}}],
                'api_key': 'CANARY-CREDENTIAL',
            }))
        huge = json.dumps({'text': 'CANARY-HUGE' * 10000})
        dumper.payload(huge)
        dumper.payload('{"CANARY-MALFORMED')
        dumper.dump_anomaly('test')
    finally:
        dumper.finish()
    for path in tmp_path.glob('raw_sse*.log'):
        text = path.read_text()
        assert 'visible text' in text
        assert 'TOOL-FRAGMENT' in text
        assert 'CANARY-CREDENTIAL' in text
        assert f'omitted payload chars={len(huge)}' in text
        assert 'malformed payload chars=' in text
        assert 'CANARY-HUGE' not in text
        assert 'CANARY-MALFORMED' not in text
