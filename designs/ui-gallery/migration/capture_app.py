#!/usr/bin/env python3
"""App-surface screenshots for the token-sweep migration evidence.

Boots the real Tofu app on an ephemeral port against a DISPOSABLE storage
root (same isolation contract as tests/conftest.py), then screenshots the
welcome surface and the settings modal in all three themes.

Usage:
  python3 designs/ui-gallery/migration/capture_app.py <output-dir>

Run before AND after the token sweep; diff the two directories with
diff_app.py. Value-identical substitutions must produce zero pixel drift
(up to sub-pixel text rasterization noise).
"""

import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

OUT_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
if OUT_DIR is None:
    raise SystemExit("usage: capture_app.py <output-dir>")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Disposable storage authority (mirrors tests/conftest.py) ─────────
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

_tmp = Path(tempfile.mkdtemp(prefix="tofu-shot-storage-"))
os.environ["TOFU_DATA_DIR"] = str(_tmp)
os.environ["TOFU_STORAGE_PROJECT_ROOT"] = str(_tmp)
os.environ["TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE"] = "1"
os.environ.setdefault("TOFU_API_MAJOR", "1")

from chromium_env import ensure_chromium_env  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server() -> tuple[str, threading.Event]:
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    from lib.storage import start_storage
    start_storage()
    import server
    server.app.config.update(TESTING=True)

    port = free_port()
    cfg = Config()
    cfg.bind = [f"127.0.0.1:{port}"]
    cfg.accesslog = None
    cfg.errorlog = None
    shutdown = asyncio.Event()

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(serve(server.app, cfg, shutdown_trigger=shutdown.wait))
        except BaseException as exc:  # surface boot failures on the main thread
            runner.errors.append(exc)
        finally:
            loop.close()

    runner.errors = []
    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    for _ in range(100):
        if runner.errors:
            raise RuntimeError(f"server boot failed: {runner.errors[0]!r}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("server did not start listening")
    return f"http://127.0.0.1:{port}", shutdown


def main() -> None:
    base, shutdown = start_server()
    ensure_chromium_env()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.emulate_media(reduced_motion="reduce")
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_function(
            "window.TofuModules && window.TofuModules.version === 3", timeout=15000)
        page.wait_for_timeout(800)

        for theme in ("tofu", "dark", "light"):
            page.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT_DIR / f"welcome-{theme}.png"))

        # Settings modal (settings.css is the second authored sheet)
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.wait_for_selector("#settingsModal.open", timeout=10000)
        page.wait_for_timeout(500)
        for theme in ("tofu", "dark", "light"):
            page.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT_DIR / f"settings-{theme}.png"))

        browser.close()
    shutdown.set()
    print(f"screenshots -> {OUT_DIR}")


if __name__ == "__main__":
    main()
