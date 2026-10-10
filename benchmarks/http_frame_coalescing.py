"""Private Headless SSE frame coalescing; same JSON, IDs, errors and ownership.

Entry: create_app, streams. Only successful id/data yields are joined. On JSON
failure, the original partial ID is yielded before propagating the same failure.
No production module global or transport implementation is replaced.
"""
import inspect
import json
from types import FunctionType

from tofu_agent import server


def frames(payload, sequence):
    prefix = f'id: {int(sequence)}\n' if sequence is not None else ''
    try:
        data = f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
    except BaseException:
        if prefix:
            yield prefix
        raise
    yield prefix + data


def streams():
    result = {}
    for name, payload in (('_native_task_stream', 'event'), ('_agent_run_stream', 'chunk')):
        function = getattr(server, name)
        source = inspect.getsource(function)
        original = ("            if sequence is not None:\n"
                    "                yield f'id: {int(sequence)}\\n'\n"
                    f"            yield f'data: {{json.dumps({payload}, ensure_ascii=False)}}\\n\\n'")
        assert source.count(original) == 1
        source = source.replace(original,
            f'            for frame in _coalesced_frames({payload}, sequence):\n                yield frame')
        namespace = dict(function.__globals__, _coalesced_frames=frames)
        exec(compile(source, '<headless-coalesced-stream>', 'exec'), namespace)
        result[name] = namespace[name]
    return result


def create_app(*args, **kwargs):
    function = server.create_app
    namespace = dict(function.__globals__, **streams())
    cloned = FunctionType(function.__code__, namespace, argdefs=function.__defaults__)
    cloned.__kwdefaults__ = function.__kwdefaults__
    return cloned(*args, **kwargs)
