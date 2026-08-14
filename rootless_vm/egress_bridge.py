"""Per-connection byte bridge spawned by QEMU's restricted guestfwd rule."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
from pathlib import Path


def _stdin_to_socket(upstream: socket.socket) -> None:
    try:
        while True:
            chunk = os.read(sys.stdin.fileno(), 64 * 1024)
            if not chunk:
                break
            upstream.sendall(chunk)
    except OSError:
        pass
    try:
        upstream.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.settimeout(10.0)
        upstream.connect(str(args.socket))
    except OSError:
        try:
            upstream.close()
        except UnboundLocalError:
            pass
        return 1
    upstream.settimeout(None)
    sender = threading.Thread(target=_stdin_to_socket, args=(upstream,), daemon=True)
    sender.start()
    try:
        while True:
            chunk = upstream.recv(64 * 1024)
            if not chunk:
                break
            os.write(sys.stdout.fileno(), chunk)
    except OSError:
        return 1
    finally:
        upstream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
