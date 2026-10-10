"""Full JavaScript programs in an isolated WASM instance.

Entry point: execute_javascript_program. Only the supplied search/call/call_many
callbacks reach task capabilities. No WASI filesystem, descriptors or environment
are granted. Each invocation owns its store, promises and receipt provenance.
Heavy Wasmtime loading/compilation happens on first program use, never startup.
"""
from __future__ import annotations

import atexit
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import secrets
import struct
import threading
import time
from typing import Any, Callable

from lib.tools.program_contract import (
    MAX_OUTPUT_BYTES, MAX_SOURCE_BYTES, MAX_TOOL_CALLS, ProgramExecutionError,
    catalog_search_arguments, observation_payload, receipt_value,
)

ROOT = Path(__file__).parent
WASM_SHA256 = 'd4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9'
MAX_SAFE_INTEGER = 2**53 - 1
_compiled = None
_compile_lock = threading.Lock()


def _host_value(value):
    """Carry unsafe JSON integers as decimal strings plus private value paths.

    Paths belong to the transport packet, so user strings/objects cannot be
    mistaken for tagged integers. The guest restores only these values to
    BigInt; receipt and argument codecs restore their exact Python integers.
    """
    integer_paths = []

    path = []

    def visit(item):
        if type(item) is int and abs(item) > MAX_SAFE_INTEGER:
            integer_paths.append(list(path))
            return str(item)
        if isinstance(item, (dict, list, tuple)):
            result = None
            entries = item.items() if isinstance(item, dict) else enumerate(item)
            for key, child in entries:
                path.append(key)
                converted = visit(child)
                path.pop()
                if converted is not child:
                    if result is None:
                        result = dict(item) if isinstance(item, dict) else list(item)
                    result[key] = converted
            return item if result is None else result
        return item

    return visit(value), integer_paths


@dataclass(frozen=True)
class JavaScriptLimits:
    heap_bytes: int = 64 * 1024 * 1024
    # Fuel remains observable and explicitly reducible for deterministic tests.
    # Normal admission uses elapsed guest time, independent of WASM instruction density.
    fuel: int = MAX_SAFE_INTEGER
    execution_seconds: float = 30.0

    def __post_init__(self):
        if type(self.heap_bytes) is not int or not 2 * 1024 * 1024 <= self.heap_bytes <= 1024**3:
            raise ValueError('JavaScript heap must be between 2 MiB and 1 GiB')
        if type(self.fuel) is not int or not 1 <= self.fuel <= 2**63 - 1:
            raise ValueError('JavaScript fuel must be a positive signed 64-bit integer')
        if (type(self.execution_seconds) not in (int, float)
                or not math.isfinite(self.execution_seconds) or self.execution_seconds <= 0):
            raise ValueError('JavaScript execution time must be finite and positive')


def javascript_limits() -> JavaScriptLimits:
    """Resolve one launch-scaled heap; expose the exact limits being enforced."""
    from runtime_guards import resolve_resource_budget
    heap_mib = resolve_resource_budget(
        'TOFU_PROGRAM_HEAP_MAX_MIB', minimum=16, maximum=1024)
    return JavaScriptLimits(heap_bytes=heap_mib * 1024 * 1024)


class _Observation(BaseException):
    def __init__(self, value):
        self.value = value


def _dispose_compiled(module, engine, clock):
    """Release immutable native handles before Python tears down FFI modules."""
    clock.close()
    module.close()
    engine.close()


def _compile():
    global _compiled
    if _compiled is not None:
        return _compiled
    with _compile_lock:
        if _compiled is None:
            try:
                import wasmtime
            except (ImportError, OSError) as exc:
                raise ProgramExecutionError(
                    "runtime_unavailable",
                    "JavaScript runtime could not load Wasmtime: " + str(exc)[:2048] +
                    ". Verify with python -m bootstrap_pkg.runtime_checks using the "
                    "application interpreter. Install a host-compatible Wasmtime build "
                    "or use a supported runtime environment; changing tool arguments "
                    "or retrying the same program will not fix this failure.") from exc
            data = (ROOT / 'assets/quickjs.wasm').read_bytes()
            if hashlib.sha256(data).hexdigest() != WASM_SHA256:
                raise ProgramExecutionError('runtime_integrity', 'JavaScript engine checksum mismatch')
            config = wasmtime.Config()
            config.consume_fuel = True
            config.epoch_interruption = True
            # A single 637 KiB module compiles once. Avoid retaining a global
            # Rayon compiler pool after this optional first-use operation.
            config.parallel_compilation = False
            engine = wasmtime.Engine(config)
            module = wasmtime.Module(engine, data)
            from .execution_clock import ExecutionClock
            clock = ExecutionClock(engine)
            atexit.register(_dispose_compiled, module, engine, clock)
            _compiled = (wasmtime, engine, module, clock)
    return _compiled


