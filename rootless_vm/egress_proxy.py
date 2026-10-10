"""Ephemeral public-Internet proxy for a rootless QEMU guest.

The QEMU user network remains in ``restrict=on`` mode. Its only explicit
guest-forward points here, and this process refuses every non-global address
after host-side DNS resolution. HTTPS remains end-to-end encrypted through
CONNECT; the proxy does not hold a CA key or inspect TLS payloads.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import errno
import fcntl
import hmac
import ipaddress
import json
import os
import selectors
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


_MAX_HEADER_BYTES = 64 * 1024
_CONNECT_TIMEOUT_SEC = 15.0
_RELAY_WRITE_TIMEOUT_SEC = 120.0
_IDLE_TIMEOUT_SEC = 90.0
_MAX_CONNECTIONS = 48
_GLOBAL_CONNECTIONS = 16
_ALLOWED_CONNECT_PORTS = frozenset({443})
_ALLOWED_HTTP_PORTS = frozenset({80})


def _is_global_address(value: str) -> bool:
    """Only globally routable unicast destinations may leave the host."""

    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global and not address.is_multicast


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


def _validated_hostname(host: str) -> str:
    if not host or "\x00" in host:
        raise ValueError("invalid destination host")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid destination hostname") from exc
    lowered = ascii_host.rstrip(".").lower()
    forbidden_suffixes = (".localhost", ".local", ".internal", ".home", ".lan")
    if (
        "." not in lowered
        or lowered == "localhost"
        or lowered.endswith(forbidden_suffixes)
    ):
        raise PermissionError("destination hostname is local or unqualified")
    return lowered


def _safe_addresses_direct(host: str, port: int) -> list[tuple[int, tuple]]:
    ascii_host = _validated_hostname(host)
    answers = socket.getaddrinfo(
        ascii_host,
        port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    if not answers:
        raise ValueError("destination did not resolve")
    resolved = [(family, sockaddr) for family, _, _, _, sockaddr in answers]
    # Reject the whole hostname if DNS returns even one local/private answer.
    # Connecting to a pre-resolved numeric sockaddr below then closes the
    # resolve/check/connect gap used by DNS-rebinding attacks.
    if any(not _is_global_address(str(sockaddr[0])) for _, sockaddr in resolved):
        raise PermissionError("destination resolves to a non-public address")
    return list(dict.fromkeys(resolved))


def _safe_addresses_doh(host: str, port: int, upstream_proxy: str) -> list[tuple[int, tuple]]:
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        ascii_host = _validated_hostname(host)
    else:
        if not _is_global_address(str(literal)):
            raise PermissionError("destination is not a public address")
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        sockaddr = (
            (str(literal), port, 0, 0)
            if literal.version == 6
            else (str(literal), port)
        )
        return [(family, sockaddr)]

    opener = build_opener(ProxyHandler({"https": upstream_proxy}))
    addresses: list[str] = []
    for query_type in ("A", "AAAA"):
        request = Request(
            "https://cloudflare-dns.com/dns-query?name="
            + quote(ascii_host, safe="")
            + "&type="
            + query_type,
            headers={"Accept": "application/dns-json"},
        )
        try:
            with opener.open(request, timeout=_CONNECT_TIMEOUT_SEC) as response:
                payload = json.load(response)
        except Exception:
            # Proxy URLs may contain infrastructure credentials. Never include
            # the underlying urllib exception in guest-visible text or logs.
            continue
        if payload.get("Status") != 0:
            continue
        for answer in payload.get("Answer") or ():
            if answer.get("type") in {1, 28} and isinstance(answer.get("data"), str):
                addresses.append(answer["data"])
    if not addresses:
        raise OSError("public DNS-over-HTTPS resolution failed")
    if any(not _is_global_address(address) for address in addresses):
        raise PermissionError("destination resolves to a non-public address")
    result: list[tuple[int, tuple]] = []
    for address in dict.fromkeys(addresses):
        parsed = ipaddress.ip_address(address)
        if parsed.version == 6:
            result.append((socket.AF_INET6, (address, port, 0, 0)))
        else:
            result.append((socket.AF_INET, (address, port)))
    return result


def _connect_through_upstream(upstream_proxy: str, address: str, port: int) -> socket.socket:
    parsed = urlsplit(upstream_proxy)
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        raise ValueError("configured upstream proxy must be an http URL")
    try:
        proxy_port = parsed.port or 80
    except ValueError as exc:
        raise ValueError("configured upstream proxy port is invalid") from exc
    try:
        connection = socket.create_connection(
            (parsed.hostname, proxy_port), timeout=_CONNECT_TIMEOUT_SEC
        )
        authority = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        request = (
            f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
        ).encode()
        if parsed.username is not None:
            credentials = unquote(parsed.username) + ":" + unquote(parsed.password or "")
            request += b"Proxy-Authorization: Basic " + base64.b64encode(
                credentials.encode()
            ) + b"\r\n"
        connection.sendall(request + b"Connection: keep-alive\r\n\r\n")
        head, extra = _read_head(connection)
        status = head.split(b"\r\n", 1)[0].split(b" ", 2)
        if len(status) < 2 or status[1] != b"200" or extra:
            raise OSError("configured upstream proxy refused the public destination")
        connection.settimeout(None)
        return connection
    except Exception:
        try:
            connection.close()
        except (NameError, OSError):
            pass
        raise OSError("configured upstream proxy connection failed") from None


def _connect_public(
    host: str,
    port: int,
    upstream_proxy: str | None,
) -> socket.socket:
    if not 1 <= port <= 65535:
        raise ValueError("invalid destination port")
    last_error: OSError | None = None
    addresses = (
        _safe_addresses_doh(host, port, upstream_proxy)
        if upstream_proxy
        else _safe_addresses_direct(host, port)
    )
    for family, sockaddr in addresses:
        if upstream_proxy:
            try:
                return _connect_through_upstream(upstream_proxy, str(sockaddr[0]), port)
            except OSError as exc:
                last_error = exc
                continue
        upstream = socket.socket(family, socket.SOCK_STREAM)
        upstream.settimeout(_CONNECT_TIMEOUT_SEC)
        try:
            upstream.connect(sockaddr)
            upstream.settimeout(None)
            return upstream
        except OSError as exc:
            last_error = exc
            upstream.close()
    if upstream_proxy and not _is_ip_literal(host):
        # Corporate proxies commonly refuse CONNECT to numeric destinations.
        # The DoH answers above already validated this name as public, and the
        # tunneled bytes stay bound to it (verified TLS on the upgrade path,
        # guest-terminated TLS on the relay path), so let the upstream proxy
        # resolve the name itself as a last resort.
        try:
            return _connect_through_upstream(upstream_proxy, host, port)
        except OSError as exc:
            last_error = exc
    raise OSError(f"cannot connect to public destination: {last_error}")


def _connect_plain_http(
    host: str,
    port: int,
    upstream_proxy: str | None,
) -> socket.socket:
    """Connect plain proxy requests without delegating DNS to a parent proxy.

    Corporate HTTP proxies commonly reject CONNECT to numeric port 80, while
    sending the hostname to them would reintroduce DNS-rebinding access to
    private hosts. For the standard HTTP port, safely upgrade the host-side hop
    to certificate-verified HTTPS. Direct hosts still use the validated numeric
    destination and requested port unchanged.
    """

    if not upstream_proxy or port != 80:
        return _connect_public(host, port, upstream_proxy)
    last_error: OSError | None = None
    context = ssl.create_default_context()
    candidates = [
        str(sockaddr[0]) for _, sockaddr in _safe_addresses_doh(host, 443, upstream_proxy)
    ]
    if not _is_ip_literal(host):
        # Corporate proxies commonly refuse CONNECT to numeric destinations;
        # the certificate-verified handshake still pins the session to host.
        candidates.append(host)
    for target in candidates:
        raw: socket.socket | None = None
        try:
            raw = _connect_through_upstream(upstream_proxy, target, 443)
            secured = context.wrap_socket(raw, server_hostname=host)
            secured.settimeout(None)
            return secured
        except (OSError, ssl.SSLError):
            last_error = OSError("certificate-verified HTTPS upgrade failed")
            if raw is not None:
                raw.close()
    raise OSError(f"cannot securely upgrade public HTTP destination: {last_error}")


def _read_head(client: socket.socket) -> tuple[bytes, bytes]:
    payload = bytearray()
    while b"\r\n\r\n" not in payload:
        chunk = client.recv(8192)
        if not chunk:
            raise ValueError("connection closed before proxy request headers")
        payload.extend(chunk)
        if len(payload) > _MAX_HEADER_BYTES:
            raise ValueError("proxy request headers exceed 64 KiB")
    head, body = bytes(payload).split(b"\r\n\r\n", 1)
    return head, body


def _send_with_backpressure(target: socket.socket, chunk: bytes) -> None:
    """Bound a relay write without reusing the short connect timeout.

    A TCG guest can temporarily stop draining its forwarded socket while it
    verifies or unpacks a downloaded wheel. Treat that as ordinary transport
    backpressure rather than tearing down an otherwise healthy TLS tunnel.
    """

    target.setblocking(True)
    target.settimeout(_RELAY_WRITE_TIMEOUT_SEC)
    try:
        target.sendall(chunk)
    finally:
        target.settimeout(None)
        target.setblocking(False)


def _headers(lines: list[bytes]) -> list[tuple[bytes, bytes]]:
    parsed: list[tuple[bytes, bytes]] = []
    for line in lines:
        name, separator, value = line.partition(b":")
        if not separator or not name.strip():
            raise ValueError("malformed proxy request header")
        parsed.append((name.strip(), value.strip()))
    return parsed


def _authority(value: str, default_port: int | None = None) -> tuple[str, int]:
    target = urlsplit("//" + value)
    if target.username is not None or target.password is not None:
        raise ValueError("userinfo is not valid in a CONNECT authority")
    host = target.hostname
    if not host:
        raise ValueError("destination host is missing")
    try:
        port = target.port or default_port
    except ValueError as exc:
        raise ValueError("destination port is invalid") from exc
    if port is None:
        raise ValueError("destination port is required")
    return host, port


def _relay(left: socket.socket, right: socket.socket, budget: "_Budget") -> None:
    selector = selectors.DefaultSelector()
    for endpoint in (left, right):
        endpoint.setblocking(False)
        selector.register(endpoint, selectors.EVENT_READ)
    last_activity = time.monotonic()
    try:
        while selector.get_map():
            events = selector.select(timeout=1.0)
            if not events:
                if time.monotonic() - last_activity >= _IDLE_TIMEOUT_SEC:
                    return
                continue
            for key, _ in events:
                source = key.fileobj
                target = right if source is left else left
                try:
                    chunk = source.recv(64 * 1024)
                except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    # A nonblocking TLS socket may need another readiness edge
                    # before OpenSSL can produce plaintext. This is not EOF.
                    continue
                except ConnectionResetError:
                    chunk = b""
                if not chunk:
                    selector.unregister(source)
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                budget.consume(len(chunk))
                _send_with_backpressure(target, chunk)
                last_activity = time.monotonic()
    finally:
        selector.close()


@dataclass
class _Budget:
    remaining: int
    lock: threading.Lock

    def consume(self, amount: int) -> None:
        with self.lock:
            if amount > self.remaining:
                raise PermissionError("session egress byte budget exhausted")
            self.remaining -= amount


def _reply(client: socket.socket, status: int, reason: str) -> None:
    body = f"rootless-vm proxy: {reason}\n".encode()
    client.sendall(
        f"HTTP/1.1 {status} {reason}\r\n".encode()
        + b"Connection: close\r\nContent-Type: text/plain\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


@contextlib.contextmanager
def _global_connection_slot(gate_dir: Path | None, limit: int):
    """Bound upstream tunnels across every VM sharing one private state root."""

    if gate_dir is None:
        yield
        return
    if not 1 <= limit <= 128:
        raise ValueError("global egress concurrency must be between 1 and 128")
    gate_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = gate_dir.lstat()
    if gate_dir.is_symlink() or not gate_dir.is_dir() or info.st_uid != os.getuid():
        raise PermissionError(f"unsafe egress gate directory: {gate_dir}")
    if info.st_mode & 0o077:
        raise PermissionError(
            f"egress gate directory is group/world accessible: {gate_dir}"
        )
    directory_fd = os.open(
        gate_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    acquired: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        offset = (os.getpid() + threading.get_ident()) % limit
        while acquired is None:
            for index in range(limit):
                # Retain one owned lock, not one fd per configured slot per
                # connection. Waiting tunnels must fit the proxy's fd ceiling.
                descriptor = os.open(
                    f"slot-{(offset + index) % limit:03d}.lock",
                    flags, 0o600, dir_fd=directory_fd,
                )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = descriptor
                except BlockingIOError:
                    continue
                finally:
                    if acquired != descriptor:
                        os.close(descriptor)
                break
            if acquired is None:
                time.sleep(0.05)
        yield
    finally:
        try:
            if acquired is not None:
                try:
                    fcntl.flock(acquired, fcntl.LOCK_UN)
                finally:
                    os.close(acquired)
        finally:
            os.close(directory_fd)


def _serve_client(
    client: socket.socket,
    token: str,
    budget: _Budget,
    upstream_proxy: str | None,
    gate_dir: Path | None,
    global_connections: int,
) -> None:
    upstream: socket.socket | None = None
    try:
        client.settimeout(_CONNECT_TIMEOUT_SEC)
        head, initial_body = _read_head(client)
        lines = head.split(b"\r\n")
        try:
            method_raw, target_raw, version = lines[0].split(b" ", 2)
        except ValueError as exc:
            raise ValueError("malformed proxy request line") from exc
        method = method_raw.decode("ascii", errors="strict").upper()
        target_text = target_raw.decode("ascii", errors="strict")
        if version not in {b"HTTP/1.0", b"HTTP/1.1"}:
            raise ValueError("unsupported HTTP version")
        parsed_headers = _headers(lines[1:])
        expected = b"Basic " + base64.b64encode(f"rootless:{token}".encode())
        supplied = next(
            (value for name, value in parsed_headers if name.lower() == b"proxy-authorization"),
            b"",
        )
        if not hmac.compare_digest(supplied, expected):
            _reply(client, 407, "Proxy Authentication Required")
            return

        with _global_connection_slot(gate_dir, global_connections):
            if method == "CONNECT":
                host, port = _authority(target_text)
                if port not in _ALLOWED_CONNECT_PORTS:
                    raise PermissionError("CONNECT destination port is not allowed")
                upstream = _connect_public(host, port, upstream_proxy)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                target = urlsplit(target_text)
                if target.scheme.lower() != "http" or not target.hostname:
                    raise ValueError("plain proxy requests must use an absolute http URL")
                if target.username is not None or target.password is not None:
                    raise ValueError("destination URLs must not contain userinfo")
                try:
                    port = target.port or 80
                except ValueError as exc:
                    raise ValueError("destination port is invalid") from exc
                if port not in _ALLOWED_HTTP_PORTS:
                    raise PermissionError("HTTP destination port is not allowed")
                upstream = _connect_plain_http(target.hostname, port, upstream_proxy)
                path = target.path or "/"
                if target.query:
                    path += "?" + target.query
                forwarded = [
                    (name, value)
                    for name, value in parsed_headers
                    if not name.lower().startswith(b"proxy-")
                ]
                forwarded = [
                    (name, value)
                    for name, value in forwarded
                    if name.lower() != b"connection"
                ]
                request = (
                    method_raw
                    + b" "
                    + path.encode("ascii")
                    + b" "
                    + version
                    + b"\r\n"
                )
                request += b"\r\n".join(
                    name + b": " + value for name, value in forwarded
                )
                request += b"\r\nConnection: close\r\n\r\n" + initial_body
                budget.consume(len(request))
                upstream.sendall(request)
                initial_body = b""
            if initial_body:
                budget.consume(len(initial_body))
                upstream.sendall(initial_body)
            client.settimeout(None)
            _relay(client, upstream, budget)
    except PermissionError as exc:
        try:
            _reply(client, 403, str(exc))
        except OSError:
            pass
    except (OSError, UnicodeError, ValueError) as exc:
        try:
            _reply(client, 502, str(exc))
        except OSError:
            pass
    finally:
        if upstream is not None:
            upstream.close()
        client.close()


def _child_main(listen_fd: int) -> int:
    config_line = sys.stdin.buffer.readline(_MAX_HEADER_BYTES)
    config = json.loads(config_line)
    token = str(config["token"])
    max_bytes = int(config["max_bytes"])
    upstream_proxy_value = config.get("upstream_proxy")
    upstream_proxy = (
        str(upstream_proxy_value) if upstream_proxy_value else None
    )
    gate_dir_value = config.get("gate_dir")
    gate_dir = Path(str(gate_dir_value)) if gate_dir_value else None
    global_connections = int(config.get("global_connections") or _GLOBAL_CONNECTIONS)
    if len(token) < 32 or max_bytes < 1024 * 1024:
        raise ValueError("invalid proxy startup configuration")
    listener = socket.socket(fileno=listen_fd)
    listener.settimeout(0.5)
    budget = _Budget(max_bytes, threading.Lock())
    slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
    stopping = threading.Event()

    def log_line(message: str) -> None:
        # Best-effort: writes can fail (e.g. RLIMIT_FSIZE cap); diagnostics
        # must never take the proxy down.
        with contextlib.suppress(OSError):
            print(message, file=sys.stderr, flush=True)

    def stop_on_parent_close() -> None:
        # Raw fd reads only: a daemon thread parked in a buffered read aborts
        # interpreter shutdown with '_enter_buffered_busy'.
        original_ppid = os.getppid()
        while os.read(sys.stdin.fileno(), 4096):
            pass
        if os.getppid() == original_ppid:
            # Spurious EOF (e.g. an fd-reuse accident in the parent): the
            # owner is alive, so keep serving. A real stop() escalates to
            # SIGTERM after its wait budget; an orphan is reparented and
            # caught by the ppid change below.
            log_line("parent stdin closed but parent alive; continuing")
            return
        log_line("parent exited; stopping")
        stopping.set()

    threading.Thread(target=stop_on_parent_close, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    # An RLIMIT_FSIZE cap delivers SIGXFSZ on write; the default disposition
    # would kill the proxy for a full log file.
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    # Default 8 MiB stacks cap the proxy at ~50 threads under the 512 MiB
    # address-space limit; relay workers need far less.
    threading.stack_size(1024 * 1024)
    log_line(f"egress proxy listening pid={os.getpid()}")
    workers: set[threading.Thread] = set()
    try:
        while not stopping.is_set():
            try:
                client, address = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if exc.errno in (
                    errno.EMFILE,
                    errno.ENFILE,
                    errno.ENOBUFS,
                    errno.ENOMEM,
                ):
                    # Transient resource exhaustion must not kill the proxy;
                    # workers release descriptors as connections drain.
                    log_line(f"transient accept error: {exc!r}")
                    time.sleep(0.1)
                    continue
                log_line(f"accept loop exiting: {exc!r}")
                break
            peer_uid = -1
            try:
                _, peer_uid, _ = struct.unpack(
                    "3i",
                    client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
                )
            except (OSError, struct.error):
                pass
            if peer_uid != os.getuid():
                client.close()
                continue
            # Package managers such as uv open bursts of parallel tunnels.
            # Apply bounded backpressure instead of turning a safe concurrency
            # limit into an opaque EOF that defeats their retry logic.
            if not slots.acquire(timeout=5.0):
                try:
                    _reply(client, 503, "Proxy Busy")
                except OSError:
                    pass
                client.close()
                continue

            def worker(connection: socket.socket) -> None:
                try:
                    _serve_client(
                        connection,
                        token,
                        budget,
                        upstream_proxy,
                        gate_dir,
                        global_connections,
                    )
                finally:
                    slots.release()

            thread = threading.Thread(target=worker, args=(client,), daemon=True)
            try:
                thread.start()
            except RuntimeError:
                # Thread creation can still fail under memory pressure;
                # degrade to bounded backpressure instead of dying.
                slots.release()
                log_line("thread start failed; rejecting connection")
                try:
                    _reply(client, 503, "Proxy Busy")
                except OSError:
                    pass
                client.close()
                time.sleep(0.1)
                continue
            workers.add(thread)
            workers = {item for item in workers if item.is_alive()}
    finally:
        listener.close()
        for worker in workers:
            worker.join(timeout=2.0)
    return 0


class EgressProxy:
    """Lifecycle wrapper for the credential-free proxy subprocess."""

    guest_host = "10.0.2.100"
    guest_port = 3128

    def __init__(
        self,
        *,
        socket_path: Path,
        max_bytes: int = 4 * 1024**3,
        gate_dir: Path | None = None,
        global_connections: int = _GLOBAL_CONNECTIONS,
    ) -> None:
        if max_bytes < 1024 * 1024:
            raise ValueError("egress max_bytes must be at least 1 MiB")
        self.max_bytes = int(max_bytes)
        self.global_connections = int(global_connections)
        if not 1 <= self.global_connections <= 128:
            raise ValueError("global_connections must be between 1 and 128")
        self._upstream_proxy = next(
            (
                os.environ[name]
                for name in (
                    "HTTPS_PROXY",
                    "https_proxy",
                    "HTTP_PROXY",
                    "http_proxy",
                )
                if os.environ.get(name)
            ),
            None,
        )
        self.token = os.urandom(24).hex()
        parent = socket_path.expanduser().parent.resolve(strict=True)
        if parent.stat().st_mode & 0o077:
            raise PermissionError("egress socket parent must be private")
        if gate_dir is not None:
            gate_parent = gate_dir.expanduser().parent.resolve(strict=True)
            gate_info = gate_parent.stat()
            if gate_info.st_uid != os.getuid() or gate_info.st_mode & 0o077:
                raise PermissionError("egress gate parent must be private and owned")
            self.gate_dir = gate_parent / gate_dir.name
        else:
            self.gate_dir = None
        self.socket_path = parent / socket_path.name
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise FileExistsError(f"egress socket path already exists: {self.socket_path}")
        self._parent_fd: int | None = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        # sockaddr_un limits pathnames to roughly 108 bytes. Resolve the private
        # parent through a directory descriptor so arbitrarily deep state roots
        # remain usable; the jailed bridge later connects via /run/egress.sock.
        self.socket_reference = (
            f"/proc/self/fd/{self._parent_fd}/{self.socket_path.name}"
        )
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._listener.bind(self.socket_reference)
        except Exception:
            self._listener.close()
            os.close(self._parent_fd)
            self._parent_fd = None
            raise
        self.socket_path.chmod(0o600)
        self._listener.listen(_MAX_CONNECTIONS)
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def proxy_url(self) -> str:
        return f"http://rootless:{self.token}@{self.guest_host}:{self.guest_port}"

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("egress proxy already started")
        prlimit = Path("/usr/bin/prlimit")
        command = []
        if prlimit.is_file():
            command += [
                str(prlimit),
                "--core=0:0",
                "--nofile=1024:1024",
                # 48 concurrent TLS tunnels (long-lived model streams) plus
                # ssl buffers exceed 512 MiB and workers die on MemoryError.
                f"--as={1024**3}:{1024**3}",
                # Nonzero cap: the stderr log must stay writable, and hitting
                # the cap must not SIGXFSZ-kill the proxy (child ignores it).
                f"--fsize={8 * 1024**2}:{8 * 1024**2}",
                "--",
            ]
        command += [sys.executable, str(Path(__file__).resolve()), "--listen-fd", str(self._listener.fileno())]
        # stderr goes to a log file, not a pipe: a full pipe the parent
        # never drains can wedge the child, and a crash otherwise leaves no
        # evidence behind. Session run dirs are wiped after each trial, so the
        # log lives beside the persistent egress gate instead.
        log_parent = self.gate_dir.parent if self.gate_dir is not None else self.socket_path.parent
        self._log_path = log_parent / f"{self.socket_path.parent.name}-egress.stderr.log"
        self._log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_fd = os.open(
            self._log_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
            0o600,
        )
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log_fd,
                pass_fds=(self._listener.fileno(),),
                start_new_session=True,
                env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
            )
        finally:
            os.close(log_fd)
        assert self.process.stdin is not None
        self.process.stdin.write(
            (
                json.dumps(
                    {
                        "token": self.token,
                        "max_bytes": self.max_bytes,
                        "upstream_proxy": self._upstream_proxy,
                        "gate_dir": str(self.gate_dir) if self.gate_dir else None,
                        "global_connections": self.global_connections,
                    }
                )
                + "\n"
            ).encode()
        )
        self.process.stdin.flush()
        self._listener.close()
        time.sleep(0.05)
        if self.process.poll() is not None:
            stderr = (
                self._log_path.read_text(encoding="utf-8", errors="replace")
                if self._log_path.is_file()
                else ""
            )
            raise RuntimeError(f"egress proxy failed to start: {stderr.strip()}")

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if self.process is None:
            self._listener.close()
            self.socket_path.unlink(missing_ok=True)
            self._close_parent_fd()
            return
        process, self.process = self.process, None
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        if process.stderr:
            process.stderr.close()
        self.socket_path.unlink(missing_ok=True)
        self._close_parent_fd()

    def _close_parent_fd(self) -> None:
        if self._parent_fd is not None:
            os.close(self._parent_fd)
            self._parent_fd = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-fd", type=int, required=True)
    args = parser.parse_args(argv)
    return _child_main(args.listen_fd)


if __name__ == "__main__":
    raise SystemExit(main())
