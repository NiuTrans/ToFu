"""Bounded HTTP CONNECT bridge for network-aware SSH commands.

Responsibility: open one authenticated HTTP(S)-proxy tunnel and bridge it to
the stdin/stdout contract OpenSSH expects from ``ProxyCommand``.  The proxy URL
is accepted only through the child environment so credentials never appear in
the model-visible command, process argv, route metadata, or logs.

Entry point: ``python -m lib.project_mod.proxy_connect <host> <port>``.
Dependencies: Python sockets/TLS only; route selection remains in
``network_command.py`` and proxy configuration remains in ``lib.proxy``.
"""

from __future__ import annotations

import base64
import os
import re
import socket
import ssl
import sys
import threading
from urllib.parse import unquote, urlsplit


_MAX_CONNECT_RESPONSE = 16 * 1024
_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")


class ProxyConnectError(OSError):
    """A credential-free CONNECT failure with a stable classification."""

    def __init__(self, kind: str, message: str, status_code: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status_code = int(status_code or 0)


def _bounded_timeout(value, default: float = 3.0) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = default
    if not 0 < seconds <= 10:
        seconds = default
    return seconds


def _target_authority(host: str, port: int) -> str:
    clean_host = str(host or "").strip()
    if not _HOST_RE.fullmatch(clean_host) or "\r" in clean_host or "\n" in clean_host:
        raise ProxyConnectError("invalid_target", "invalid target host")
    try:
        clean_port = int(port)
    except (TypeError, ValueError) as error:
        raise ProxyConnectError("invalid_target", "invalid target port") from error
    if not 1 <= clean_port <= 65535:
        raise ProxyConnectError("invalid_target", "invalid target port")
    if ":" in clean_host and not clean_host.startswith("["):
        clean_host = "[" + clean_host + "]"
    return f"{clean_host}:{clean_port}"


def open_http_connect_tunnel(
    proxy_url: str, target_host: str, target_port: int, *, timeout: float = 3.0
) -> tuple[socket.socket, bytes]:
    """Open one HTTP CONNECT tunnel and return ``(socket, prefetched)``.

    ``prefetched`` holds bytes received after the CONNECT response headers
    (occasionally the target's SSH banner arrives in the same packet).
    Callers own and must close the returned socket.
    """
    raw_proxy = str(proxy_url or "").strip()
    try:
        parsed = urlsplit(raw_proxy if "://" in raw_proxy else "http://" + raw_proxy)
        scheme = (parsed.scheme or "").lower()
        proxy_host = parsed.hostname or ""
        proxy_port = parsed.port or (443 if scheme == "https" else 80)
    except (TypeError, ValueError) as error:
        raise ProxyConnectError(
            "proxy_configuration", "proxy address is invalid"
        ) from error
    if scheme not in ("http", "https") or not proxy_host:
        raise ProxyConnectError("proxy_configuration", "proxy must use HTTP or HTTPS")

    authority = _target_authority(target_host, target_port)
    bounded_timeout = _bounded_timeout(timeout)
    stream = socket.create_connection((proxy_host, proxy_port), timeout=bounded_timeout)
    try:
        stream.settimeout(bounded_timeout)
        if scheme == "https":
            context = ssl.create_default_context()
            stream = context.wrap_socket(stream, server_hostname=proxy_host)
            stream.settimeout(bounded_timeout)

        headers = [
            f"CONNECT {authority} HTTP/1.1",
            f"Host: {authority}",
            "Proxy-Connection: Keep-Alive",
        ]
        if parsed.username is not None:
            username = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
            credential = base64.b64encode(
                f"{username}:{password}".encode("utf-8")
            ).decode("ascii")
            headers.append("Proxy-Authorization: Basic " + credential)
        request = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
        stream.sendall(request)

        response = bytearray()
        marker = b"\r\n\r\n"
        while marker not in response:
            chunk = stream.recv(4096)
            if not chunk:
                raise ProxyConnectError(
                    "proxy_connect", "proxy closed CONNECT negotiation"
                )
            response.extend(chunk)
            if len(response) > _MAX_CONNECT_RESPONSE:
                raise ProxyConnectError(
                    "proxy_connect", "proxy CONNECT response was too large"
                )
        header, prefetched = bytes(response).split(marker, 1)
        first_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        match = re.match(r"^HTTP/\d(?:\.\d)?\s+(\d{3})\b", first_line)
        status = int(match.group(1)) if match else 0
        if status != 200:
            kind = (
                "proxy_auth"
                if status == 407
                else "policy_blocked"
                if status in (401, 403)
                else "proxy_connect"
            )
            raise ProxyConnectError(
                kind, f"proxy CONNECT rejected with HTTP {status or '?'}", status
            )
        stream.settimeout(None)
        return stream, prefetched
    except BaseException:
        try:
            stream.close()
        except OSError:
            pass
        raise


def _bridge_stdin_to_socket(stream: socket.socket) -> None:
    try:
        while True:
            chunk = os.read(0, 64 * 1024)
            if not chunk:
                break
            stream.sendall(chunk)
    except (BrokenPipeError, ConnectionError, OSError):
        pass
    try:
        stream.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("proxy connector requires target host and port", file=sys.stderr)
        return 2
    proxy_url = os.environ.get("TOFU_COMMAND_PROXY_URL", "")
    if not proxy_url:
        print("proxy connector route is unavailable", file=sys.stderr)
        return 2
    timeout = _bounded_timeout(
        os.environ.get("TOFU_COMMAND_PROXY_CONNECT_TIMEOUT", "3")
    )
    try:
        stream, prefetched = open_http_connect_tunnel(
            proxy_url, args[0], int(args[1]), timeout=timeout
        )
    except (ProxyConnectError, OSError, ValueError) as error:
        # Never print the proxy URL or exception repr: either may contain
        # credential-bearing connection details.
        kind = getattr(error, "kind", "proxy_connect")
        print(f"proxy connector failed ({kind})", file=sys.stderr)
        return 1

    feeder = threading.Thread(
        target=_bridge_stdin_to_socket,
        args=(stream,),
        name="tofu-proxy-connect-input",
        daemon=True,
    )
    feeder.start()
    try:
        if prefetched:
            os.write(1, prefetched)
        while True:
            chunk = stream.recv(64 * 1024)
            if not chunk:
                break
            os.write(1, chunk)
    except (BrokenPipeError, ConnectionError, OSError):
        return 1
    finally:
        try:
            stream.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
