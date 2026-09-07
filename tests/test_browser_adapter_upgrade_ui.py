"""Behavior contract for actionable browser-adapter upgrade guidance.

A connected legacy extension can keep ordinary browser reads working while it
lacks capabilities required by native site adapters.  The Settings UI must not
collapse that state into an unexplained ``upgrade required`` badge: it names
what is old, lists the missing browser abilities, and offers a working path to
the browser-extension upgrade surface.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SEARCH_PANEL = ROOT / "static/settings_panels/search.html"


def _node_available() -> bool:
    return bool(shutil.which("node") and (ROOT / "node_modules/jsdom").is_dir())


_HARNESS = textwrap.dedent(
    r"""
    (async () => {
    const fs = require('fs');
    const path = require('path');
    const { JSDOM } = require(path.join(process.argv[1], 'node_modules', 'jsdom'));
    const locale = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
    const dom = new JSDOM(`<!doctype html><body>
      <div id="searchBrowserStatus"></div>
      <div id="searchAdapterSummary">正在读取站点适配器…</div>
      <div id="browserAccessList"></div>
      <input id="browserReadDenyInput">
    </body>`, { url: 'http://localhost/' });

    globalThis.window = dom.window;
    globalThis.document = dom.window.document;
    globalThis.HTMLElement = dom.window.HTMLElement;
    globalThis.HTMLInputElement = dom.window.HTMLInputElement;
    globalThis.AbortController = dom.window.AbortController;
    globalThis.Event = dom.window.Event;

    const interpolate = (template, values) => String(template).replace(
      /\{([A-Za-z0-9_]+)\}/g,
      (token, name) => Object.prototype.hasOwnProperty.call(values || {}, name)
        ? String(values[name] ?? '') : token,
    );
    window.t = (key, values) => interpolate(locale[key] || key, values || {});

    let opened = 0;
    let closed = 0;
    let upgradeRequested = false;
    window.openLocalControlModal = (options) => {
      opened += 1;
      upgradeRequested = options?.browserUpgrade === true;
    };
    window.closeSettings = () => { closed += 1; };
    const toasts = [];
    window.showToast = (message) => { toasts.push(String(message)); };

    const cases = JSON.parse(process.argv[4]);
    const result = {};
    eval(fs.readFileSync(process.argv[2], 'utf8'));

    for (const [name, fixture] of Object.entries(cases)) {
      window.Api = {
        browser: {
          status: async () => {
            if (fixture.renderError) throw new Error('status failed');
            return fixture.status;
          },
          adapters: async () => fixture.adapters,
          access: async () => ({ read_denied_domains: [], write_grants: [] }),
          updateAccess: async () => {
            if (fixture.denyError) throw new Error('policy update failed');
            return { read_denied_domains: [], write_grants: [] };
          },
          test: async () => ({}),
        },
      };
      opened = 0; closed = 0;
      upgradeRequested = false;
      toasts.length = 0;
      await globalThis.renderSearchBrowserAccess();
      if (fixture.denyError) {
        document.getElementById('browserReadDenyInput').value = 'blocked.example';
        await globalThis.browserAccessDenyRead();
      }
      const rows = [...document.querySelectorAll('.browser-adapter-row')];
      const upgradeButtons = [...document.querySelectorAll('.browser-adapter-upgrade-button')];
      if (upgradeButtons[0]) upgradeButtons[0].click();
      result[name] = {
        connection: document.getElementById('searchBrowserStatus').textContent
          .replace(/\s+/g, ' ').trim(),
        rowTexts: rows.map((row) => row.textContent.replace(/\s+/g, ' ').trim()),
        badges: rows.map((row) => row.querySelector('.search-status-badge')?.textContent.trim() || ''),
        upgradeButtonCount: upgradeButtons.length,
        upgradeButtonText: upgradeButtons[0]?.textContent.trim() || '',
        opened,
        closed: closed,
        upgradeRequested,
        summary: document.getElementById('searchAdapterSummary').textContent.trim(),
        toasts: [...toasts],
      };
    }
    console.log(JSON.stringify(result));
    })().catch((error) => { console.error(error); process.exitCode = 1; });
    """
)


def _render_cases(cases: dict) -> dict:
    if not _node_available():
        pytest.skip("node + jsdom dev dependencies are required")
    bundle = native_module_path(
        ".native/browser-access.js",
        "frontend/src/features/settings/browser-access.ts",
    )
    proc = subprocess.run(
        [
            "node",
            "-e",
            _HARNESS,
            str(ROOT),
            bundle,
            str(ROOT / "frontend/src/i18n/locales/zh.json"),
            json.dumps(cases, ensure_ascii=False),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, (
        f"browser-access harness failed:\nSTDOUT {proc.stdout}\nSTDERR {proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _adapter(status: str, *, missing: list[str] | None = None) -> dict:
    return {
        "id": "modelplaza",
        "name": "ModelPlaza",
        "health": {
            "status": status,
            "healthy": status == "ready",
            "missing_capabilities": missing or [],
            "client_id": "legacy-browser" if status != "offline" else "",
            "protocol_version": 2,
        },
    }


def test_upgrade_row_names_versions_missing_abilities_and_opens_recovery():
    rendered = _render_cases({
        "outdated": {
            "status": {
                "connected": True,
                "servedExtVersion": "2.4.0",
                "clients": [{
                    "client_id": "legacy-browser",
                    "ext_version": "1.8.0",
                    "protocol_version": 2,
                    "last_poll": 20,
                }],
            },
            "adapters": {
                "count": 1,
                "available_count": 0,
                "adapters": [_adapter(
                    "upgrade_required", missing=["snapshot", "fill"]
                )],
            },
        },
    })["outdated"]

    assert rendered["badges"] == ["扩展缺少能力"]
    assert rendered["upgradeButtonCount"] == 1
    assert rendered["upgradeButtonText"] == "升级浏览器扩展"
    detail = rendered["rowTexts"][0]
    assert "浏览器扩展 v1.8.0" in detail and "v2.4.0" in detail
    assert "网页结构快照" in detail and "填写表单" in detail
    assert rendered["closed"] == 1 and rendered["opened"] == 1, (
        "the recovery button must close Settings and open the real Local Control "
        "extension install/upgrade surface"
    )
    assert rendered["upgradeRequested"] is True, (
        "Local Control must retain the capability-mismatch intent; legacy clients "
        "often report no extension version, so version comparison alone is a dead end"
    )


def test_same_binary_version_reports_capability_gap_not_a_fake_version_jump():
    rendered = _render_cases({
        "protocol_gap": {
            "status": {
                "connected": True,
                "servedExtVersion": "2.4.0",
                "clientProtocolVersion": 2,
                "clients": [{
                    "client_id": "legacy-browser",
                    "ext_version": "2.4.0",
                    "protocol_version": 2,
                    "last_poll": 20,
                }],
            },
            "adapters": {
                "count": 1,
                "available_count": 0,
                "adapters": [_adapter(
                    "upgrade_required", missing=["snapshot"]
                )],
            },
        },
    })["protocol_gap"]

    detail = rendered["rowTexts"][0]
    assert "协议 v2" in detail
    assert "网页结构快照" in detail
    assert "v2.4.0 → v2.4.0" not in detail
    assert rendered["upgradeButtonCount"] == 1


def test_ready_and_offline_adapters_never_receive_a_misleading_upgrade_button():
    rendered = _render_cases({
        "mixed": {
            "status": {"connected": True, "clients": []},
            "adapters": {
                "count": 2,
                "available_count": 1,
                "adapters": [
                    _adapter("ready"),
                    {**_adapter("offline"), "id": "xiaohongshu", "name": "小红书"},
                    {**_adapter("error"), "id": "broken", "name": "异常适配器"},
                ],
            },
        },
    })["mixed"]

    assert rendered["badges"] == ["只读可用", "扩展离线", "浏览器状态不可用"]
    assert rendered["upgradeButtonCount"] == 0
    assert rendered["opened"] == 0 and rendered["closed"] == 0


def test_current_extension_surfaces_devtools_bridge_readiness():
    rendered = _render_cases({
        "devtools": {
            "status": {
                "connected": True,
                "clients": [{
                    "client_id": "browser-a",
                    "profile": "Default",
                    "protocol_version": 2,
                    "capabilities": ["devtools_console", "js_debugger"],
                    "last_poll": 20,
                }],
            },
            "adapters": {"count": 0, "available_count": 0, "adapters": []},
        },
    })["devtools"]

    assert "DevTools Bridge 就绪" in rendered["connection"]


def test_browser_api_failures_clear_loading_copy_and_report_policy_errors():
    rendered = _render_cases({
        "render_failure": {
            "renderError": True,
            "status": {},
            "adapters": {},
        },
        "deny_failure": {
            "denyError": True,
            "status": {"connected": False},
            "adapters": {"count": 0, "available_count": 0, "adapters": []},
        },
    })

    assert rendered["render_failure"]["summary"] == "浏览器状态不可用"
    assert rendered["deny_failure"]["toasts"] == ["policy update failed"]


def test_search_connection_install_button_targets_the_real_local_control_action():
    panel = SEARCH_PANEL.read_text(
        encoding="utf-8",
    )
    install = next(
        line for line in panel.splitlines()
        if 'data-i18n="settings.browserInstallUpgrade"' in line
    )
    assert 'data-tofu-action="closeSettings();openLocalControlModal()"' in install
    assert "openLocalControl()" not in install, (
        "the old action name does not exist; it rendered an install/upgrade "
        "button that silently did nothing"
    )
