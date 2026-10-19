"""Pinned native topology/cursor port over the independent runtime library.

The library is borrowed by application composition. This module owns immutable
code admission and transient graph handles, with joined close. It owns no task
status, model call, callback worker, journal, wallet or backend selection.
"""
from contextlib import contextmanager
import ctypes as c
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import threading

from lib.orchestration.graph import FlowExecutionError, GraphNavigator
from lib.orchestration.native_flow_contract import SPEC

NONE = SPEC['none_index']
_LOOP_ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',',':'))


class _Step(c.Structure):
    _fields_ = [(name,{'u64':c.c_uint64,'u32':c.c_uint32}[kind]) for name,kind in SPEC['step_fields']]


class _Span(c.Structure):
    _fields_ = [(name,{'bytes':c.c_char_p,'usize':c.c_size_t}[kind]) for name,kind in SPEC['span_fields']]


class NativeFlowLibrary:
    """Explicit pinned executable dependency; callers retain its lifecycle."""
    def __init__(self, *, path: Path, sha256: str, maximum_bytes: int):
        if (not isinstance(path, Path) or not path.is_absolute()
                or not isinstance(sha256, str) or len(sha256) != 64
                or any(ch not in '0123456789abcdef' for ch in sha256)
                or type(maximum_bytes) is not int or maximum_bytes <= 0):
            raise FlowExecutionError('native Flow requires a pinned library and explicit capacity')
        self._initialize()
        source = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum_bytes:
                raise FlowExecutionError('native Flow library exceeds its declared capacity')
            from lib.agent_core.native_process import _memfd,_ADD_SEALS,_ALL_SEALS
            descriptor = _memfd()
            self._fd = descriptor
            digest = hashlib.sha256()
            length = 0
            while True:
                chunk = os.read(source, 1 << 20)
                if not chunk:
                    break
                length += len(chunk)
                if length > maximum_bytes:
                    raise FlowExecutionError('native Flow library grew beyond capacity')
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    count = os.write(descriptor, view)
                    if count <= 0:
                        raise FlowExecutionError('native Flow library snapshot failed')
                    view = view[count:]
            if digest.hexdigest() != sha256:
                raise FlowExecutionError('native Flow library digest differs')
            fcntl.fcntl(descriptor, _ADD_SEALS, _ALL_SEALS)
            self._library = c.CDLL(f'/proc/self/fd/{descriptor}')
            self._bind()
            if self._library.tofu_flow_abi_version() != SPEC['abi_version']:
                raise FlowExecutionError('native Flow library ABI differs')
        except BaseException:
            self._release_code()
            raise
        finally:
            os.close(source)
        self.sha256 = sha256

    def _initialize(self):
        self._condition = threading.Condition()
        self._active = 0
        self._closed = False
        self._retiring = False
        self._graphs = set()
        self._fd = None
        self._library = None

    @classmethod
    def from_archive(cls, *, archive: Path, sha256: str, application_version: str, maximum_bytes: int):
        """Admit the Flow extension from the pinned release without extraction."""
        from lib.agent_core.native_artifact import seal_flow_archive
        return cls._from_image(lambda: seal_flow_archive(
            archive,sha256,application_version,maximum_bytes), sha256)

    @classmethod
    def from_shared_archive(cls, *, image):
        """Bind the declared Flow ABI over its own loan of sealed release bytes."""
        from lib.agent_core.native_image import NativeArchiveImage
        if not isinstance(image, NativeArchiveImage):
            raise FlowExecutionError('native Flow requires an owned archive image')
        return cls._from_image(lambda: image.duplicate_library('optional_flow'), image.sha256)

    @classmethod
    def _from_image(cls, acquire_image, sha256):
        result=cls.__new__(cls)
        result._initialize()
        try:
            with acquire_image() as image:
                result._fd=os.dup(image.fileno())
            result._library=c.CDLL(f'/proc/self/fd/{result._fd}')
            result._bind()
            if result._library.tofu_flow_abi_version()!=SPEC['abi_version']:
                raise FlowExecutionError('native Flow library ABI differs')
            result.sha256=sha256
            return result
        except BaseException:
            result._release_code()
            raise

    def _bind(self):
        u = c.c_uint64
        size = c.c_size_t
        types={'u64':u,'u32':c.c_uint32,'usize':size,'bytes':c.c_void_p,
            'u64_out':c.POINTER(u),'u64_array':c.POINTER(u),
            'usize_out':c.POINTER(size),'step_out':c.POINTER(_Step),'span_array':c.POINTER(_Span)}
        for name,arguments in SPEC['signatures'].items():
            method=getattr(self._library,name)
            method.argtypes=[types[kind] for kind in arguments]
            method.restype=c.c_uint32
        for name,arguments in SPEC.get('optional_signatures',{}).items():
            method=getattr(self._library,name,None)
            if method is not None:
                method.argtypes=[types[kind] for kind in arguments]
                method.restype=c.c_uint32
        loop_symbols=SPEC['loop_required_symbols']
        loop_exports=[getattr(self._library,name,None) is not None for name in loop_symbols]
        if (any(loop_exports) or getattr(self._library,'tofu_flow_loop_advance_text',None) is not None) and not all(loop_exports):
            raise FlowExecutionError('native loop capability is incomplete')
        branch_exports=[getattr(self._library,name,None) is not None for name in SPEC['branch_required_symbols']]
        if any(branch_exports) and not all(branch_exports):raise FlowExecutionError('native branch capability is incomplete')
        if getattr(self._library,'tofu_flow_classify_verdict',None) is not None or any(loop_exports) or any(branch_exports):
            import unicodedata
            from lib.orchestration.native_verdict_contract import UNICODE_VERSION
            if unicodedata.unidata_version != UNICODE_VERSION:
                raise FlowExecutionError('native verifier Unicode version differs from host Python')

    @contextmanager
    def _call(self, *, admit_graph=False):
        with self._condition:
            if self._closed or (admit_graph and self._retiring):
                raise FlowExecutionError('native Flow library is closed')
            self._active += 1
        try:
            yield self._library
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    @staticmethod
    def check(status):
        if status == SPEC['statuses']['node_budget']:
            raise FlowExecutionError('walk exceeded node budget — likely an unhandled cycle')
        if status:
            label = next((name for name, value in SPEC['statuses'].items() if value == status), 'unknown')
            raise FlowExecutionError('native Flow refused: ' + label)

    def open_graph(self, *, principal, task_id, nodes, edges):
        owner = principal.require_owner(context='native Flow graph')
        payload = json.dumps(dict(owner_user_id=owner, tenant_id=principal.tenant_id or '',
            task_id=task_id, nodes=[dict(id=name, node_type=node.get('type') or '', kind=node.get('kind') or '')
                for name, node in nodes.items()], edges=[[edge.get('from'), edge.get('to')] for edge in edges]),
            ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        with self._call(admit_graph=True) as library:
            buffer = c.create_string_buffer(payload)
            handle = c.c_uint64()
            self.check(library.tofu_flow_graph_open(buffer, len(payload), c.byref(handle)))
            try:
                graph = NativeFlowGraph(self, handle.value, nodes, edges)
            except BaseException:
                self.check(library.tofu_flow_graph_close(handle.value))
                raise
            with self._condition:
                self._graphs.add(graph)
            return graph

    def _release_code(self):
        if self._library is not None:
            import _ctypes
            _ctypes.dlclose(self._library._handle)
            self._library = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def close(self, *, wait_for_graphs=False):
        with self._condition:
            if self._closed:
                return
            if wait_for_graphs:
                # Application shutdown keeps the serving loop alive while
                # accepted graph owners finish their cancellation/teardown.
                # Existing graph calls must remain usable until they release.
                self._retiring = True
                while self._graphs or self._active:
                    self._condition.wait()
                self._closed = True
                self._release_code()
                return
            if self._graphs:
                raise FlowExecutionError('native Flow library still has graph borrowers')
            self._closed = True
            while self._active:
                self._condition.wait()
            if self._graphs:
                self._closed = False
                raise FlowExecutionError('native Flow graph opened during library close')
            self._release_code()


class NativeFlowGraph(GraphNavigator):
    """Presentation adjacency remains detached; execution queries run in Rust."""
    def __init__(self, runtime, handle, nodes, edges):
        adjacency = GraphNavigator.from_edges(nodes, edges)
        super().__init__(nodes, adjacency.fwd, adjacency.rev)
        self.runtime = runtime
        self.handle = handle
        self._names = tuple(nodes)
        self._indices = {name: index for index, name in enumerate(nodes)}
        self.closed = False

    def index(self, name):
        return self._indices.get(name, NONE)

    def name(self, index):
        return None if index == NONE else self._names[index]

    def query(self, operation, a=None, b=None, avoid=None, values=()):
        if self.closed:
            raise FlowExecutionError('native Flow graph is closed')
        indices = (c.c_uint64 * len(values))(*(self.index(name) for name in values))
        capacity = max(2,len(self._names) + 2) if operation in ('reachable','parallel_plan') else 2
        output = (c.c_uint64 * capacity)()
        written = c.c_size_t()
        with self.runtime._call() as library:
            self.runtime.check(library.tofu_flow_query(self.handle, SPEC['queries'][operation],
                self.index(a), self.index(b), self.index(avoid), indices, len(indices),
                output, len(output), c.byref(written)))
        return tuple(output[:written.value])

    def single_next(self, node_id):
        return None if node_id not in self._indices else self.name(self.query('single_next', node_id)[0])

    def find_start(self):
        return self.name(self.query('find_start')[0])

    def loop_parts(self, loop_id):
        return tuple(self.name(i) for i in self.query('loop_parts', loop_id))

    def find_loop_planner(self, loop_id, body_entry):
        return self.name(self.query('loop_planner', loop_id, body_entry)[0])

    def find_common_barrier(self, branches):
        return self.name(self.query('common_barrier', values=branches)[0])

    def reachable(self, start):
        return {start} if start not in self._indices else {self.name(i) for i in self.query('reachable', start)}

    def can_reach(self, start, target, *, avoid=''):
        if start == target:
            return True
        if start not in self._indices or target not in self._indices:
            return False
        return bool(self.query('can_reach', start, target, avoid=avoid)[0])

    def distance(self, start, target):
        if start == target:
            return 0
        if start not in self._indices or target not in self._indices:
            return 1 << 30
        return self.query('distance', start, target)[0]

    def supports_native_branch(self):
        return all(getattr(self.runtime._library,name,None) is not None for name in SPEC['branch_required_symbols'])

    def branch_plan(self,node_id,enabled):
        output=(c.c_uint64*max(4,len(self._names)+1))();written=c.c_size_t()
        with self.runtime._call() as library:
            function=library.tofu_flow_branch_plan
            status=function(self.handle,self.index(node_id),int(enabled),output,len(output),c.byref(written))
            if status==SPEC['statuses']['buffer_small']:
                output=(c.c_uint64*written.value)()
                status=function(self.handle,self.index(node_id),int(enabled),output,len(output),c.byref(written))
            self.runtime.check(status)
        return [self.name(i) for i in output[1:written.value]],bool(output[0])

    def branch_choose(self,node_id,verdict,labels):
        parts=[str(verdict or '').encode(),*(str(value or '').encode() for value in labels.values())]
        spans=(_Span*len(parts))(*((value,len(value)) for value in parts))
        output=(c.c_uint64*2)();written=c.c_size_t()
        with self.runtime._call() as library:
            self.runtime.check(library.tofu_flow_branch_choose(self.handle,self.index(node_id),spans,len(spans),output,len(output),c.byref(written)))
        return self.name(output[0]),bool(output[1])

    def supports_native_parallel(self):
        return any(getattr(self.runtime._library,name,None) is not None for name in (
            'tofu_flow_parallel_merge_spans','tofu_flow_parallel_merge'))

    def parallel_plan(self,node_id):
        values=self.query('parallel_plan',node_id)
        return [self.name(i) for i in values[2:]],self.name(values[0]),self.name(values[1])

    def parallel_merge(self,node_id,context,outputs):
        if getattr(self.runtime._library,'tofu_flow_parallel_merge_spans',None) is not None:
            parts=[context.encode(),*(value.encode() for value in outputs)]
            spans=(_Span*len(parts))(*((value,len(value)) for value in parts))
            output=c.create_string_buffer(max(1,sum(len(value) for value in parts)+2*len(outputs)))
            written=c.c_size_t()
            with self.runtime._call() as library:
                self.runtime.check(library.tofu_flow_parallel_merge_spans(self.handle,self.index(node_id),
                    spans,len(spans),output,len(output),c.byref(written)))
            return output[:written.value].decode()
        raw=_LOOP_ENCODER.encode(dict(context=context,outputs=outputs)).encode()
        output=c.create_string_buffer(max(1,len(context.encode())+sum(len(v.encode())+2 for v in outputs)))
        written=c.c_size_t()
        with self.runtime._call() as library:
            function=getattr(library,'tofu_flow_parallel_merge',None)
            if function is None:raise FlowExecutionError('native parallel capability absent')
            self.runtime.check(function(self.handle,self.index(node_id),raw,len(raw),output,len(output),c.byref(written)))
        return output[:written.value].decode()

    def supports_native_verdict(self):
        return getattr(self.runtime._library,'tofu_flow_classify_verdict',None) is not None

    def classify_verdict(self,text,*,verifier_role='',loose_fallback=False,strip_feedback=False):
        import sys
        from lib.agent_verdict import replan_enabled
        raw=(text or '').encode('utf-8')
        flags=SPEC['verdict_flags']
        options=(flags['virtual_user'] if verifier_role=='virtual_user' else 0) | (
            flags['loose_fallback'] if loose_fallback else 0) | (
            flags['strip_feedback'] if strip_feedback else 0) | (
            flags['replan_enabled'] if replan_enabled() else 0)
        limit=getattr(sys,'get_int_max_str_digits',lambda:0)()
        buffer=c.create_string_buffer(raw)
        output=c.create_string_buffer(4096);written=c.c_size_t()
        with self.runtime._call() as library:
            function=getattr(library,'tofu_flow_classify_verdict',None)
            if function is None:raise FlowExecutionError('native verdict capability absent')
            status=function(self.handle,buffer,len(raw),options,limit,output,len(output),c.byref(written))
            if status==SPEC['statuses']['buffer_small']:
                output=c.create_string_buffer(written.value)
                status=function(self.handle,buffer,len(raw),options,limit,output,len(output),c.byref(written))
            self.runtime.check(status)
        result=json.loads(output.raw[:written.value])
        from lib.agent_verdict import emit_native_verdict_result
        return emit_native_verdict_result(result)

    def supports_native_loop(self):
        return all(getattr(self.runtime._library,name,None) is not None for name in (
            'tofu_flow_loop_open','tofu_flow_loop_advance','tofu_flow_loop_close'))

    def _loop_frame(self,name,prefix,payload,transport=None):
        raw=_LOOP_ENCODER.encode(payload).encode()
        output=transport.output if transport is not None else c.create_string_buffer(4096)
        written=c.c_size_t()
        with self.runtime._call() as library:
            function=getattr(library,name)
            status=function(self.handle,*prefix,raw,len(raw),output,len(output),c.byref(written))
            if status==SPEC['statuses']['buffer_small']:
                output=c.create_string_buffer(written.value)
                if transport is not None:transport.output=output
                status=function(self.handle,*prefix,raw,len(raw),output,len(output),c.byref(written))
            self.runtime.check(status)
        return json.loads(output[:written.value])

    @contextmanager
    def native_loop(self,node_id,configuration):
        result=self._loop_frame('tofu_flow_loop_open',(self.index(node_id),),configuration)
        loop=NativeFlowLoop(self,result)
        try:yield loop
        except BaseException as error:
            try:loop.close()
            except BaseException as cleanup:error.add_note('native Flow loop cleanup: '+str(cleanup))
            raise
        else:loop.close()

    @contextmanager
    def cursor(self, start, *, stop, budget):
        handle = c.c_uint64()
        with self.runtime._call() as library:
            self.runtime.check(library.tofu_flow_cursor_open(self.handle, self.index(start), self.index(stop),
                budget, c.byref(handle)))
        cursor = NativeFlowCursor(self, handle.value)
        try:
            yield cursor
        except BaseException as error:
            try:cursor.close()
            except BaseException as cleanup:error.add_note('native Flow cursor cleanup: '+str(cleanup))
            raise
        else:
            cursor.close()

    def close(self):
        if self.closed:
            return
        with self.runtime._call() as library:
            self.runtime.check(library.tofu_flow_graph_close(self.handle))
            self.closed = True
            with self.runtime._condition:
                self.runtime._graphs.discard(self)


class NativeFlowLoop:
    """Borrowed graph-scoped decision state; effects remain with the caller."""
    def __init__(self,graph,metadata):
        self.graph=graph;self.handle=metadata['handle'];self.window=metadata['window']
        self.body=graph.name(metadata['body']) if metadata['body'] is not None else None
        self.exit=graph.name(metadata['exit']) if metadata['exit'] is not None else None
        self.planner=graph.name(metadata['planner']) if metadata['planner'] is not None else None
        self.closed=False
        self.native_justification=getattr(graph.runtime._library,'tofu_flow_loop_advance_text',None) is not None
        self._advance_symbol='tofu_flow_loop_advance_text' if self.native_justification else 'tofu_flow_loop_advance'
        self._operation_lock=threading.Lock()
        self._closing=False
        self.output=c.create_string_buffer(4096)
    def advance(self,payload):
        if self.closed or self._closing:raise FlowExecutionError('native Flow refused: unknown_handle')
        with self._operation_lock:
            if self.closed or self._closing:raise FlowExecutionError('native Flow refused: unknown_handle')
            return self.graph._loop_frame(self._advance_symbol,(self.handle,),payload,self)
    def close(self):
        # Withdraw new host calls before waiting for the current frame consumer.
        self._closing=True
        try:
            with self._operation_lock:
                if self.closed:return
                with self.graph.runtime._call() as library:
                    self.graph.runtime.check(library.tofu_flow_loop_close(self.graph.handle,self.handle))
                    self.closed=True
                    self.output=None
        except BaseException:
            self._closing=False
            raise


class NativeFlowCursor:
    def __init__(self, graph, handle):
        self.graph, self.handle, self.closed = graph, handle, False

    def step(self):
        value = _Step()
        with self.graph.runtime._call() as library:
            self.graph.runtime.check(library.tofu_flow_cursor_next(self.graph.handle, self.handle, c.byref(value)))
        return value

    def advance(self, target):
        with self.graph.runtime._call() as library:
            self.graph.runtime.check(library.tofu_flow_cursor_advance(self.graph.handle, self.handle,
                self.graph.index(target)))

    def transition(self,target):
        """One native acknowledgement/next lookup; old libraries keep both calls."""
        value=_Step()
        with self.graph.runtime._call() as library:
            combined=getattr(library,'tofu_flow_cursor_transition',None)
            if combined is not None:
                self.graph.runtime.check(combined(self.graph.handle,self.handle,
                    self.graph.index(target),c.byref(value)))
            else:
                self.graph.runtime.check(library.tofu_flow_cursor_advance(
                    self.graph.handle,self.handle,self.graph.index(target)))
                self.graph.runtime.check(library.tofu_flow_cursor_next(
                    self.graph.handle,self.handle,c.byref(value)))
        return value

    def close(self):
        if not self.closed:
            with self.graph.runtime._call() as library:
                self.graph.runtime.check(library.tofu_flow_cursor_close(self.graph.handle, self.handle))
            self.closed = True


class NativeFlowBinding(GraphNavigator):
    """Defer native allocation until the existing executor's joined run scope."""
    def __init__(self, library, principal, task_id, nodes, edges):
        adjacency = GraphNavigator.from_edges(nodes, edges)
        super().__init__(nodes, adjacency.fwd, adjacency.rev)
        self.library, self.principal, self.task_id = library, principal, task_id
        self.edges = edges
        self.graph = None

    @contextmanager
    def run_scope(self):
        if self.graph is not None:
            raise FlowExecutionError('native Flow executor is already running')
        graph = self.library.open_graph(principal=self.principal, task_id=self.task_id,
            nodes=self.nodes, edges=self.edges)
        self.graph = graph
        try:
            yield
        except BaseException as error:
            try:graph.close()
            except BaseException as cleanup:error.add_note('native Flow graph cleanup: '+str(cleanup))
            raise
        else:
            graph.close()
        finally:
            self.graph = None

    def _active(self):
        if self.graph is None:
            raise FlowExecutionError('native Flow query is outside its run scope')
        return self.graph

    def supports_native_branch(self):return self._active().supports_native_branch()

    def branch_plan(self,node_id,enabled):return self._active().branch_plan(node_id,enabled)

    def branch_choose(self,node_id,verdict,labels):return self._active().branch_choose(node_id,verdict,labels)

    def supports_native_parallel(self):return self._active().supports_native_parallel()

    def parallel_plan(self,node_id):return self._active().parallel_plan(node_id)

    def parallel_merge(self,node_id,context,outputs):return self._active().parallel_merge(node_id,context,outputs)

    def supports_native_loop(self):return self._active().supports_native_loop()

    def native_loop(self,node_id,configuration):return self._active().native_loop(node_id,configuration)

    def single_next(self, node_id):
        return self._active().single_next(node_id)

    def find_start(self):
        return self._active().find_start()

    def loop_parts(self, loop_id):
        return self._active().loop_parts(loop_id)

    def find_loop_planner(self, loop_id, body_entry):
        return self._active().find_loop_planner(loop_id, body_entry)

    def find_common_barrier(self, branches):
        return self._active().find_common_barrier(branches)

    def reachable(self, start):
        return self._active().reachable(start)

    def can_reach(self, start, target, *, avoid=''):
        return self._active().can_reach(start, target, avoid=avoid)

    def distance(self, start, target):
        return self._active().distance(start, target)
