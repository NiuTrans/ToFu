#!/usr/bin/env python3
"""Uniform Settings audit: screenshot every tab and inventory its copy.

Reviewing Settings one tab at a time hides the patterns that make it feel
off: a tab that is mostly blank, everyday switches folded into a disclosure,
one sentence repeated as description, hint and empty state, explainer
diagrams that control nothing. This tool puts every tab on the same footing.
It boots the real app against disposable storage (app_harness.py), opens
Settings and, for each tab in the navigation, writes

  <out>/<theme>/<NN>-<tab>.png        the tab as first opened (1440x900)
  <out>/<theme>/<NN>-<tab>-full.png   the whole tab, every <details> open
  <out>/report.md, report.json        per-tab inventory: visible controls,
                                      collapsed sections, first-screen fill
                                      and every explanatory text block

It measures and does not judge: read the report beside the screenshots with
the review checklist in designs/ui-gallery/README.md.

Usage:
  python3 designs/ui-gallery/settings_audit.py <out-dir>
      [--theme tofu|dark|light ...] [--lang zh|en] [--tab general ...]
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_harness import start_server, use_cjk_fontconfig  # noqa: E402  (isolates env first)

from chromium_env import ensure_chromium_env  # noqa: E402

VIEWPORT = {"width": 1440, "height": 900}
# Liveness bound, not a pacing knob: a tab still loading at the deadline is
# captured and reported as "still loading" instead of hanging the audit.
TAB_READY_DEADLINE_S = 20
# A tab counts as settled once the requests it started have finished and its
# loading indicators are gone for this long (async paints after the reply).
TAB_SETTLE_S = 0.4
REPORT_BLOCKS_PER_TAB = 15  # report.md readability; report.json keeps all

# Transient toasts are not part of any tab; the expansion rule lets the
# modal grow to the content so one screenshot shows the whole tab.
CAPTURE_CSS = "#toastContainer, .ui-toast-region { display: none !important; }"
EXPAND_CSS = ("#settingsModal .settings-panel { max-height: none !important; height: auto !important; }"
              " #settingsModal .settings-content { max-height: none !important; overflow: visible !important; }")

INVENTORY_JS = r"""
(tab) => {
  const panel = document.getElementById('settingsTab_' + tab);
  if (!panel) return null;
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    if (el.closest('details:not([open]) > :not(summary)')) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const controls = [...panel.querySelectorAll(
    'input:not([type=hidden]), select, textarea, button, a[href]')]
    .filter((el) => visible(el.matches('input[type=checkbox], input[type=radio]')
      ? (el.closest('label') || el) : el));
  const LABEL = 'label, summary, button, option, select, th, h1, h2, h3, h4, '
    + '.settings-section-title, .settings-toggle-label, .settings-field-label';
  // Each text node belongs to its nearest block box, so a card's title and
  // its description stay separate blocks instead of being counted twice.
  const buckets = new Map();
  const walker = document.createTreeWalker(panel, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const parent = walker.currentNode.parentElement;
    const text = walker.currentNode.textContent.replace(/\s+/g, ' ').trim();
    if (!text || !visible(parent)) continue;
    let block = parent;
    while (block !== panel && getComputedStyle(block).display.startsWith('inline')) {
      block = block.parentElement;
    }
    const entry = buckets.get(block) || { parts: [], label: true };
    entry.parts.push(text);
    entry.label = entry.label && !!parent.closest(LABEL);
    buckets.set(block, entry);
  }
  const blocks = [...buckets].map(([block, entry]) => {
    const text = entry.parts.join(' ');
    return {
      text, chars: text.length, label: entry.label,
      cls: typeof block.className === 'string' && block.className
        ? block.className : block.tagName.toLowerCase(),
    };
  });
  const content = document.querySelector('#settingsModal .settings-content');
  return {
    controls: controls.length,
    collapsed: [...panel.querySelectorAll('details:not([open]) > summary')]
      .filter(visible).map((summary) => summary.innerText.replace(/\s+/g, ' ').trim()),
    fill: Math.round(100 * panel.scrollHeight / Math.max(1, content.clientHeight)),
    loading: [...panel.querySelectorAll('.stg-loading')].some(visible),
    blocks,
  };
}
"""

READY_JS = r"""
(tab) => {
  const panel = document.getElementById('settingsTab_' + tab);
  if (!panel || !panel.classList.contains('active')) return false;
  return ![...panel.querySelectorAll('.stg-loading, [aria-busy="true"]')]
    .some((el) => el.offsetParent !== null);
}
"""


class RequestTracker:
    """In-flight fetch/XHR requests, so a tab is captured after its own reads."""

    def __init__(self, page):
        self.pending = set()
        page.on("request", self._started)
        page.on("requestfinished", self._ended)
        page.on("requestfailed", self._ended)

    def _started(self, request):
        if request.resource_type in ("fetch", "xhr"):
            self.pending.add(request)

    def _ended(self, request):
        self.pending.discard(request)

    def settle(self, page, tab, before):
        """Wait for requests started since ``before`` and the tab's loaders."""
        deadline = time.monotonic() + TAB_READY_DEADLINE_S
        quiet_since = None
        while time.monotonic() < deadline:
            page.wait_for_timeout(100)
            if (self.pending - before) or not page.evaluate(READY_JS, tab):
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= TAB_SETTLE_S:
                return True
        return False


