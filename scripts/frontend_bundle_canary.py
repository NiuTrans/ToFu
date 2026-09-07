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
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chromium_env  # noqa: E402


CANARY_ORIGIN = 'http://tofu-frontend-canary.test'
BOOT_TIMEOUT_MS = 20_000
MAX_ASSET_BYTES = 16 * 1024 * 1024


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


def _api_body(path: str) -> dict:
    if path == '/api/v1/conversations':
        return {
            'ok': True,
            'items': [],
            'page': {'totalCount': 0, 'hasMore': False, 'nextCursor': None},
        }
    if path == '/api/v1/folders':
        return {'ok': True, 'items': []}
    if path == '/api/v1/users/me':
        return {'ok': True, 'id': 1, 'userId': 1}
    if path in ('/api/health', '/api/v1/health'):
        return {'ok': True, 'storage': {'ready': True}}
    if path == '/api/v1/features':
        return {'ok': True, 'debug_mode': False, 'optimizer_enabled': False}
    return {'ok': True, 'items': []}


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
        page.add_init_script("""
          window.__tofuCanary = { ready: false, unhandled: [] };
          window.addEventListener('tofu:app-ready', () => {
            window.__tofuCanary.ready = true;
          });
          window.addEventListener('error', (event) => {
            window.__tofuCanary.unhandled.push(String(event.error || event.message));
          });
          window.addEventListener('unhandledrejection', (event) => {
            window.__tofuCanary.unhandled.push(String(event.reason));
          });
        """)

        def route_request(route) -> None:
            parsed = urlparse(route.request.url)
            path = parsed.path
            try:
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
                    if path == '/api/v1/conversations':
                        observed['conversations'] = True
                    elif path == '/api/v1/folders':
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
            page.goto(f'{CANARY_ORIGIN}/', wait_until='domcontentloaded', timeout=BOOT_TIMEOUT_MS)
            page.wait_for_function(
                'window.__tofuCanary && window.__tofuCanary.ready === true',
                timeout=BOOT_TIMEOUT_MS,
            )
            page.wait_for_function(
                'document.querySelector("#sidebar") !== null', timeout=2_000,
            )
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
