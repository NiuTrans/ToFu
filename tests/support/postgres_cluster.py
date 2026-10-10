"""Disposable PostgreSQL for real row-lock and semantic authority tests.

Entry point: postgres_cluster. The fixture binds only a private Unix socket,
owns its foreground process and reaps it before removing test files. It uses
no existing service, database, authentication profile or application.
"""

from pathlib import Path
import os
import shutil
import signal
import subprocess
import tempfile
import time

import pytest


class RestartablePostgresDsn(str):
    """A normal DSN with an explicit restart hook for its owning fixture."""
    def __new__(cls, value, restart):
        result = super().__new__(cls, value)
        result.restart = restart
        return result


@pytest.fixture
def postgres_cluster():
    if os.name != "posix" or os.getuid() == 0:
        pytest.skip("Local PostgreSQL tests need an ordinary POSIX user")
    psycopg = pytest.importorskip("psycopg")
    initdb = shutil.which("initdb")
    postgres = shutil.which("postgres")
    if not initdb or not postgres:
        pytest.skip("Install PostgreSQL test binaries to exercise row locks")
    environment = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
    # Unix socket paths have a small platform limit; the checkout's mount
    # prefix is too long. This directory is private and unique to the fixture.
    with tempfile.TemporaryDirectory(prefix="tofu-pg-") as temporary:
        root = Path(temporary)
        data = root / "data"
        socket_dir = root / "socket"
        socket_dir.mkdir(mode=0o700)
        subprocess.run(
            [initdb, "-D", str(data), "--auth=trust", "--no-locale", "--encoding=UTF8"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        dsn = psycopg.conninfo.make_conninfo(
            host=str(socket_dir), dbname="postgres", connect_timeout=1
        )
        with (root / "server.log").open("wb") as log:
            arguments = [
                    postgres,
                    "-D",
                    str(data),
                    "-k",
                    str(socket_dir),
                    "-h",
                    "",
                    "-c",
                    "max_connections=16",
                    "-c",
                    "shared_buffers=16MB",
                ]
            server = None

            def start_server():
                nonlocal server
                server = subprocess.Popen(arguments, env=environment, stdout=log,
                                          stderr=subprocess.STDOUT, start_new_session=True)
                deadline = time.monotonic() + 20
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("Private PostgreSQL exited during startup")
                    try:
                        with psycopg.connect(dsn):
                            return
                    except psycopg.OperationalError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Private PostgreSQL readiness deadline exceeded")
                        time.sleep(0.05)

            def restart_server():
                assert server is not None and server.poll() is None
                server.send_signal(signal.SIGINT)
                server.wait(timeout=10)
                start_server()

            try:
                start_server()
                yield RestartablePostgresDsn(dsn, restart_server)
            finally:
                if server is not None and server.poll() is None:
                    server.send_signal(signal.SIGINT)
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(server.pid, signal.SIGKILL)
                        server.wait(timeout=10)
