"""Host Unix-socket connections under private sandbox directories.

Entry point: connect_unix_socket. Path admission remains the caller's owner
boundary. A short fd-relative address avoids Linux sun_path limits without
moving endpoints to shared directories or changing namespace isolation.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket


def connect_unix_socket(client: socket.socket, path: Path) -> None:
    """Connect while pinning the parent directory; release its fd on every exit."""
    path = Path(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        client.connect(f'/proc/self/fd/{descriptor}/{path.name}')
    finally:
        os.close(descriptor)
