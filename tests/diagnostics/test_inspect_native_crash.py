"""Offline native evidence is bounded, explicit, and honest about truncation."""

import struct
import sys

import pytest

from scripts import inspect_native_crash as native

pytestmark = pytest.mark.unit


def _core(path, *, actual_size=1024, segment_size=512):
    ident = b'\x7fELF\x02\x01\x01' + b'\x00' * 9
    header = ident + struct.pack('<HHIQQQIHHHHHH',
        4, 62, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    program = struct.pack('<IIQQQQQQ', 1, 4, 512, 0, 0, segment_size, segment_size, 4096)
    path.write_bytes(header + program + b'\x00' * (actual_size - 120))
    return path


def test_truncated_memory_segments_are_reported_before_debugging(tmp_path):
    core = _core(tmp_path / 'core', segment_size=1024)
    metadata = native.inspect_elf_core(core)
    assert metadata['sizeBytes'] == 1024
    assert metadata['expectedSizeBytes'] == 1536
    assert metadata['complete'] is False
    assert metadata['truncatedSegments'] == 1


def test_complete_core_header_is_reported_without_loading_memory(tmp_path):
    metadata = native.inspect_elf_core(_core(tmp_path / 'core'))
    assert metadata['complete'] is True
    assert metadata['truncatedSegments'] == 0


def test_invalid_core_and_incomplete_headers_fail_closed(tmp_path):
    core = tmp_path / 'core'
    core.write_bytes(b'not-an-elf-core')
    with pytest.raises(ValueError, match='ELF header'):
        native.inspect_elf_core(core)
    _core(core)
    core.write_bytes(core.read_bytes()[:80])
    with pytest.raises(ValueError, match='program headers'):
        native.inspect_elf_core(core)


def test_debugger_output_budget_stops_only_owned_process():
    result = native.capture_debugger([
        sys.executable, '-c', 'import os,time; os.write(1,b"x"*65536); time.sleep(30)'],
        max_bytes=1000, timeout=3)
    assert result['limitReason'] == 'output_budget'
    assert len(result['output']) == 1000
    assert result['returnCode'] is not None


def test_debugger_timeout_closes_owned_pipe_and_reaps_child():
    result = native.capture_debugger([
        sys.executable, '-c', 'import time; time.sleep(30)'], timeout=0.1)
    assert result['limitReason'] == 'timeout'
    assert result['returnCode'] is not None


def test_report_retains_signal_and_never_claims_binary_identity(monkeypatch, tmp_path):
    core = _core(tmp_path / 'core', segment_size=1024)
    executable = tmp_path / 'python'
    executable.write_bytes(b'fixture executable')
    def capture(command):
        assert '--core' in command and str(core) in command
        assert not any('attach' in argument for argument in command)
        return {'returnCode': 0, 'limitReason': '', 'output': (
            'NATIVE_SIGNAL=11\nNATIVE_SIGNAL_CODE=1\nNATIVE_FAULT_ADDRESS=0x8a\n'
            '#0 dump_traceback ()\n')}
    monkeypatch.setattr(native, 'capture_debugger', capture)
    report = native.inspect_native_crash(core, executable, gdb='fixture-gdb')
    assert report['signal'] == 11
    assert report['signalCode'] == 1
    assert report['faultAddress'] == '0x8a'
    assert report['executableMatch'] == 'not_proven'
    assert report['warnings']


def test_missing_debugger_preserves_core_completeness_evidence(monkeypatch, tmp_path):
    core = _core(tmp_path / 'core')
    monkeypatch.setattr(native.shutil, 'which', lambda _name: None)
    report = native.inspect_native_crash(core, core)
    assert report['core']['complete'] is True
    assert 'signal' not in report
    assert report['warnings']


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux pipe core policy')
def test_debugger_inherits_pipe_safe_core_limit_without_changing_parent():
    import resource
    original = resource.getrlimit(resource.RLIMIT_CORE)
    result = native.capture_debugger([
        sys.executable, '-c',
        'import resource; print(resource.getrlimit(resource.RLIMIT_CORE))'])
    assert result['returnCode'] == 0
    assert result['output'].strip() == '(1, 1)'
    assert resource.getrlimit(resource.RLIMIT_CORE) == original


@pytest.mark.parametrize('signal_code', [0, -6])
def test_user_delivered_signal_does_not_interpret_sender_union_as_fault_address(
        monkeypatch, tmp_path, signal_code):
    core = _core(tmp_path / 'core')
    monkeypatch.setattr(native, 'capture_debugger', lambda _command: {
        'returnCode': 0, 'limitReason': '', 'output': (
            f'NATIVE_SIGNAL=7\nNATIVE_SIGNAL_CODE={signal_code}\n'
            'NATIVE_FAULT_ADDRESS=0x123456789\n')})
    report = native.inspect_native_crash(core, core, gdb='fixture-gdb')
    assert report['signal'] == 7
    assert report['signalCode'] == signal_code
    assert 'faultAddress' not in report
