"""Always-on raw SSE diagnostics must be bounded and concurrency-safe."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.llm import diagnostics

pytestmark = pytest.mark.unit


def test_anomaly_append_rotates_before_crossing_limit(tmp_path, monkeypatch):
    path = tmp_path / 'raw_sse_anomaly.log'
    path.write_bytes(b'x' * (1 << 20))
    monkeypatch.setenv('TOFU_RAW_SSE_ANOMALY_MAX_BYTES', str(1 << 20))
    monkeypatch.setenv('TOFU_RAW_SSE_ANOMALY_BACKUPS', '2')

    diagnostics._append_anomaly(path, 'fresh-block\n')

    assert path.read_text(encoding='utf-8') == 'fresh-block\n'
    assert (tmp_path / 'raw_sse_anomaly.log.1').stat().st_size == 1 << 20


def test_concurrent_anomaly_blocks_never_interleave(tmp_path):
    path = tmp_path / 'raw_sse_anomaly.log'

    def append(index):
        diagnostics._append_anomaly(
            path, f'BEGIN-{index}\nbody-{index}\nEND-{index}\n')

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(32)))

    lines = path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 32 * 3
    for offset in range(0, len(lines), 3):
        index = lines[offset].removeprefix('BEGIN-')
        assert lines[offset:offset + 3] == [
            f'BEGIN-{index}', f'body-{index}', f'END-{index}']
