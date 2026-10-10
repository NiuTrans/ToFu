"""Local HTTP peers retain exclusive socket ownership under host port pressure.

LoopbackThreadingHTTPServer uses the production pre-dispatch listener allocator;
HTTP handler behavior and socketserver activation/close lifecycles stay standard.
"""
from http.server import ThreadingHTTPServer
import socket

from tofu_db._loopback import bind_ephemeral_loopback


class LoopbackThreadingHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        if self.server_address != ('127.0.0.1', 0):
            return super().server_bind()
        bind_ephemeral_loopback(self.socket)
        self.server_address = self.socket.getsockname()
        host, self.server_port = self.server_address
        self.server_name = socket.getfqdn(host)
