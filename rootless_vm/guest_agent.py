"""Bounded client for the QEMU Guest Agent virtio-serial protocol."""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


_MAX_RESPONSE_BYTES = 24 * 1024 * 1024
_FILE_CHUNK_BYTES = 32 * 1024


class GuestAgentError(RuntimeError):
    """Raised when an untrusted guest returns an invalid or failed response."""


@dataclass(frozen=True)
class GuestExecResult:
    stdout: bytes
    stderr: bytes
    return_code: int
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class GuestAgent:
    """Synchronous QGA client with timeouts and response-size limits.

    The Unix socket is created by QEMU inside a private session directory. It
    is a message channel, not a host filesystem mount. Responses are treated as
    hostile because every byte originates in the task VM.
    """

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self._lock = threading.Lock()

    @staticmethod
    def _read_response(stream) -> dict:
        raw = stream.readline(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise GuestAgentError("guest-agent response exceeds safety limit")
        if not raw:
            raise GuestAgentError("guest-agent channel closed")
        raw = raw.lstrip(b"\xff")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GuestAgentError("guest-agent returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise GuestAgentError("guest-agent returned a non-object response")
        return value

    def request(
        self,
        command: str,
        arguments: dict[str, object] | None = None,
        *,
        timeout: float = 10.0,
    ) -> object:
        request_id = uuid.uuid4().hex
        sync_value = int.from_bytes(os.urandom(8), "big") & ((1 << 63) - 1)
        deadline = time.monotonic() + timeout
        with self._lock, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(self.socket_path))
            stream = client.makefile("rwb", buffering=0)
            sync_payload = {
                "execute": "guest-sync-delimited",
                "arguments": {"id": sync_value},
                "id": f"sync-{request_id}",
            }
            stream.write(b"\xff" + json.dumps(sync_payload).encode() + b"\n")
            while True:
                client.settimeout(max(0.05, deadline - time.monotonic()))
                response = self._read_response(stream)
                if response.get("return") == sync_value:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out synchronizing guest-agent channel")

            payload: dict[str, object] = {"execute": command, "id": request_id}
            if arguments is not None:
                payload["arguments"] = arguments
            stream.write(json.dumps(payload).encode() + b"\n")
            while True:
                client.settimeout(max(0.05, deadline - time.monotonic()))
                response = self._read_response(stream)
                if response.get("id") != request_id:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for guest-agent {command}")
                    continue
                if "error" in response:
                    raise GuestAgentError(f"guest-agent {command} failed: {response['error']}")
                return response.get("return")

    def wait_ready(self, *, timeout: float = 60.0) -> None:
        # A frame sent before qemu-ga opens its virtio port may be dropped. Use
        # moderately sized attempts: each reconnect sends 0xff plus
        # guest-sync-delimited, which is the protocol's documented stream reset.
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        time.sleep(min(1.0, timeout))
        while time.monotonic() < deadline:
            try:
                remaining = deadline - time.monotonic()
                self.request("guest-ping", timeout=min(10.0, remaining))
                return
            except (OSError, TimeoutError, GuestAgentError) as exc:
                last_error = exc
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        raise TimeoutError(f"guest-agent did not become ready: {last_error}")

    def execute(
        self,
        command: str,
        *,
        timeout: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> GuestExecResult:
        arguments: dict[str, object] = {
            "path": "/bin/sh",
            "arg": ["-lc", command],
            # The boolean alternate is supported by both older agents and the
            # newer enum-capable schema. The enum itself is encoded as a JSON
            # string, not an object, but boolean keeps the wire compatible.
            "capture-output": True,
        }
        if env:
            arguments["env"] = [f"{key}={value}" for key, value in env.items()]
        started = self.request("guest-exec", arguments, timeout=min(timeout, 10.0))
        if not isinstance(started, dict) or not isinstance(started.get("pid"), int):
            raise GuestAgentError(f"invalid guest-exec response: {started!r}")
        pid = started["pid"]
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"guest command timed out after {timeout}s")
            status = self.request(
                "guest-exec-status",
                {"pid": pid},
                timeout=min(5.0, deadline - time.monotonic()),
            )
            if not isinstance(status, dict):
                raise GuestAgentError(f"invalid guest-exec-status response: {status!r}")
            if status.get("exited"):
                try:
                    stdout = base64.b64decode(status.get("out-data", ""), validate=True)
                    stderr = base64.b64decode(status.get("err-data", ""), validate=True)
                    if "exitcode" in status:
                        return_code = int(status["exitcode"])
                    elif "signal" in status:
                        # QGA omits exitcode for signalled processes. Mirror the
                        # conventional shell status so timeout(1) and killed
                        # verifier commands remain ordinary failed results.
                        return_code = 128 + int(status["signal"])
                    else:
                        raise KeyError("exitcode")
                except (KeyError, TypeError, ValueError) as exc:
                    raise GuestAgentError(f"invalid completed exec response: {status!r}") from exc
                return GuestExecResult(
                    stdout=stdout,
                    stderr=stderr,
                    return_code=return_code,
                    stdout_truncated=bool(status.get("out-truncated", False)),
                    stderr_truncated=bool(status.get("err-truncated", False)),
                )
            time.sleep(0.05)

    def upload(self, source: Path, target: str, *, timeout: float = 120.0) -> None:
        source = source.expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"upload source must be a regular file: {source}")
        handle = self.request(
            "guest-file-open", {"path": target, "mode": "w"}, timeout=timeout
        )
        if not isinstance(handle, int):
            raise GuestAgentError(f"invalid guest-file-open response: {handle!r}")
        try:
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(_FILE_CHUNK_BYTES), b""):
                    result = self.request(
                        "guest-file-write",
                        {"handle": handle, "buf-b64": base64.b64encode(chunk).decode()},
                        timeout=timeout,
                    )
                    if not isinstance(result, dict) or result.get("count") != len(chunk):
                        raise GuestAgentError(f"short guest file write: {result!r}")
            self.request("guest-file-flush", {"handle": handle}, timeout=timeout)
        finally:
            self.request("guest-file-close", {"handle": handle}, timeout=timeout)

    def download(
        self,
        source: str,
        target: Path,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        timeout: float = 120.0,
    ) -> None:
        handle = self.request(
            "guest-file-open", {"path": source, "mode": "r"}, timeout=timeout
        )
        if not isinstance(handle, int):
            raise GuestAgentError(f"invalid guest-file-open response: {handle!r}")
        target = target.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            with target.open("xb") as stream:
                while True:
                    result = self.request(
                        "guest-file-read",
                        {"handle": handle, "count": _FILE_CHUNK_BYTES},
                        timeout=timeout,
                    )
                    if not isinstance(result, dict):
                        raise GuestAgentError(f"invalid guest file read: {result!r}")
                    try:
                        chunk = base64.b64decode(result.get("buf-b64", ""), validate=True)
                    except (TypeError, ValueError) as exc:
                        raise GuestAgentError("guest file read returned invalid base64") from exc
                    total += len(chunk)
                    if total > max_bytes:
                        raise GuestAgentError("guest file exceeds download safety limit")
                    stream.write(chunk)
                    if result.get("eof"):
                        break
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            self.request("guest-file-close", {"handle": handle}, timeout=timeout)