def invoke(page, name, *args):
    """Drive the same v3 command port as production data-tofu-action seams."""
    return page.evaluate(
        """async ({name, args}) => {
          const modules = window.TofuModules;
          if (modules.canInvokeFeature(name)) return await modules.invokeFeature(name, args, () => {});
          return await modules.resolveAction(name)(...args);
        }""", {"name": name, "args": list(args)})


def open_settings(page, base, lang):
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_function(
        "window.TofuModules?.version === 3"
        " && typeof window.TofuModules.resolveAction?.('_obClose') === 'function'",
        timeout=30000)
    page.evaluate("() => window.TofuModules.resolveAction('_obClose')()")
    page.add_style_tag(content=CAPTURE_CSS)
    if lang != "zh":
        invoke(page, "_onLanguageChange", lang)
    invoke(page, "openSettings")
    page.wait_for_selector("#settingsModal.open", timeout=15000)
    return page.evaluate(
        """() => [...document.querySelectorAll('#settingsModal .settings-tab[data-tab]')]
             .filter((button) => button.offsetParent !== null)
             .map((button) => [button.dataset.tab, button.innerText.trim()])""")


def capture_tab(page, requests, tab, index, theme_dir):
    page.set_viewport_size(VIEWPORT)
    before = set(requests.pending)  # long-lived streams opened before the switch
    invoke(page, "switchSettingsTab", tab)
    settled = requests.settle(page, tab, before)
    first = page.evaluate(INVENTORY_JS, tab)
    modal = page.locator("#settingsModal .settings-panel")
    modal.screenshot(path=str(theme_dir / f"{index:02d}-{tab}.png"))

    expand = page.add_style_tag(content=EXPAND_CSS)
    page.evaluate("""(tab) => document.getElementById('settingsTab_' + tab)
        .querySelectorAll('details:not([open])')
        .forEach((d) => { d.open = true; d.dataset.auditOpened = '1'; })""", tab)
    page.wait_for_timeout(600)
    height = page.evaluate("() => Math.ceil(document.querySelector("
                           "'#settingsModal .settings-panel').getBoundingClientRect().height)")
    page.set_viewport_size({"width": VIEWPORT["width"], "height": max(VIEWPORT["height"], height + 80)})
    full = page.evaluate(INVENTORY_JS, tab)
    modal.screenshot(path=str(theme_dir / f"{index:02d}-{tab}-full.png"))
    page.evaluate("""(tab) => document.getElementById('settingsTab_' + tab)
        .querySelectorAll('details[data-audit-opened]')
        .forEach((d) => { d.open = false; delete d.dataset.auditOpened; })""", tab)
    expand.evaluate("(node) => node.remove()")
    return {"controls": first["controls"], "collapsed": first["collapsed"],
            "fill": first["fill"], "loading": first["loading"] or not settled,
            "explanatory": [b for b in full["blocks"] if not b["label"]]}


def write_report(out_dir, tabs):
    (out_dir / "report.json").write_text(json.dumps(tabs, ensure_ascii=False, indent=1))
    lines = ["# Settings audit", "",
             "fill = tab content height / visible height on first open (%).", "",
             "| tab | controls | collapsed | fill | explanatory blocks | chars |",
             "|---|---|---|---|---|---|"]
    for tab, row in tabs.items():
        copy = row["explanatory"]
        lines.append(f"| {tab} ({row['label']}) | {row['controls']} | {len(row['collapsed'])}"
                     f" | {row['fill']}%{' (still loading)' if row['loading'] else ''}"
                     f" | {len(copy)} | {sum(b['chars'] for b in copy)} |")
    for tab, row in tabs.items():
        lines += ["", f"## {tab} — {row['label']}"]
        if row["collapsed"]:
            lines.append("collapsed: " + " / ".join(row["collapsed"]))
        for block in sorted(row["explanatory"], key=lambda b: -b["chars"])[:REPORT_BLOCKS_PER_TAB]:
            lines.append(f"- [{block['chars']}] `{block['cls'][:40]}` {block['text'][:240]}")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--theme", action="append", choices=("tofu", "dark", "light"))
    parser.add_argument("--lang", default="zh", choices=("zh", "en"))
    parser.add_argument("--tab", action="append", help="limit to these tab ids")
    args = parser.parse_args()
    themes = args.theme or ["tofu"]
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    base, shutdown = start_server()
    ensure_chromium_env()
    if use_cjk_fontconfig() is None and not os.environ.get("FONTCONFIG_FILE") and args.lang == "zh":
        print("note: no CJK-capable fontconfig found beside the interpreter; "
              "set FONTCONFIG_FILE if Chinese text renders as boxes", file=sys.stderr)
    from playwright.sync_api import sync_playwright

    report = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
        page = browser.new_page(viewport=VIEWPORT)
        page.emulate_media(reduced_motion="reduce")
        requests = RequestTracker(page)
        tabs = open_settings(page, base, args.lang)
        if args.tab:
            tabs = [entry for entry in tabs if entry[0] in args.tab]
        for theme in themes:
            page.evaluate("(theme) => document.documentElement.setAttribute('data-theme', theme)", theme)
            theme_dir = out_dir / theme
            theme_dir.mkdir(exist_ok=True)
            for index, (tab, label) in enumerate(tabs):
                row = capture_tab(page, requests, tab, index, theme_dir)
                report.setdefault(tab, {"label": label, **row})
        browser.close()
    shutdown.set()
    write_report(out_dir, report)
    print(f"settings audit -> {out_dir}/report.md")


if __name__ == "__main__":
    main()
