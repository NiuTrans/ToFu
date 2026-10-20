"""Capture the real CONTEXT disclosure with clearly synthetic, public data.

This bundles the retained production renderer and native disclosure/icon
helpers, then opens a local static fixture. No app server, user store, provider,
or captured private conversation is accessed.
"""

import argparse
import functools
import hashlib
import http.server
import json
import subprocess
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--browser", required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    assets = Path(__file__).parent / "assets"
    renderer = repo / "frontend/src/runtime/sections/core/debug_panel.js"
    fixture = [{
        "registryId": "active_checklist",
        "source": "task.todo_state",
        "necessity": "required_continuity",
        "reason": "Restore the accepted checklist.",
        "tokens": 86,
        "injected": True,
        "placement": "tail",
    }]
    # Reproduction provenance, including the exact production bytes inspected.
    sources = [
        renderer,
        repo/"frontend/src/features/diagnostics-message-disclosure.ts",
        repo/"frontend/src/icons.ts",
        repo/"frontend/src/html-safety.ts",
        repo/"static/styles.css",
        repo/"frontend/src/styles/application/02-inspector-shell.css",
    ]
    with tempfile.TemporaryDirectory(prefix="tofu-poster-inspector-") as directory:
        temporary = Path(directory)
        entry = temporary/"entry.ts"
        entry.write_text(
            f'import {{ createInspectorDisclosure, syncInspectorDisclosure }} from '
            f'{json.dumps(str(sources[1]))};\n'
            f'import {{ iconHtml as Icon }} from {json.dumps(str(sources[2]))};\n'
            f'import {{ escapeHtml }} from {json.dumps(str(sources[3]))};\n'
            + renderer.read_text() +
            '\nupdateDebugContextBlock(document.getElementById("fixture"),'
            + json.dumps(fixture) + ');\n'
            'document.querySelector(".debug-msg-header").click();\n'
        )
        bundle = temporary/"bundle.js"
        driver = temporary/"build.cjs"
        driver.write_text(
            f'const esbuild=require({json.dumps(str(repo/"node_modules/esbuild/lib/main.js"))});'
            f'esbuild.buildSync({{entryPoints:[{json.dumps(str(entry))}],bundle:true,'
            f'outfile:{json.dumps(str(bundle))},format:"iife",platform:"browser"}});'
        )
        subprocess.run([args.node, str(driver)], check=True, capture_output=True)
        style = sources[4].read_text()+"\n"+sources[5].read_text()
        (temporary/"index.html").write_text(
            '<!doctype html><html data-theme="light" lang="en"><head>'
            '<meta charset="UTF-8"><style>'+style+'</style><style>'
            'body{margin:0;background:white;display:block;min-width:0;}'
            '#fixture{width:560px;padding:14px;box-sizing:border-box;}'
            '</style></head><body><div id="fixture"></div>'
            '<script src="bundle.js"></script></body></html>'
        )
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass
        handler = functools.partial(Handler, directory=directory)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    executable_path=args.browser, headless=True, args=["--no-sandbox"])
                page = browser.new_page(viewport={"width": 560, "height": 900},
                                        device_scale_factor=3)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/",
                          wait_until="networkidle")
                page.locator(".debug-context-block.open").wait_for()
                page.evaluate("document.fonts.ready")
                page.locator("#fixture").screenshot(path=str(assets/"inspector-context.png"))
                if errors:
                    raise RuntimeError(errors)
                geometry = page.locator("#fixture").bounding_box()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    provenance = {
        "kind": "production-component-with-synthetic-data",
        "fixture": fixture,
        "not_a_measured_request": True,
        "geometry_css_pixels": geometry,
        "sources": [{"path": str(p.relative_to(repo)),
                     "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sources],
    }
    (assets/"inspector-context.provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2))
    print(json.dumps({"capture": str(assets/"inspector-context.png"), "geometry": geometry}))


if __name__ == "__main__":
    main()
