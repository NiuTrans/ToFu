"""HTTPX connection-phase evidence from httpcore's public trace extension.

A proxy's CONNECT response is part of connection setup. A timeout there is
replay-safe; a timeout after application headers started is never relabelled.
Caller tracing is forwarded, and the original extension is restored afterward.
"""
import inspect


class ConnectTrace:
    def __init__(self, request):
        self.request = request
        self.original = request.extensions.get('trace')
        self.connecting = False

    def observe(self, name, info):
        if name.endswith('send_request_headers.started'):
            self.connecting = getattr(info.get('request'), 'method', None) == b'CONNECT'

    def sync(self, name, info):
        self.observe(name, info)
        if self.original is not None:
            return self.original(name, info)

    async def asynchronous(self, name, info):
        self.observe(name, info)
        if self.original is not None:
            result = self.original(name, info)
            if inspect.isawaitable(result):
                await result

    def restore(self):
        if self.original is None:
            self.request.extensions.pop('trace', None)
        else:
            self.request.extensions['trace'] = self.original
