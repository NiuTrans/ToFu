#!/usr/bin/env python3
"""Regenerate designs/ui-gallery/previews/*.png and the contrast report.

Serves the repository root over a local HTTP port (relative asset paths in
index.html resolve against it), then screenshots the gallery in every
theme x width combination, plus the open-dialog state. Also verifies the
feedback text tokens clear WCAG 4.5:1 on their theme's --bg-primary.

Usage: python3 designs/ui-gallery/capture.py
Requires: playwright (repo test dependency) + chromium.
"""

import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from chromium_env import ensure_chromium_env  # noqa: E402 — repo-root helper
from playwright.sync_api import sync_playwright  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "previews"
PORT = 18923

THEMES = ["tofu", "dark", "light"]
WIDTHS = [(1440, 900, "desktop"), (320, 800, "mobile")]

# token -> (hex color, hex of theme --bg-primary) — mirrors 00-design-tokens.css
CONTRAST_CASES = {
    "dark": {
        "--bg-primary": "#0a0a0c",
        "--success-text": "#34D399",
        "--warning-text": "#CFB850",
        "--info-text": "#60A5FA",
        "--error-text": "#cf5050",
        "--text-on-accent:on --accent #6e56cf": ("#ffffff", "#6e56cf"),
    },
    "light": {
        "--bg-primary": "#f4f2ed",
        "--success-text": "#1F7A5C",
        "--warning-text": "#96620E",
        "--info-text": "#4068B8",
        "--error-text": "#b82020",
        "--text-on-accent:on --accent #6366f1": ("#ffffff", "#6366f1"),
    },
    "tofu": {
        "--bg-primary": "#F8FAFC",
        "--success-text": "#3D7263",
        "--warning-text": "#8B560F",
        "--info-text": "#3F6E8C",
        "--error-text": "#B05B48",
        "--text-on-accent:on --accent #C97A1D": ("#ffffff", "#C97A1D"),
    },
}


def luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(fg: str, bg: str) -> float:
    l1, l2 = luminance(fg), luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# --text-on-accent cases measure the PRE-EXISTING primary-button paint
# (white on --accent), which predates the design system and is identical to
# every ghost fallback it replaced. Reported as legacy debt (phase-4
# decision: deepen accent or darken label), never as a gate failure.
LEGACY_TOKENS = {"--text-on-accent:on --accent #6e56cf",
                 "--text-on-accent:on --accent #6366f1",
                 "--text-on-accent:on --accent #C97A1D"}


def run_contrast_check() -> list[dict]:
    report = []
    for theme, tokens in CONTRAST_CASES.items():
        bg = tokens["--bg-primary"]
        for name, value in tokens.items():
            if name == "--bg-primary":
                continue
            if isinstance(value, tuple):
                fg, bg_for = value
            else:
                fg, bg_for = value, bg
            ratio = contrast_ratio(fg, bg_for)
            report.append({
                "theme": theme, "token": name,
                "fg": fg, "bg": bg_for,
                "ratio": round(ratio, 2),
                "pass_4_5": ratio >= 4.5,
                "legacy": name in LEGACY_TOKENS,
            })
    return report



def ensure_bundle() -> None:
    """Bundle the real TS factories so the gallery demos what apps import."""
    import subprocess
    bundle = Path(__file__).resolve().parent / "ui-kit.bundle.js"
    src = REPO_ROOT / "frontend/src/ui/index.ts"
    sources = sorted((REPO_ROOT / "frontend/src/ui").glob("*.ts"))
    if (bundle.exists()
            and all(bundle.stat().st_mtime > s.stat().st_mtime for s in sources)):
        return
    subprocess.run(
        [str(REPO_ROOT / "node_modules/.bin/esbuild"), str(src),
         "--bundle", "--format=esm", f"--outfile={bundle}"],
        check=True, capture_output=True,
    )

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO_ROOT), **kwargs)


def main() -> None:
    ensure_bundle()
    OUT_DIR.mkdir(exist_ok=True)

    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", PORT), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        ensure_chromium_env()  # rootless-conda GUI libs + fontconfig
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            for theme in THEMES:
                for width, height, label in WIDTHS:
                    page = browser.new_page(viewport={"width": width, "height": height})
                    page.goto(
                        f"http://127.0.0.1:{PORT}/designs/ui-gallery/index.html?theme={theme}",
                        wait_until="networkidle",
                    )
                    page.screenshot(
                        path=str(OUT_DIR / f"{label}-{theme}.png"),
                        full_page=True,
                    )
                    page.close()
            # dialog open state, desktop tofu + dark
            for theme in ("tofu", "dark"):
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(
                    f"http://127.0.0.1:{PORT}/designs/ui-gallery/index.html?theme={theme}&dialog=open",
                    wait_until="networkidle",
                )
                page.screenshot(path=str(OUT_DIR / f"dialog-{theme}.png"))
                page.close()
            # app scaffold, desktop, all themes
            for theme in THEMES:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(
                    f"http://127.0.0.1:{PORT}/designs/ui-gallery/scaffold.html",
                    wait_until="networkidle",
                )
                page.evaluate(
                    f"document.documentElement.setAttribute('data-theme', '{theme}')")
                page.wait_for_timeout(300)
                page.screenshot(path=str(OUT_DIR / f"scaffold-{theme}.png"), full_page=True)
                page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    report = run_contrast_check()
    (OUT_DIR / "contrast-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    failures = [r for r in report if not r["pass_4_5"] and not r["legacy"]]
    for row in report:
        if row["pass_4_5"]:
            mark = "PASS"
        elif row["legacy"]:
            mark = "WARN(legacy)"
        else:
            mark = "FAIL"
        print(f"[{mark}] {row['theme']:5s} {row['token']:38s} {row['ratio']:5.2f}:1")
    print(f"\nScreenshots -> {OUT_DIR}")
    if failures:
        raise SystemExit(f"{len(failures)} contrast case(s) below 4.5:1")


if __name__ == "__main__":
    main()
