#!/usr/bin/env python3
"""Validate one unpublished Vite graph in a real Chromium page.

Responsibility: load the production-minified entry from an explicit artifact
directory, provide bounded in-memory API fixtures, and require application
readiness plus conversation/folder bootstrap requests before publication.
No network, user storage, or running Tofu process is used.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
import sys
import time
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chromium_env  # noqa: E402


CANARY_ORIGIN = 'http://tofu-frontend-canary.test'
# No-progress window, not a total deadline: a healthy slow boot renews it on
# every request, console line or document mutation (2026-11-06: the shared
# tree's own build needed about 124 s on a loaded host).
BOOT_TIMEOUT_MS = 20_000
_BOOT_POLL_MS = 1_000
MAX_ASSET_BYTES = 16 * 1024 * 1024


class BootNoProgress(TimeoutError):
    """The page stopped progressing for a whole window."""


def _await_boot_ready(page, progress, page_errors, fatal_console, *,
                      ready_flag=None) -> None:
    """Wait for readiness and the sidebar while the page keeps progressing.

    Each slice is bounded by the driver, so a busy page cannot extend it. Real
    errors fail at once. The window renews on every routed request, console
    line or document mutation, never on elapsed time alone.
    """
    deadline = time.monotonic() + BOOT_TIMEOUT_MS / 1000
    seen = 0
    while True:
        if page_errors or fatal_console:
            break  # A real boot error: fail at once, not after the window.
        # The page reports readiness through the exposed binding; no driver
        # command needs its event loop, so a busy page still times out here.
        if ready_flag is not None and ready_flag['ready']:
            return
        if progress['count'] != seen:
            seen = progress['count']
            deadline = time.monotonic() + BOOT_TIMEOUT_MS / 1000
        elif time.monotonic() >= deadline:
            raise BootNoProgress(
                f'frontend bundle canary made no progress for '
                f'{BOOT_TIMEOUT_MS // 1000}s; page errors={page_errors[:3]}, '
                f'console errors={fatal_console[:3]}')
        page.wait_for_timeout(_BOOT_POLL_MS)


def _safe_file(root: Path, relative: str) -> Path:
    candidate = (root / unquote(relative)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f'canary asset escapes its root: {relative}') from exc
    if not candidate.is_file():
        raise RuntimeError(f'canary asset is missing: {relative}')
    if candidate.stat().st_size > MAX_ASSET_BYTES:
        raise RuntimeError(f'canary asset exceeds {MAX_ASSET_BYTES} bytes: {relative}')
    return candidate


def _entry_file(artifact_dir: Path, manifest_path: Path) -> str:
    manifest_path = manifest_path.resolve()
    try:
        manifest_path.relative_to(artifact_dir.resolve())
    except ValueError as exc:
        raise RuntimeError('canary manifest must stay inside its artifact directory') from exc
    manifest = json.loads(manifest_path.read_text('utf-8'))
    row = manifest.get('frontend/src/main.ts')
    entry = row.get('file') if isinstance(row, dict) else None
    if not isinstance(entry, str) or not entry.startswith('assets/') or '..' in entry.split('/'):
        raise RuntimeError('canary manifest has no safe frontend/src/main.ts entry')
    _safe_file(artifact_dir, entry)
    return entry


def _shell(entry: str) -> str:
    source = (ROOT / 'index.html').read_text('utf-8')
    marker = '<!-- TOFU_APP_ASSETS -->'
    if source.count(marker) != 1:
        raise RuntimeError('application shell must contain one asset marker')
    boot = (
        '<script type="application/json" id="tofu-boot-config">'
        '{"entry":"main","uiLanguageHint":"en","viteBase":"static/vite/",'
        '"transportProfile":"canary"}</script>\n'
        f'<script type="module" src="/static/vite/{entry}"></script>'
    )
    return source.replace(marker, boot)


def _api_payload(path: str) -> dict:
    """Flat payload for a canary v4 product route (envelope added by caller)."""
    if path.endswith('/conversations'):
        return {
            'ok': True,
            'items': [],
                    'page': {'totalCount': 0, 'hasMore': False, 'nextCursor': None},
        }
    if path.endswith('/users/me'):
        return {'ok': True, 'id': 1, 'userId': 1}
    if path.endswith('/health'):
        return {'ok': True, 'storage': {'ready': True}}
    if path.endswith('/features'):
        return {'ok': True, 'debug_mode': False, 'optimizer_enabled': False}
    if path.endswith('/router/owner-delegation'):
        # The runtime reads this boot-required surface while building the
        # router controls; an object without `actions` throws before app-ready.
        return {
            'ok': True, 'active': False, 'grant': None,
            'router': {'conversationId': '', 'revision': 0,
                       'availability': 'unbound'},
            'actions': [],
        }
    return {'ok': True, 'items': []}


def _api_body(path: str) -> dict:
    """Mirror the served v4 surface: success responses carry `{data, meta}`."""
    if path.startswith('/api/v4/'):
        return {'data': _api_payload(path), 'meta': {'requestId': 'canary'}}
    return _api_payload(path)


def run_canary(artifact_dir: Path, manifest_path: Path | None = None) -> None:
    artifact_dir = artifact_dir.resolve()
    entry = _entry_file(artifact_dir, manifest_path or artifact_dir / 'manifest.json')
    shell = _shell(entry)
    chromium_env.ensure_chromium_env()
    executable = chromium_env.chromium_executable()
    if not executable:
        issues = '; '.join(chromium_env.describe_chromium_env()['issues'])
        raise RuntimeError(f'frontend canary requires Chromium: {issues}')

    from playwright.sync_api import sync_playwright

    observed = {'conversations': False, 'folders': False}
    page_errors: list[str] = []
    fatal_console: list[str] = []
    progress = {'count': 0}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=executable,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        context = browser.new_context(service_workers='block')
        page = context.new_page()
        page.on('pageerror', lambda error: page_errors.append(str(error)))
        page.on('console', lambda message: (
            fatal_console.append(message.text)
            if message.type == 'error' and (
                '[boot]' in message.text or '[main.js]' in message.text
            ) else None
        ))
        page.on('console', lambda message: progress.__setitem__(
            'count', progress['count'] + 1))
        # The page reports activity to the driver, so the driver never has to
        # evaluate into a busy page to learn that it is still working.
        ready_flag = {'ready': False}
        page.expose_function('__tofuCanaryProgress', lambda: progress.__setitem__(
            'count', progress['count'] + 1))
        page.expose_function('__tofuCanaryReady', lambda: ready_flag.__setitem__('ready', True))
        page.add_init_script("""
          window.__tofuCanary = { ready: false, unhandled: [] };
          window.addEventListener('tofu:app-ready', () => {
            window.__tofuCanary.ready = true;
            if (window.__tofuCanaryReady) window.__tofuCanaryReady();
          });
          window.addEventListener('error', (event) => {
            window.__tofuCanary.unhandled.push(String(event.error || event.message));
          });
          window.addEventListener('unhandledrejection', (event) => {
            window.__tofuCanary.unhandled.push(String(event.reason));
          });
          if (window.MutationObserver) {
            const observer = new MutationObserver(() => {
              window.__tofuCanaryProgress();
            });
            const observeDocument = () => {
              // documentElement does not exist yet on an empty initial document.
              if (document.documentElement) {
                observer.observe(document.documentElement, {
                  childList: true, subtree: true, attributes: true, characterData: true
                });
              } else if (!window.__tofuCanary.ready) {
                setTimeout(observeDocument, 25);
              }
            };
            observeDocument();
          }
          if (window.fetch) {
            const originalFetch = window.fetch.bind(window);
            window.fetch = (...args) => {
              window.__tofuCanaryProgress();
              return originalFetch(...args).then(
                response => { window.__tofuCanaryProgress(); return response; },
                error => { window.__tofuCanaryProgress(); throw error; });
            };
          }
        """)

        def route_request(route) -> None:
            parsed = urlparse(route.request.url)
            path = parsed.path
            try:
                progress['count'] += 1
                if path == '/':
                    route.fulfill(status=200, body=shell, content_type='text/html; charset=utf-8')
                    return
                if path.startswith('/static/vite/'):
                    relative = path.removeprefix('/static/vite/')
                    target = _safe_file(artifact_dir, relative)
                    content_type = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
                    if target.suffix in ('.js', '.mjs'):
                        content_type = 'text/javascript; charset=utf-8'
                    route.fulfill(status=200, body=target.read_bytes(), content_type=content_type)
                    return
                if path.startswith('/static/'):
                    target = _safe_file(ROOT / 'static', path.removeprefix('/static/'))
                    route.fulfill(
                        status=200,
                        body=target.read_bytes(),
                        content_type=mimetypes.guess_type(target.name)[0] or 'application/octet-stream',
                    )
                    return
                if path.startswith('/api/'):
                    if path.endswith('/conversations'):
                        observed['conversations'] = True
                    elif path.endswith('/folders'):
                        observed['folders'] = True
                    route.fulfill(
                        status=200,
                        body=json.dumps(_api_body(path)),
                        content_type='application/json',
                    )
                    return
                route.fulfill(status=204, body='')
            except Exception as exc:
                route.fulfill(status=500, body=str(exc), content_type='text/plain')

        page.route('**/*', route_request)
        try:
            # commit, not DOMContentLoaded: a page busy parsing its modules has
            # not rendered yet, so a fixed navigation budget would fail it.
            page.goto(f'{CANARY_ORIGIN}/', wait_until='commit', timeout=BOOT_TIMEOUT_MS)
            _await_boot_ready(page, progress, page_errors, fatal_console,
                              ready_flag=ready_flag)
            page.wait_for_timeout(250)
            unhandled = page.evaluate('window.__tofuCanary.unhandled')
        finally:
            context.close()
            browser.close()

    failures = []
    if not observed['conversations']:
        failures.append('conversation catalog request was not issued')
    if not observed['folders']:
        failures.append('folder catalog request was not issued')
    if page_errors:
        failures.append(f'page errors: {page_errors[:3]}')
    if fatal_console:
        failures.append(f'fatal console errors: {fatal_console[:3]}')
    if unhandled:
        failures.append(f'unhandled browser errors: {unhandled[:3]}')
    if failures:
        raise RuntimeError('frontend bundle canary failed: ' + '; '.join(failures))
    print(f'frontend bundle canary: OK ({entry})')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    try:
        run_canary(args.artifact_dir, args.manifest)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
