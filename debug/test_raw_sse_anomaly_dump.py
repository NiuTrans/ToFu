"""Smoke tests for the raw-SSE anomaly dumper.

Verifies that:
  1. _RawSSEDumper.line() always feeds the ring buffer, even when
     env-gated transcript dumping is OFF (default).
  2. Ring buffer respects both line-count and byte caps.
  3. dump_anomaly() writes a real file under logs/ with the request
     snapshot, summary kwargs, and ring contents — and is idempotent.
  4. Old captured lines beyond the cap are evicted in FIFO order.

Run:  python debug/test_raw_sse_anomaly_dump.py
"""
import os
import pathlib
import sys
import tempfile

# Ensure project root on sys.path so `import lib.*` works when the script
# is run directly from anywhere.
_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Force-disable env-based transcript dumping for this test (we want to
# confirm the ring buffer + anomaly path work without it).
os.environ.pop('LLM_DEBUG_RAW_SSE', None)

# Run inside a tempdir so logs/raw_sse_anomaly.log we create here
# doesn't pollute the real project log dir.
_TMP = tempfile.mkdtemp(prefix='raw_sse_anomaly_test_')
os.chdir(_TMP)

from lib.llm import diagnostics as _llm_diag  # noqa: E402


def _new_dumper(model='aws.claude-opus-4.7', body=None):
    body = body or {
        'model': model,
        'temperature': 1.0,
        'max_tokens': 4096,
        'messages': [{'role': 'user', 'content': 'hi'}],
        'tools': [{'type': 'function'}],
    }
    return _llm_diag.RawSSEDumper(model, 'trace-abc-123', body)


def test_disabled_when_env_unset():
    d = _new_dumper()
    assert d.enabled is False, 'env not set → enabled should be False'
    print('  ok: enabled=False when env unset')


def test_ring_buffer_records_lines_when_disabled():
    d = _new_dumper()
    d.line('data: {"a":1}')
    d.line('')
    d.line('data: [DONE]')
    assert len(d._ring) == 3, f'ring should have 3 lines, got {len(d._ring)}'
    assert d._ring[0] == 'data: {"a":1}'
    assert d._ring[2] == 'data: [DONE]'
    print('  ok: ring captures 3 lines while disabled')


def test_ring_buffer_caps_by_line_count():
    d = _new_dumper()
    for i in range(_llm_diag._ANOMALY_RING_LINES + 50):
        d.line(f'line-{i}')
    assert len(d._ring) == _llm_diag._ANOMALY_RING_LINES, (
        f'ring exceeded line cap: {len(d._ring)}')
    # Oldest evicted, newest retained
    assert d._ring[0] == 'line-50'
    assert d._ring[-1] == f'line-{_llm_diag._ANOMALY_RING_LINES + 49}'
    print('  ok: ring evicts FIFO at line-count cap')


def test_ring_buffer_caps_by_bytes():
    d = _new_dumper()
    big = 'X' * 50000  # 50 KB per line
    # 256 KB / 50 KB ≈ 5 lines max
    for _ in range(20):
        d.line(big)
    assert d._ring_bytes <= _llm_diag._ANOMALY_RING_BYTES, (
        f'ring exceeded byte cap: {d._ring_bytes}')
    # And still has at least a few lines (proves it didn't drop them all)
    assert len(d._ring) >= 1
    print(f'  ok: ring respects byte cap ({d._ring_bytes} <= '
          f'{_llm_diag._ANOMALY_RING_BYTES})')


def test_dump_anomaly_writes_file_when_disabled():
    d = _new_dumper()
    d.line('data: {"id":"chatcmpl-xyz"}')
    d.line('data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}')
    d.line('')  # blank keepalive
    # Note: NO [DONE] line — simulates the real-world anomaly

    d.dump_anomaly(
        'empty_stop',
        elapsed_s=5.7,
        chunks=2,
        thinking_len=0,
        finish_reason='stop',
    )

    log_path = pathlib.Path('logs/raw_sse_anomaly.log')
    assert log_path.exists(), f'expected {log_path} to exist'
    text = log_path.read_text(encoding='utf-8')
    assert 'reason=empty_stop' in text
    assert 'trace=trace-abc-123' in text
    assert 'aws.claude-opus-4.7' in text
    assert '"chunks": 2' in text or "'chunks': 2" in text or 'chunks": 2' in text
    assert 'data: {"id":"chatcmpl-xyz"}' in text
    assert 'data: {"choices":[' in text
    assert 'ring_lines=3' in text
    print(f'  ok: anomaly file created with all expected fields '
          f'({log_path.stat().st_size} bytes)')


def test_dump_anomaly_is_idempotent():
    d = _new_dumper()
    d.line('one')
    d.line('two')

    log_path = pathlib.Path('logs/raw_sse_anomaly.log')
    size_before = log_path.stat().st_size if log_path.exists() else 0

    d.dump_anomaly('first_reason', x=1)
    size_after_first = log_path.stat().st_size

    d.dump_anomaly('second_reason', x=2)
    size_after_second = log_path.stat().st_size

    assert size_after_first > size_before, 'first dump should write'
    assert size_after_second == size_after_first, (
        f'second dump should be a no-op '
        f'({size_after_second} != {size_after_first})')
    text = log_path.read_text(encoding='utf-8')
    assert 'reason=first_reason' in text
    assert 'reason=second_reason' not in text, (
        'second dump leaked into file — idempotency broken')
    print('  ok: dump_anomaly is idempotent (only first call writes)')


def test_dump_anomaly_handles_unserializable_summary():
    d = _new_dumper()
    d.line('hi')
    # Pass an object that json can't serialize natively
    class Weird:
        def __repr__(self): return '<weird>'
    d.dump_anomaly('weird_payload', obj=Weird())
    log_path = pathlib.Path('logs/raw_sse_anomaly.log')
    text = log_path.read_text(encoding='utf-8')
    assert 'weird_payload' in text
    assert '<weird>' in text  # default=str fallback
    print('  ok: summary with unserializable values handled gracefully')


def main():
    tests = [
        test_disabled_when_env_unset,
        test_ring_buffer_records_lines_when_disabled,
        test_ring_buffer_caps_by_line_count,
        test_ring_buffer_caps_by_bytes,
        test_dump_anomaly_writes_file_when_disabled,
        test_dump_anomaly_is_idempotent,
        test_dump_anomaly_handles_unserializable_summary,
    ]
    print(f'Running {len(tests)} tests in {_TMP}')
    for t in tests:
        print(f'\n[{t.__name__}]')
        t()
    print(f'\nALL {len(tests)} TESTS PASSED')


if __name__ == '__main__':
    main()
