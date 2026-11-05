"""Disposable live Tofu app for design captures (gallery evidence and audits).

Importing this module isolates the process BEFORE any application import:
inherited lifecycle/supervisor variables are dropped and storage points at a
fresh temporary root — the same isolation contract as tests/conftest.py — so
a capture never reads or writes a real deployment. ``start_server`` then
serves ``server.app`` on an ephemeral loopback port from a daemon thread.

The storage backend is inherited. When the deployment's native engine was not
built from this checkout (typical in a git worktree), boot fails with the
engine-identity refusal; run the SQLite lane with
``TOFU_PERSONAL_STORAGE_BACKEND=sqlite``, the remedy the test suite names.

Consumers: migration/capture_app.py, settings_audit.py.
"""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_INHERITED_LIFECYCLE_ENV_NAMES = (
    "TOFU_RESTART_GATE_PASSED", "TOFU_LIFECYCLE_GATE_PASSED",
    "TOFU_ALLOW_LIFECYCLE_TEST", "TOFU_LIFECYCLE_TEST_ROOT",
    "TOFU_LIFECYCLE_TEST_PORT", "TOFU_LIFECYCLE_TEST_TARGET_PID",
    "TOFU_TESTING", "TOFU_EXTERNAL_CONSOLE_LOG", "TOFU_EXTERNAL_CONSOLE_STREAM",
    "TOFU_STORAGE_CONNECTION_FILE", "TOFU_STORAGE_TOKEN",
    "TOFU_STORAGE_PARENT_PID", "TOFU_RESOURCE_BUDGET_POLICY_VERSION",
    "TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS", "TOFU_SUPERVISOR_CONF",
    "TOFU_SUPERVISOR_CONF_DIR", "TOFU_SUPERVISOR_HOME", "TOFU_SUPERVISOR_HOST",
    "TOFU_SUPERVISOR_PORT", "TOFU_SUPERVISOR_PROJECTS", "TOFU_SUPERVISOR_PYTHON",
    "TOFU_SUPERVISOR_USER", "TOFU_HEARTBEAT_DIR", "TOFU_PYTEST_RUN_ROOT",
)
for _name in _INHERITED_LIFECYCLE_ENV_NAMES:
    os.environ.pop(_name, None)

STORAGE_ROOT = Path(tempfile.mkdtemp(prefix="tofu-shot-storage-"))
os.environ["TOFU_DATA_DIR"] = str(STORAGE_ROOT)
os.environ["TOFU_STORAGE_PROJECT_ROOT"] = str(STORAGE_ROOT)
os.environ["TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE"] = "1"
os.environ.setdefault("TOFU_API_MAJOR", "1")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server() -> tuple[str, asyncio.Event]:
    """Serve the app on loopback; return its base URL and shutdown event."""
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    from lib.storage import start_storage
    start_storage()
    import server
    server.app.config.update(TESTING=True)

    port = _free_port()
    cfg = Config()
    cfg.bind = [f"127.0.0.1:{port}"]
    cfg.accesslog = None
    cfg.errorlog = None
    shutdown = asyncio.Event()
    errors: list[BaseException] = []

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(serve(server.app, cfg, shutdown_trigger=shutdown.wait))
        except BaseException as exc:  # surface boot failures on the main thread
            errors.append(exc)
        finally:
            loop.close()

    threading.Thread(target=runner, daemon=True).start()
    for _ in range(100):
        if errors:
            raise RuntimeError(f"server boot failed: {errors[0]!r}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("server did not start listening")
    return f"http://127.0.0.1:{port}", shutdown


def use_cjk_fontconfig() -> str | None:
    """Point headless Chromium at a fontconfig that can draw CJK text.

    chromium_env keeps a host's own /etc/fonts, which may carry no CJK face:
    the Chinese UI then screenshots as rows of tofu boxes. An interpreter
    prefix (conda) fonts.conf that lists CJK faces is exported instead. An
    explicit FONTCONFIG_FILE always wins; returns the config chosen, if any.
    """
    if os.environ.get("FONTCONFIG_FILE") or not shutil.which("fc-list"):
        return None
    prefixes = (sys.prefix, sys.base_prefix, os.environ.get("CONDA_PREFIX", ""))
    for prefix in dict.fromkeys(p for p in prefixes if p):
        conf = Path(prefix, "etc", "fonts", "fonts.conf")
        if not conf.is_file():
            continue
        env = {**os.environ, "FONTCONFIG_FILE": str(conf), "FONTCONFIG_PATH": str(conf.parent)}
        listed = subprocess.run(["fc-list", ":lang=zh", "family"], env=env,
                                capture_output=True, text=True, check=False)
        if listed.returncode == 0 and listed.stdout.strip():
            os.environ["FONTCONFIG_FILE"] = env["FONTCONFIG_FILE"]
            os.environ["FONTCONFIG_PATH"] = env["FONTCONFIG_PATH"]
            return str(conf)
    return None