class _Program:
    def __init__(self, *, search, call, call_many, aborted, limits, result_reader=None, input_lister=None, value_keeper=None, child_settled=None):
        self.wire_limit_bytes = limits.heap_bytes
        self.search, self.call, self.call_many = search, call, call_many
        self.result_reader = result_reader
        self.result_read_attempts = self.result_reads = 0
        self.input_lister = input_lister
        self.input_queries = 0
        self.value_keeper = value_keeper
        self.child_settled = child_settled
        self.value_keeps = self.value_keep_attempts = 0
        self.aborted = aborted or (lambda: False)
        self.limits = limits
        self.pending = []
        self.pending_count = 0
        self.receipts = {}
        self.next_id = 0
        self.calls = self.stages = self.parallel_stages = 0
        self.successful_calls = 0
        self.phase = 'running'
        self.halt = None
        self.w, self.engine, self.module, self.clock = _compile()
        self.guest_seconds = 0.0
        self.guest_entry_started = None
        self.host_seconds = 0.0
        self.store = self.w.Store(self.engine)
        try:
            self.clock.acquire()
        except BaseException:
            self.store.close()
            raise

    def initialize(self):
        limits = self.limits
        self.store.set_fuel(limits.fuel)
        self.store.set_limits(memory_size=limits.heap_bytes * 2 + 16 * 1024 * 1024,
                              instances=1, memories=1)
        imports = []
        for imported in self.module.imports:
            imports.append(self.w.Func(self.store, imported.type,
                                      self.guard_import(imported.module, imported.name),
                                      access_caller=True))
        self.instance = self.w.Instance(self.store, self.module, imports)
        self.exports = self.instance.exports(self.store)
        self.memory = self.exports['memory']
        self.fn('_initialize')
        # Dynamic JavaScript evaluation remains inside the same bounded WASM
        # instance; it grants no additional host capabilities.
        if self.fn('qjs_init') != 0:
            raise ProgramExecutionError('memory_limit', 'JavaScript initialization failed')
        self.fn('qjs_set_memory_limit', limits.heap_bytes)
        self.fn('qjs_set_interrupt_handler', 1)
        global_value = self.fn('qjs_get_global')
        name = self.alloc('__tofu_rpc')
        host_function = self.fn('qjs_new_host_function', name, len('__tofu_rpc'), 1)
        self.fn('qjs_set_prop_string', global_value, name, host_function)
        self.fn('qjs_free_value', host_function)
        self.fn('qjs_free_value', global_value)
        self.fn('wasm_free', name)
        bridge = self.eval((ROOT / 'bootstrap.js').read_text())
        self.resolver = self.property(bridge, 'resolve')
        self.packer = self.property(bridge, 'pack')
        self.fn('qjs_free_value', bridge)

    def arm_guest_deadline(self):
        spent = self.guest_seconds
        if self.guest_entry_started is not None:
            spent += time.monotonic() - self.guest_entry_started - self.host_seconds
        remaining = self.limits.execution_seconds - spent
        if remaining <= 0:
            raise ProgramExecutionError('execution_limit',
                'JavaScript exceeded its active execution time budget',
                reason='execution_time', limit_seconds=self.limits.execution_seconds)
        # One extra tick avoids firing early due to an entry between clock ticks.
        self.store.set_epoch_deadline(math.ceil(remaining / self.clock.tick_seconds) + 1)

    def fn(self, name, *args, caller=None):
        if caller is None:
            if self.halt is not None:
                raise self.halt
            self.arm_guest_deadline()
            self.guest_entry_started = time.monotonic()
            self.host_seconds = 0.0
        try:
            result = self.exports[name](caller or self.store, *args)
        except Exception:
            if caller is None and self.halt is not None:
                raise self.halt
            raise
        finally:
            if caller is None:
                self.guest_seconds += time.monotonic() - self.guest_entry_started - self.host_seconds
                self.guest_entry_started = None
                self.store.set_epoch_deadline(2**63 - 1)
        if caller is None and self.halt is not None:
            raise self.halt
        return result

    def property(self, value, name):
        pointer = self.alloc(name)
        try:
            return self.fn('qjs_get_prop_string', value, pointer)
        finally:
            self.fn('wasm_free', pointer)

    def invoke_guest(self, function, values):
        pointer = self.fn('wasm_malloc', 4 * len(values))
        if not pointer:
            raise ProgramExecutionError('memory_limit', 'JavaScript argument allocation refused')
        undefined = self.fn('qjs_get_undefined')
        try:
            self.memory.write(self.store, struct.pack('<' + 'I' * len(values), *values), pointer)
            return self.check_exception(self.fn('qjs_call', function, undefined, len(values), pointer))
        finally:
            self.fn('wasm_free', pointer)
            self.fn('qjs_free_value', undefined)

    def javascript_failure(self, message, *, syntax=True):
        if message == 'InternalError: out of memory':
            return ProgramExecutionError('memory_limit', message,
                                         limit_bytes=self.limits.heap_bytes)
        code = 'syntax_error' if syntax and message.startswith('SyntaxError:') else 'javascript_error'
        return ProgramExecutionError(code, message)

    def check_exception(self, result):
        if self.fn('qjs_is_exception', result):
            self.fn('qjs_free_value', result)
            error = self.fn('qjs_get_exception')
            try:
                message = self.string(error)[:2048]
                raise self.javascript_failure(message)
            finally:
                self.fn('qjs_free_value', error)
        return result

    def alloc(self, value: str, caller=None):
        data = value.encode('utf-8')
        pointer = self.fn('wasm_malloc', len(data) + 1, caller=caller)
        if not pointer:
            raise ProgramExecutionError('memory_limit', 'JavaScript memory allocation refused')
        self.memory.write(caller or self.store, data + b'\0', pointer)
        return pointer

    def string(self, value, caller=None):
        context = caller or self.store
        length_pointer = self.fn('wasm_malloc', 4, caller=caller)
        if not length_pointer:
            raise ProgramExecutionError('memory_limit', 'JavaScript string allocation refused')
        try:
            pointer = self.fn('qjs_get_string_len', value, length_pointer, caller=caller)
            if not pointer:
                raise ProgramExecutionError('javascript_error', 'JavaScript string conversion failed')
            try:
                length = struct.unpack('<I', self.memory.read(context, length_pointer, length_pointer + 4))[0]
                if length > self.wire_limit_bytes:
                    raise ProgramExecutionError('output_limit', 'JavaScript transport exceeds byte limit')
                return bytes(self.memory.read(context, pointer, pointer + length)).decode('utf-8')
            finally:
                self.fn('qjs_free_cstring', pointer, caller=caller)
        finally:
            self.fn('wasm_free', length_pointer, caller=caller)

    def new_string(self, value, caller=None):
        pointer = self.alloc(value, caller=caller)
        try:
            return self.fn('qjs_new_string', pointer, len(value.encode('utf-8')), caller=caller)
        finally:
            self.fn('wasm_free', pointer, caller=caller)

    def eval(self, source):
        pointer, filename = self.alloc(source), self.alloc('tool-program.js')
        try:
            result = self.fn('qjs_eval', pointer, len(source.encode('utf-8')), filename, 0)
            return self.check_exception(result)
        finally:
            self.fn('wasm_free', pointer)
            self.fn('wasm_free', filename)

    def decode(self, value, depth=0):
        if depth > 34 or not isinstance(value, dict):
            raise ProgramExecutionError('invalid_output', 'Invalid JavaScript value encoding')
        tag = value.get('t')
        if tag == 'p':
            return value.get('v')
        if tag == 'i':
            try:
                return int(value['v'])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProgramExecutionError(
                    'invalid_output', 'JavaScript integer exceeds the JSON integer boundary') from exc
        if tag == 'a':
            return [self.decode(value['v'][str(i)], depth + 1) for i in range(value['n'])]
        if tag == 'o':
            return {k: self.decode(v, depth + 1) for k, v in value['v'].items()}
        if tag == 'r':
            original = self.receipts.get(value['id'])
            if original is None:
                raise ProgramExecutionError('invalid_output', 'Unknown JavaScript receipt reference')
            decoded = self.decode(value['v'], depth + 1)
            if not isinstance(decoded, dict):
                raise ProgramExecutionError('invalid_output', 'Invalid JavaScript receipt value')
            original.clear()
            original.update(decoded)
            return original
        raise ProgramExecutionError('invalid_output', 'Unknown JavaScript value encoding')

    def guard_import(self, module, name):
        callback = self._import(module, name)

        def guarded(caller, *args):
            # Never let Wasmtime's process-global LAST_EXCEPTION carry a task's
            # exception. Keep the terminal signal on this instance and raise it
            # only after the outer export invocation has returned to Python.
            paused_at = None
            try:
                if name == 'host_call':
                    paused_at = time.monotonic()
                    self.store.set_epoch_deadline(2**63 - 1)
                if self.halt is not None:
                    if name == 'host_call':
                        return self.fn('qjs_get_undefined', caller=caller)
                    return 1
                return callback(caller, *args)
            except BaseException as exc:
                self.halt = exc
                if name == 'host_call':
                    return self.fn('qjs_get_undefined', caller=caller)
                return 1
            finally:
                if paused_at is not None:
                    self.host_seconds += time.monotonic() - paused_at
                    # A host exception stays instance-local, including deadline faults.
                    try:
                        if self.halt is None:
                            self.arm_guest_deadline()
                        else:
                            # Unwind immediately after returning from the callback,
                            # even when guest Promise constructors catch rejections.
                            self.store.set_epoch_deadline(0)
                    except BaseException as exc:
                        if self.halt is None:
                            self.halt = exc
        return guarded

    def _import(self, module, name):
        if module == 'env':
            if name == 'host_call':
                return self.rpc
            if name == 'host_interrupt':
                def interrupt(caller):
                    if self.aborted():
                        self.halt = ProgramExecutionError('cancelled', 'JavaScript program cancelled')
                        return 1
                    return 0
                return interrupt
            if name in {'host_get_timezone_offset', 'host_module_normalize', 'host_module_load'}:
                return lambda caller, *args: 0  # UTC; no module loading capability.
            if name == 'host_promise_rejection':
                def rejection(caller, promise, reason, handled):
                    self.fn('qjs_free_value', promise, caller=caller)
                    self.fn('qjs_free_value', reason, caller=caller)
                return rejection
        if module == 'wasi_snapshot_preview1':
            if name == 'clock_time_get':
                def clock(caller, clock_id, precision, pointer):
                    timestamp = time.time_ns() if clock_id == 0 else time.monotonic_ns()
                    caller['memory'].write(caller, struct.pack('<Q', timestamp), pointer)
                    return 0
                return clock
            if name == 'random_get':
                def random(caller, pointer, length):
                    if not 0 <= length <= MAX_OUTPUT_BYTES:
                        raise ProgramExecutionError('input_limit', 'WASI random request exceeds limit')
                    caller['memory'].write(caller, secrets.token_bytes(length), pointer)
                    return 0
                return random
            if name in {'fd_close', 'fd_fdstat_get', 'fd_seek', 'fd_write'}:
                return lambda caller, *args: 8  # EBADF: there are no inherited descriptors.
        raise ProgramExecutionError('runtime_integrity', f'Unreviewed WASM import {module}.{name}')

    def observe(self, reason, data):
        raise _Observation(observation_payload(reason, data))

    def unwrap(self, receipt):
        return receipt_value(receipt, self.observe)

    def rpc(self, caller, name_pointer, name_length, this, argc, argv):
        name = bytes(caller['memory'].read(caller, name_pointer, name_pointer + name_length))
        if name != b'__tofu_rpc' or argc != 1:
            raise ProgramExecutionError('invalid_call', 'Invalid JavaScript host call')
        if self.aborted():
            raise ProgramExecutionError('cancelled', 'JavaScript program cancelled')
        if self.phase != 'running':
            raise ProgramExecutionError('invalid_call', 'Tool calls during result serialization are refused')
        argument = struct.unpack('<I', caller['memory'].read(caller, argv, argv + 4))[0]
        request = json.loads(self.string(argument, caller=caller))
        args = self.decode(request['args'])
        op = request['op']
        if op == 'observe':
            if len(args) != 2:
                raise ProgramExecutionError('type_error', 'tools.observe expects (reason, data)')
            self.observe(*args)
        elif op == 'value':
            value = self.unwrap(args[0])
        elif op == 'search':
            value = self.search(*catalog_search_arguments(args))
        elif op == 'inputs':
            from lib.tools.program_results import validate_program_input_query
            if len(args) != 1:
                raise ProgramExecutionError('type_error', 'tools.inputs expects one query object')
            if self.input_queries >= MAX_TOOL_CALLS:
                raise ProgramExecutionError('input_query_limit', 'Program input query limit exceeded', limit=MAX_TOOL_CALLS)
            self.input_queries += 1
            query = validate_program_input_query(args[0])
            value = (self.input_lister(query) if self.input_lister is not None
                     else {'inputs': [], 'total': 0, 'nextOffset': None})
        elif op == 'keep':
            if len(args) != 1:
                raise ProgramExecutionError('type_error', 'tools.keep expects one JSON value')
            if self.value_keep_attempts >= MAX_TOOL_CALLS:
                raise ProgramExecutionError('value_keep_limit', 'Program value retention limit exceeded', limit=MAX_TOOL_CALLS)
            self.value_keep_attempts += 1
            value = self.value_keeper(args[0]) if self.value_keeper is not None else None
            if value is None:
                self.observe('program_value_unavailable', {})
            self.value_keeps += 1
        elif op == 'result':
            if len(args) != 2:
                raise ProgramExecutionError('type_error', 'tools.result expects name and arguments')
            if isinstance(args[0], dict):
                from lib.tools.program_results import validate_program_reference_selector
                validate_program_reference_selector(*args)
            if self.result_read_attempts >= MAX_TOOL_CALLS:
                raise ProgramExecutionError('result_read_limit', 'Retained result read limit exceeded', limit=MAX_TOOL_CALLS)
            self.result_read_attempts += 1
            if self.result_reader is None:
                self.observe('retained_result_unavailable', {'tool': args[0]})
            receipt = self.result_reader(*args)
            if (isinstance(receipt, dict) and isinstance(receipt.get('error'), dict)
                    and receipt['error'].get('code') == 'retained_result_unavailable'):
                self.observe('retained_result_unavailable', receipt)
            value = self.unwrap(receipt)
            self.result_reads += 1
        elif op in {'call', 'invoke', 'batch'}:
            if op == 'batch':
                if (len(args) != 2 or not isinstance(args[0], list)
                        or not isinstance(args[1], str)
                        or args[1] not in {'auto', 'sequential', 'parallel'}):
                    raise ProgramExecutionError('type_error', 'tools.callMany expects calls and an execution mode')
                count = len(args[0])
            else:
                if not 1 <= len(args) <= 3:
                    raise ProgramExecutionError('type_error', 'tools.call expects name, arguments, optional call_id')
                count = 1
            if self.calls + self.pending_count + count > MAX_TOOL_CALLS:
                raise ProgramExecutionError('tool_call_limit', 'JavaScript tool call limit exceeded', limit=MAX_TOOL_CALLS)
            self.pending_count += count
            self.next_id += 1
            value = self.next_id
            self.pending.append((value, op, args))
        else:
            raise ProgramExecutionError('invalid_call', 'Unknown JavaScript host operation')
        transported, integer_paths = _host_value(value)
        encoded = json.dumps({'value': transported, 'integers': integer_paths},
                             ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(encoded.encode('utf-8')) > self.wire_limit_bytes:
            raise ProgramExecutionError('output_limit', 'JavaScript host response exceeds byte limit')
        return self.new_string(encoded, caller=caller)

    def resolve(self, request_id, value, batch, receipt=True):
        tokens = []
        for item in (value if batch else [value]) if receipt else []:
            token = secrets.token_hex(24)
            self.receipts[token] = item
            tokens.append(token)
        transported, integer_paths = _host_value(value)
        packet = json.dumps({'value': transported, 'integers': integer_paths,
                             'tokens': tokens, 'batch': batch, 'receipt': receipt},
                            ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(packet.encode('utf-8')) > self.wire_limit_bytes:
            raise ProgramExecutionError('output_limit', 'JavaScript receipt transport exceeds byte limit')
        args = [self.fn('qjs_new_number', float(request_id)), self.new_string(packet)]
        try:
            result = self.invoke_guest(self.resolver, args)
            self.fn('qjs_free_value', result)
        finally:
            for arg in args:
                self.fn('qjs_free_value', arg)

    def admit(self, count, parallel):
        if self.calls + count > MAX_TOOL_CALLS:
            raise ProgramExecutionError('tool_call_limit', 'JavaScript tool call limit exceeded', limit=MAX_TOOL_CALLS)
        self.calls += count
        if count:
            self.stages += 1
            self.parallel_stages += int(parallel)

    def publish_child_settlement(self):
        if self.child_settled is None:
            return
        # Runs between WASM entries, after host receipts/counters settle and
        # before guest continuation. Publication faults cannot replay effects
        # or escape through Wasmtime's global callback exception slot.
        try:
            self.child_settled()
        except ProgramExecutionError:
            raise
        except Exception as exc:
            raise ProgramExecutionError(
                'progress_publication_failed',
                'Unable to publish settled program child progress') from exc

    def dispatch(self):
        requests, self.pending = self.pending, []
        self.pending_count = 0
        while requests:
            request_id, op, args = requests.pop(0)
            if op == 'batch':
                calls, execution = args
                if not isinstance(calls, list) or execution not in {'auto', 'sequential', 'parallel'}:
                    raise ProgramExecutionError('type_error', 'tools.callMany expects calls and an execution mode')
                self.admit(len(calls), execution == 'parallel')
                value = self.call_many(calls, execution) if calls else []
                if not isinstance(value, list) or len(value) != len(calls):
                    raise ProgramExecutionError('invalid_result', 'Tool batch returned incomplete receipts')
                self.successful_calls += sum(isinstance(row, dict) and row.get('status') == 'done'
                                             for row in value)
                if calls:
                    self.publish_child_settlement()
                self.resolve(request_id, value, True)
            else:
                group = [(request_id, op, args)]
                while requests and requests[0][1] in {'call', 'invoke'}:
                    rid, other_op, other = requests.pop(0)
                    group.append((rid, other_op, other))
                calls = []
                for _, _, arguments in group:
                    if not 1 <= len(arguments) <= 3:
                        raise ProgramExecutionError('type_error', 'tools.call expects name, arguments, optional call_id')
                    calls.append({'name': arguments[0],
                                  'arguments': arguments[1] if len(arguments) > 1 else {},
                                  **({'call_id': arguments[2]} if len(arguments) > 2 else {})})
                self.admit(len(calls), len(calls) > 1)
                if len(calls) == 1:
                    arguments = group[0][2]
                    values = [self.call(arguments[0], arguments[1] if len(arguments) > 1 else {},
                                        arguments[2] if len(arguments) > 2 else None)]
                else:
                    values = self.call_many(calls, 'parallel')
                if not isinstance(values, list) or len(values) != len(group):
                    raise ProgramExecutionError('invalid_result', 'Tool batch returned incomplete receipts')
                # All siblings have settled before unwrapping can hand off.
                # Count receipts once at dispatch, never on value/reuse reads.
                self.successful_calls += sum(isinstance(row, dict) and row.get('status') == 'done'
                                             for row in values)
                self.publish_child_settlement()
                for (rid, mode, _), value in zip(group, values):
                    self.resolve(rid, self.unwrap(value) if mode == 'invoke' else value,
                                 False, receipt=mode == 'call')

    def run(self, source):
        outcome = 'completed'
        try:
            promise = self.eval('(async () => {\n' + source + '\n})()')
            while True:
                if self.aborted():
                    raise ProgramExecutionError('cancelled', 'JavaScript program cancelled')
                while self.fn('qjs_is_job_pending'):
                    if self.fn('qjs_execute_pending_job') < 0:
                        raise ProgramExecutionError('javascript_error', 'JavaScript promise job failed')
                state = self.fn('qjs_promise_state', promise)
                if state:
                    if self.pending:
                        raise ProgramExecutionError('unawaited_tools', 'Await every scheduled tool call before returning')
                    value = self.fn('qjs_promise_result', promise)
                    if state == 2:
                        raise self.javascript_failure(self.string(value)[:2048], syntax=False)
                    self.phase = 'serializing'
                    packed = self.invoke_guest(self.packer, [value])
                    encoded = json.loads(self.string(packed))
                    if encoded.get('t') == 'u':
                        self.observe('missing_program_result', {
                            'message': 'The code is an async function body. Write statements directly '
                                       'and return data; call any declared function explicitly. '
                                       'Do not repeat completed tool effects.',
                            'completedToolCalls': self.successful_calls,
                            'toolCallAttempts': self.calls,
                            'retainedReads': self.result_reads,
                        })
                    result = self.decode(encoded)
                    break
                if not self.pending:
                    raise ProgramExecutionError('unresolved_promise', 'Program awaits a promise with no executable work')
                self.dispatch()
        except _Observation as observed:
            outcome, result = 'needs_model', observed.value
            # Progress belongs to the actual host handoff, not to arbitrary
            # returned objects. Keep semantic data unchanged and disclose no
            # private inputs, arguments or sibling return values.
            result = {**result, 'executionReceipt': {
                'attemptedToolCalls': self.calls,
                'successfulToolCalls': self.successful_calls,
                'retainedReads': self.result_reads,
            }}
        return result, self.execution_stats(outcome)

    def execution_stats(self, outcome):
        """Snapshot native work before Store closure, including failed runs."""
        return {'engine': 'quickjs-wasm', 'tool_calls': self.calls,
                        'successful_tool_calls': self.successful_calls,
                        'result_reads': self.result_reads,
                        'result_read_attempts': self.result_read_attempts,
                        'input_queries': self.input_queries,
                        'value_keeps': self.value_keeps,
                        'value_keep_attempts': self.value_keep_attempts,
                        'call_stages': self.stages, 'parallel_stages': self.parallel_stages,
                        'outcome': outcome, 'guest_execution_seconds': self.guest_seconds,
                        'wasm_fuel': self.limits.fuel - self.store.get_fuel(),
                        'wasm_memory_bytes': self.memory.data_len(self.store)}


def execute_javascript_program(source: str, *, search: Callable,
                               call: Callable, call_many: Callable,
                               result_reader: Callable | None = None,
                               input_lister: Callable | None = None,
                               value_keeper: Callable | None = None,
                               child_settled: Callable | None = None,
                               aborted: Callable | None = None,
                               limits: JavaScriptLimits | None = None):
    """Execute one async function body; unwind observations past guest catches."""
    if not isinstance(source, str) or len(source.encode('utf-8')) > MAX_SOURCE_BYTES:
        raise ProgramExecutionError('source_limit', 'JavaScript source exceeds 32 KiB')
    if aborted and aborted():
        raise ProgramExecutionError('cancelled', 'JavaScript program cancelled')
    limits = limits or javascript_limits()
    program = None
    initialized = False
    try:
        program = _Program(search=search, call=call, call_many=call_many,
                           aborted=aborted, limits=limits, result_reader=result_reader,
                           input_lister=input_lister, value_keeper=value_keeper,
                           child_settled=child_settled)
        program.initialize()
        initialized = True
        return program.run(source)
    except Exception as exc:
        error = exc
        if program is not None and isinstance(exc, program.w.Trap):
            if aborted and aborted():
                error = ProgramExecutionError('cancelled', 'JavaScript program cancelled')
            else:
                trap_code = exc.trap_code
                if trap_code == program.w.TrapCode.OUT_OF_FUEL:
                    error = ProgramExecutionError('execution_limit',
                        'JavaScript exhausted its explicit WASM instruction budget',
                        reason='wasm_fuel', limit=limits.fuel)
                elif trap_code == program.w.TrapCode.INTERRUPT:
                    error = ProgramExecutionError('execution_limit',
                        'JavaScript exceeded its active execution time budget',
                        reason='execution_time', limit_seconds=limits.execution_seconds)
                elif trap_code == program.w.TrapCode.STACK_OVERFLOW:
                    error = ProgramExecutionError('stack_limit', 'JavaScript exhausted its WASM stack')
                else:
                    error = ProgramExecutionError('runtime_trap', 'JavaScript engine trapped',
                        trap_code=trap_code.name if trap_code is not None else 'unknown')
        if isinstance(error, ProgramExecutionError) and initialized:
            # Native sidecar only: error details and guest JSON cannot mint stats.
            error.execution_stats = program.execution_stats('error')
        if error is not exc:
            raise error from exc
        raise
    finally:
        if program is not None:
            try:
                program.store.close()
            finally:
                program.clock.release()
