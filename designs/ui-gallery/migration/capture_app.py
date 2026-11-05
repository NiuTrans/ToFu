#!/usr/bin/env python3
"""App-surface screenshots for the token-sweep migration evidence.

Boots the real Tofu app on an ephemeral port against a DISPOSABLE storage
root (designs/ui-gallery/app_harness.py, the same isolation contract as
tests/conftest.py), then screenshots the welcome surface and the settings
modal in all three themes.

Usage:
  python3 designs/ui-gallery/migration/capture_app.py <output-dir>

Run before AND after the token sweep; diff the two directories with
diff_app.py. Value-identical substitutions must produce zero pixel drift
(up to sub-pixel text rasterization noise).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_harness import start_server, use_cjk_fontconfig  # noqa: E402  (isolates env first)

from chromium_env import ensure_chromium_env  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: capture_app.py <output-dir>")
    out_dir = Path(sys.argv[1]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    base, shutdown = start_server()
    ensure_chromium_env()
    use_cjk_fontconfig()
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
            page.screenshot(path=str(out_dir / f"welcome-{theme}.png"))

        # Settings modal (settings.css is the second authored sheet)
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.wait_for_selector("#settingsModal.open", timeout=10000)
        page.wait_for_timeout(500)
        for theme in ("tofu", "dark", "light"):
            page.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
            page.wait_for_timeout(300)
            page.screenshot(path=str(out_dir / f"settings-{theme}.png"))

        browser.close()
    shutdown.set()
    print(f"screenshots -> {out_dir}")


if __name__ == "__main__":
    main()
