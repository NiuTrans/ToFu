"""Real guest work uses elapsed time; host waits and sibling Stores are isolated."""
from concurrent.futures import ThreadPoolExecutor
import threading
import json
import time

import pytest

from lib.tools.javascript import JavaScriptLimits, execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError
from lib.tools.result_value import CanonicalReceipt

pytestmark = pytest.mark.unit
SELECTOR = '{ref:"input_' + 'a' * 32 + '"}'


def receipt(value):
    return CanonicalReceipt({"status": "done", "output": json.dumps(value),
                             "output_truncated": False}, canonical={"owned": True})


def run(source, **kwargs):
    def unexpected(*args):
        pytest.fail('Execution boundary dispatched an unexpected child effect')

    return execute_javascript_program(source, search=unexpected, call=unexpected,
                                      call_many=unexpected, **kwargs)


def test_large_string_key_projection_exceeds_old_fuel_without_exhausting_memory():
    value, stats = run('const r=tools.result(' + SELECTOR + '); '
                      'return {topKeys:Object.keys(r),items:[]};',
                      result_reader=lambda *args: receipt('a' * 100641))
    assert value == {'topKeys': [str(i) for i in range(100641)], 'items': []}
    assert stats['wasm_fuel'] > 2_000_000_000
    assert stats['wasm_memory_bytes'] < JavaScriptLimits().heap_bytes
    assert stats['guest_execution_seconds'] < JavaScriptLimits().execution_seconds
    assert stats['result_reads'] == 1 and stats['tool_calls'] == 0


@pytest.mark.parametrize('source', [
    'while(true){}; tools.invoke("late");',
    '/(a+)+$/.test("a".repeat(100)+"!"); tools.invoke("late");',
])
def test_elapsed_budget_interrupts_loops_and_builtin_work_and_next_run_recovers(source):
    with pytest.raises(ProgramExecutionError) as error:
        run(source, limits=JavaScriptLimits(execution_seconds=0.03))
    assert error.value.code == 'execution_limit'
    assert error.value.detail == {'reason': 'execution_time', 'limit_seconds': 0.03}
    assert error.value.execution_stats['tool_calls'] == 0
    assert error.value.execution_stats['wasm_fuel'] < JavaScriptLimits().fuel
    assert run('return 7;')[0] == 7


def test_explicit_fuel_exhaustion_has_its_own_reason():
    with pytest.raises(ProgramExecutionError) as error:
        run('while(true){}', limits=JavaScriptLimits(fuel=20_000_000))
    assert error.value.code == 'execution_limit'
    assert error.value.detail == {'reason': 'wasm_fuel', 'limit': 20_000_000}


def test_synchronous_host_wait_is_excluded_from_guest_execution_time():
    def read(*args):
        time.sleep(0.08)
        return receipt({'value': 7})

    value, stats = run('return tools.result(' + SELECTOR + ');',
                       result_reader=read, limits=JavaScriptLimits(execution_seconds=0.05))
    assert value == {'value': 7}
    assert stats['guest_execution_seconds'] < 0.05


def test_one_store_timeout_does_not_interrupt_a_sibling_and_clock_retires():
    before = set(threading.enumerate())
    entered, resume = threading.Event(), threading.Event()

    def read(*args):
        entered.set()
        assert resume.wait(5), 'Sibling guest did not settle'
        return receipt('survived')

    with ThreadPoolExecutor(max_workers=1) as pool:
        sibling = pool.submit(run, 'return tools.result(' + SELECTOR + ');',
                              result_reader=read, limits=JavaScriptLimits(execution_seconds=0.1))
        try:
            assert entered.wait(5), 'Sibling host boundary was not reached'
            with pytest.raises(ProgramExecutionError) as error:
                run('while(true){}', limits=JavaScriptLimits(execution_seconds=0.03))
            assert error.value.detail['reason'] == 'execution_time'
        finally:
            resume.set()
        assert sibling.result(timeout=5)[0] == 'survived'
    assert set(threading.enumerate()) <= before


@pytest.mark.parametrize('seconds', [0, -1, float('inf'), float('nan'), True, '30'])
def test_execution_time_budget_is_finite_and_positive(seconds):
    with pytest.raises(ValueError):
        JavaScriptLimits(execution_seconds=seconds)


def test_nonlimit_engine_trap_is_not_reported_as_memory_or_execution_exhaustion(monkeypatch):
    import lib.tools.javascript as runtime
    import wasmtime

    original = runtime._Program.eval

    def fail(self, source):
        if 'injected_native_fault' in source:
            raise wasmtime.Trap('injected native engine fault')
        return original(self, source)

    monkeypatch.setattr(runtime._Program, 'eval', fail)
    with pytest.raises(ProgramExecutionError) as error:
        run('return "injected_native_fault";')
    assert error.value.code == 'runtime_trap'
    assert error.value.detail == {'trap_code': 'unknown'}
    assert error.value.execution_stats['tool_calls'] == 0
    assert run('return 7;')[0] == 7


def test_real_heap_exhaustion_is_distinct_and_next_run_recovers():
    limits = JavaScriptLimits(heap_bytes=16 * 1024**2)
    with pytest.raises(ProgramExecutionError) as error:
        run('const xs=[];while(true)xs.push(new Uint8Array(1048576));', limits=limits)
    assert error.value.code == 'memory_limit'
    assert error.value.detail == {'limit_bytes': limits.heap_bytes}
    assert error.value.execution_stats['tool_calls'] == 0
    assert run('return 7;')[0] == 7
