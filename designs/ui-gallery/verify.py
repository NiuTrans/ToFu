#!/usr/bin/env python3
"""Structural verification for the tofu-ui kit — proof instead of eyeballs.

Asserts, in a real Chromium:
  1. Toolbar alignment: every control in a .ui-toolbar shares one height.
  2. Theme-invariant geometry: key component bounding boxes are identical
     across dark/light/tofu at the same viewport (tokens retune color,
     never geometry).
  3. No horizontal overflow at 320px.
  4. Dialog behavior: opens modal, traps Tab focus inside, closes on
     backdrop click and on Escape with the documented return values.
  5. Field error wiring: .ui-field--error swaps hint/error visibility.
  6. Focus-visible ring: keyboard focus produces the token-driven box-shadow.
  7. Token presence: the 00-design-tokens.css contract resolves per theme.

Usage: python3 designs/ui-gallery/verify.py   (exits non-zero on failure)
"""

import socketserver
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from chromium_env import ensure_chromium_env  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PORT = 18924
BASE = f"http://127.0.0.1:{PORT}/designs/ui-gallery/index.html"

import http.server


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO_ROOT), **kwargs)



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

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def boxes(page, selector: str) -> list[dict]:
    # Zero-size boxes belong to elements inside the closed <dialog>
    # (display:none) — they carry no layout information.
    return page.eval_on_selector_all(
        selector,
        "els => els.map(e => { const r = e.getBoundingClientRect();"
        " return {x: r.x, y: r.y, w: r.width, h: r.height}; })"
        " .filter(b => b.w > 0 && b.h > 0)",
    )


