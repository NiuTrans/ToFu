"""Bounded, secret-free probe for operations required by the pathname IPC backend.

A directory being writable is insufficient on remote/FUSE volumes. This probe
uses named files (not unlink-while-open temp files), atomically publishes a
small record, checks advisory locking, and performs a Unix socket round trip.
It never falls back to another volume and cleans only its unique scratch child.
"""
from __future__ import annotations

import fcntl
import os
import socket
import tempfile
from pathlib import Path

from .host_paths import private_temporary_root


def probe_storage(root: Path) -> dict[str, object]:
    scratch = private_temporary_root(owner_root=root)
    report: dict[str, object] = {"root": str(root), "ok": False, "checks": {}}
    checks = report["checks"]
    assert isinstance(checks, dict)
    with tempfile.TemporaryDirectory(prefix="storage-probe-", dir=scratch) as name:
        directory = Path(name)
        try:
            source = directory / "write.partial"
            with source.open("xb") as stream:
                stream.write(b"rootless-storage-v1\n")
                stream.flush()
                os.fsync(stream.fileno())
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            target = directory / "write.complete"
            os.replace(source, target)
            if target.read_bytes() != b"rootless-storage-v1\n":
                raise OSError("storage read-back differs from committed bytes")
            checks["named_files_atomic_replace_flock"] = {"ok": True}
        except OSError as exc:
            checks["named_files_atomic_replace_flock"] = {"ok": False, "errno": exc.errno}
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        # sun_path is ~108 bytes: fd-relative access probes the actual volume
        # without mistaking a long DolphinFS prefix for filesystem incompatibility.
        address = f"/proc/self/fd/{descriptor}/ipc.sock"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.settimeout(1)
                listener.bind(address)
                listener.listen(1)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(1)
                    client.connect(address)
                    peer, _ = listener.accept()
                    with peer:
                        peer.settimeout(1)
                        client.sendall(b"p")
                        if peer.recv(1) != b"p":
                            raise OSError("Unix socket round trip failed")
            checks["pathname_unix_socket"] = {"ok": True}
        except OSError as exc:
            checks["pathname_unix_socket"] = {"ok": False, "errno": exc.errno}
        finally:
            os.close(descriptor)
            (directory / "ipc.sock").unlink(missing_ok=True)
    report["ok"] = all(value["ok"] for value in checks.values())
    return report


def require_storage(root: Path) -> dict[str, object]:
    report = probe_storage(root)
    if not report["ok"]:
        raise ValueError(
            "rootless pathname-IPC storage preflight failed at " + str(root)
            + ": " + str(report["checks"])
            + "; no host /tmp fallback is allowed. Use a filesystem-capable "
              "DolphinFS deployment or a separately validated fd-based IPC backend."
        )
    return report