def main() -> None:
    ensure_bundle()
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", PORT), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ensure_chromium_env()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})

            # ── 1. Control height ramp ──────────────────────────────
            # The alignment contract: controls of the SAME size share ONE
            # height (sm=28, md=34), regardless of variant or context.
            page.goto(f"{BASE}?theme=tofu", wait_until="networkidle")
            md_heights = {round(b["h"], 2) for b in boxes(
                page, ".ui-btn:not(.ui-btn--sm):not(.ui-btn--lg), .ui-icon-btn:not(.ui-icon-btn--sm):not(.ui-icon-btn--lg)")}
            sm_heights = {round(b["h"], 2) for b in boxes(page, ".ui-btn--sm, .ui-icon-btn--sm")}
            check("md controls share the md ramp height", md_heights == {34.0},
                  f"heights: {sorted(md_heights)}")
            check("sm controls share the sm ramp height", sm_heights == {28.0},
                  f"heights: {sorted(sm_heights)}")
            input_heights = {round(b["h"], 2) for b in boxes(page, "input.ui-input, .ui-select>select")}
            check("all md inputs/selects share the md control height", input_heights == {34.0},
                  f"heights: {sorted(input_heights)}")

            # ── 2. Theme-invariant control geometry ─────────────────
            # HEIGHTS never shift across themes (token-fixed); widths of
            # text-bearing controls may differ ±1px because the tofu theme
            # intentionally sets a different body typeface — so width
            # invariance is asserted only for fixed-width icon buttons.
            geo: dict[str, dict[str, list]] = {}
            for theme in ("tofu", "dark", "light"):
                page.goto(f"{BASE}?theme={theme}", wait_until="networkidle")
                geo[theme] = {
                    "heights": sorted({round(b["h"], 2) for b in boxes(
                        page, ".ui-btn, .ui-icon-btn, input.ui-input, .ui-select>select")}),
                    "icon_widths": [b["w"] for b in boxes(page, ".ui-icon-btn")],
                }
            check("control heights identical across themes",
                  geo["tofu"]["heights"] == geo["dark"]["heights"] == geo["light"]["heights"],
                  f"tofu={geo['tofu']['heights']} dark={geo['dark']['heights']} light={geo['light']['heights']}")
            check("icon button widths identical across themes",
                  geo["tofu"]["icon_widths"] == geo["dark"]["icon_widths"] == geo["light"]["icon_widths"],
                  f"tofu={geo['tofu']['icon_widths']} dark={geo['dark']['icon_widths']} light={geo['light']['icon_widths']}")

            # ── 3. No 320px horizontal overflow ─────────────────────
            mobile = browser.new_page(viewport={"width": 320, "height": 800})
            for theme in ("tofu", "dark", "light"):
                mobile.goto(f"{BASE}?theme={theme}", wait_until="networkidle")
                overflow = mobile.evaluate("document.documentElement.scrollWidth")
                check(f"320px no horizontal overflow ({theme})", overflow <= 320,
                      f"scrollWidth={overflow}")
            mobile.close()

            # ── 4. Dialog behavior ──────────────────────────────────
            page.goto(f"{BASE}?theme=tofu", wait_until="networkidle")
            page.click("#gal-open-dialog")
            check("dialog opens as modal", page.eval_on_selector(
                "#gal-dialog", "d => d.open && d.matches(':modal')"))
            # focus trap: Tab several times, focus must stay inside dialog
            for _ in range(6):
                page.keyboard.press("Tab")
            inside = page.evaluate(
                "document.getElementById('gal-dialog').contains(document.activeElement)")
            check("Tab focus stays trapped inside dialog", inside,
                  f"activeElement={page.evaluate('document.activeElement && document.activeElement.outerHTML.slice(0,80)')}")
            page.keyboard.press("Escape")
            page.wait_for_function("!document.getElementById('gal-dialog').open")
            check("Escape closes dialog with empty returnValue",
                  page.eval_on_selector("#gal-dialog", "d => d.returnValue === ''"))
            # backdrop click ('close' is a queued task — wait for the
            # observable result, not just the open flag)
            page.click("#gal-open-dialog")
            page.mouse.click(20, 20)  # far outside the centered dialog
            page.wait_for_function(
                "document.getElementById('gal-dialog-result').dataset.result === 'backdrop'")
            check("backdrop click closes dialog", True)
            # action button resolves ('close' is a queued task — wait for the
            # observable result, not just the open flag)
            page.click("#gal-open-dialog")
            page.click("#gal-dialog .ui-btn--danger")
            page.wait_for_function("!document.getElementById('gal-dialog').open")
            page.wait_for_function(
                "document.getElementById('gal-dialog-result').dataset.result === 'delete'")
            check("action button resolves with its value", True)

            # ── 5. Field error wiring ───────────────────────────────
            err = page.locator(".ui-field--error")
            check("error field shows error, hides hint",
                  err.locator(".ui-field__error").is_visible()
                  and not page.locator(".ui-field--error .ui-field__hint").is_visible()
                  if page.locator(".ui-field--error .ui-field__hint").count() else
                  err.locator(".ui-field__error").is_visible())

            # ── 6. Focus-visible ring ───────────────────────────────
            page.click("body", position={"x": 5, "y": 5})  # reset focus
            page.keyboard.press("Tab")
            shadow = page.evaluate(
                "getComputedStyle(document.activeElement).boxShadow")
            check("keyboard focus shows token focus ring",
                  shadow != "none" and "rgba" in shadow, f"boxShadow={shadow}")

            # ── 7. Choice controls ──────────────────────────────────
            page.goto(f"{BASE}?theme=tofu", wait_until="networkidle")
            first_checkbox = page.locator("#gal-checkboxes .ui-choice input").nth(1)
            check("checkbox toggles via label click",
                  page.evaluate(
                      "(el) => { const before = el.checked; el.closest('label').click();"
                      " return el.checked !== before; }",
                      first_checkbox.element_handle()))
            check("switch carries role=switch",
                  page.locator("#gal-switches input[role=switch]").count() == 2)
            radios = page.locator("#gal-radios input[type=radio]")
            radios.nth(1).click()
            check("radio group keeps single selection",
                  page.evaluate("[...document.querySelectorAll('#gal-radios input')]"
                                ".filter(r => r.checked).length === 1"))

            # ── 8. Tabs ─────────────────────────────────────────────
            page.click(".ui-tabs__tab >> nth=0")
            check("click activates first tab",
                  page.locator(".ui-tabs__tab[aria-selected='true']").first.text_content() == "原文")
            page.keyboard.press("ArrowRight")
            check("ArrowRight moves selection and focus",
                  page.evaluate("document.activeElement.getAttribute('aria-selected') === 'true'"
                                " && document.activeElement.textContent === '译文'"))
            check("inactive panel is hidden",
                  page.evaluate("document.querySelectorAll('.ui-tabs__panel')[0].hidden === true"
                                " && document.querySelectorAll('.ui-tabs__panel')[1].hidden === false"))
            page.keyboard.press("ArrowRight")  # wraps past disabled 术语表
            check("arrow navigation skips disabled tab and wraps",
                  page.evaluate("document.activeElement.textContent === '原文'"))

            # ── 9. Dropdown ─────────────────────────────────────────
            check("closed dropdown is visually hidden",
                  not page.locator(".ui-menu").is_visible())
            page.click(".ui-dropdown .ui-btn")
            check("dropdown opens with aria-expanded",
                  page.eval_on_selector(".ui-dropdown .ui-btn",
                                        "b => b.getAttribute('aria-expanded') === 'true'"))
            page.keyboard.press("ArrowDown")  # trigger → first item (重命名)
            page.keyboard.press("ArrowDown")  # menu → second item (归档)
            page.keyboard.press("Enter")
            check("selection hides the dropdown visually",
                  not page.locator(".ui-menu").is_visible())
            selected = page.eval_on_selector("#gal-dropdown-result", "e => e.dataset.select || ''")
            check("keyboard select resolves item id", selected == "archive",
                  f"selected={selected!r}")
            page.click(".ui-dropdown .ui-btn")
            page.keyboard.press("Escape")
            check("Escape closes and refocuses trigger",
                  page.evaluate("document.activeElement === document.querySelector('.ui-dropdown .ui-btn')"
                                " && document.querySelector('.ui-menu').hidden"))
            page.click(".ui-dropdown .ui-btn")
            page.click("h1.gal-title")  # outside
            check("outside click closes menu",
                  page.eval_on_selector(".ui-menu", "m => m.hidden"))

            # ── 10. Toast ───────────────────────────────────────────
            page.click("#gal-toast-triggers .ui-btn >> nth=1")  # success
            check("toast appears in aria-live region",
                  page.locator(".ui-toast-region[aria-live=polite] .ui-toast--success").count() == 1)
            page.evaluate("window.__ui.dismissAllToasts()")
            check("dismissAllToasts detaches region",
                  page.locator(".ui-toast-region").count() == 0)
            page.click("#gal-toast-triggers .ui-btn >> nth=2")
            page.click(".ui-toast .ui-icon-btn")
            check("toast close button dismisses",
                  page.locator(".ui-toast").count() == 0)

            # ── 11. Drawer ──────────────────────────────────────────
            page.click("#gal-open-drawer")
            page.wait_for_timeout(400)  # let the open animation settle before measuring
            check("drawer opens docked to right edge",
                  page.evaluate(
                      "(() => { const d = document.querySelector('.ui-drawer');"
                      " const r = d.getBoundingClientRect();"
                      " return d.open && Math.abs(r.right - innerWidth) < 2"
                      "   && Math.abs(r.height - innerHeight) < 2; })()"))
            page.keyboard.press("Escape")
            page.wait_for_function(
                "document.getElementById('gal-drawer-result').dataset.state === 'closed'")
            check("drawer Escape closes and resolves open()", True)

            # ── 12. Alert / progress / tooltip ──────────────────────
            dismissible = page.locator(".ui-alert--warning")
            dismissible.locator(".ui-alert__dismiss").click()
            check("dismissible alert removes itself",
                  page.locator(".ui-alert--warning").count() == 0)
            page.evaluate(
                "document.querySelector('#gal-progress .ui-progress').style"
                ".setProperty('--ui-progress-value','40%')")
            check("progress bar width follows token var",
                  page.eval_on_selector("#gal-progress .ui-progress__bar",
                                        "b => getComputedStyle(b).width !== '0px'"))
            tip = page.locator(".ui-tooltip-anchor").first
            tip.hover()
            page.wait_for_timeout(600)  # transition-delay 0.3s
            check("tooltip reveals on hover",
                  tip.evaluate("e => getComputedStyle(e, '::after').opacity === '1'"))

            # ── 13. Token contract resolves per theme ───────────────
            for theme in ("tofu", "dark", "light"):
                page.goto(f"{BASE}?theme={theme}", wait_until="networkidle")
                tokens = page.evaluate(
                    """(() => { const cs = getComputedStyle(document.documentElement);
                       return ['--space-4','--font-size-md','--control-h-md','--success-text',
                               '--warning-text','--info-text','--focus-ring','--backdrop',
                               '--z-modal','--motion-base','--font-ui']
                         .map(n => cs.getPropertyValue(n).trim()); })()""")
                check(f"token contract resolves ({theme})", all(tokens),
                      f"values={tokens}")

            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} structural check(s) FAILED:")
        for failure in FAILURES:
            print(f"  - {failure}")
        raise SystemExit(1)
    print("All structural checks passed.")


if __name__ == "__main__":
    main()
