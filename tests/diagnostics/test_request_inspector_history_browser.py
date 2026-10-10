"""Real HTTP/native long-history details and final wire survive a capped outline."""

from pathlib import Path
import json
import time
import uuid
import hashlib
import os
import subprocess

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize("width", [390, 1440])
def test_native_overflow_serves_verified_late_payload_and_latest_wire(
    browser, live_server, width
):
    from playwright.sync_api import expect
    from lib.storage import get_storage_client, storage_status
    from lib.tasks_pkg import request_inspector as ri
    from tests.conftest import _dismiss_onboarding_modals

    context = browser.new_context(
        viewport={"width": width, "height": 960}, service_workers="block"
    )
    context.add_init_script(
        "localStorage.setItem('tofu_onboarding_v1_done','1');window.TOFU_API_V4=true"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    cid = "history-window-" + uuid.uuid4().hex
    tid = "history-task-" + uuid.uuid4().hex
    endpoint = f"{live_server}/api/v4/conversations/{cid}/turns/settled"
    initial = {
        "type": "messages_snapshot",
        "kind": "request",
        "roundNum": 1,
        "messages": [
            {"role": "user", "content": "CONTROLLED COMPLETE BEFORE OVERFLOW"}
        ],
        "tools": [],
        "model": "synthetic-no-provider",
    }
    client = get_storage_client(write=True)
    try:
        created = context.request.post(
            endpoint,
            data={
                "commandId": "window-human-" + cid,
                "actor": "human",
                "kind": "text",
                "projection": {"content": "Controlled diagnostic history"},
                "conversation": {
                    "allowCreate": True,
                    "title": "Controlled history window",
                    "createdAt": 100,
                    "settings": {},
                },
            },
        )
        assert created.status == 200, created.text()
        client.command(
            "task_results.checkpoint",
            {
                "key": tid,
                "expected_version": 0,
                "value": {
                    "task_id": tid,
                    "conv_id": cid,
                    "user_id": 1,
                    "content": "",
                    "status": "done",
                    "created_at": int(time.time() * 1000),
                    "completed_at": int(time.time() * 1000),
                },
            },
            None,
        )
        client.command(
            "event.append", {"task_id": tid, "sequence": 0, "event": initial}, None
        )
        ri.invalidate_task_cache(tid)
        page.goto(live_server, wait_until="domcontentloaded")
        _dismiss_onboarding_modals(page)
        item = page.locator(f'.conv-item[data-conv-id="{cid}"]')
        expect(item).to_have_count(1, timeout=20000)
        if width < 500:
            page.locator(".toggle-sidebar-btn").click()
        item.click(position={"x": 12, "y": 12})
        expect(page.locator("#chatInner")).to_contain_text(
            "Controlled diagnostic history", timeout=20000
        )
        # Mobile intentionally hides this desktop entry; invoke its existing
        # action to test the real drawer, not entry-button availability.
        page.locator("#debugToggle").evaluate("node => node.click()")
        expect(page.locator("#debugContent")).to_contain_text(
            "CONTROLLED COMPLETE BEFORE OVERFLOW", timeout=20000
        )
        for first in range(1, 10000, 128):
            events = [
                {
                    "task_id": tid,
                    "sequence": n,
                    "event": {"type": "round_usage", "roundNum": n, "usage": {}},
                }
                for n in range(first, min(first + 128, 10000))
            ]
            client.command("event.append_batch", {"events": events}, None)
        final = {
            **initial,
            "roundNum": 10001,
            "messages": [{"role": "user", "content": "VERIFIED LATEST AFTER OVERFLOW"}],
        }
        client.command(
            "event.append", {"task_id": tid, "sequence": 10000, "event": final}, None
        )
        ri.invalidate_task_cache(tid)
        started = time.perf_counter()
        folded = context.request.get(f"{live_server}/api/v4/tasks/{tid}/requests")
        fold_seconds = time.perf_counter() - started
        assert folded.status == 200, folded.text()
        doc = folded.json()["data"]
        assert (
            doc["coverage"] == "partial" and doc["eventReadBounds"][0]["taskId"] == tid
        )
        for round_num in [1, 10001]:
            detail = context.request.get(
                f"{live_server}/api/v4/tasks/{tid}/requests/{round_num}"
            )
            assert detail.status == 200, detail.text()
            expected = (
                "CONTROLLED COMPLETE BEFORE OVERFLOW"
                if round_num == 1
                else "VERIFIED LATEST AFTER OVERFLOW"
            )
            assert detail.json()["data"]["messages"][0]["content"] == expected
        with page.expect_response(lambda response: f"/conv-wire/{cid}" in response.url):
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(page.locator("#debugContent")).to_contain_text(
            "VERIFIED LATEST AFTER OVERFLOW", timeout=20000
        )
        expect(page.locator("#debugContent .ri-feed-truncated")).to_have_count(0)
        expect(page.locator("#debugContent")).not_to_contain_text(
            "CONTROLLED COMPLETE BEFORE OVERFLOW"
        )
        page.locator("#debugContent .debug-msg-header").first.click()
        body = page.locator("#debugContent .debug-msg-body").first
        expect(body).to_be_visible()
        expect(body).to_contain_text("VERIFIED LATEST AFTER OVERFLOW")
        assert not errors, errors
        artifact = Path("output/playwright") / f"inspector-event-window-{width}.png"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifact))
        authority = storage_status()
        engine_info = (
            json.loads(
                subprocess.check_output(
                    [os.environ["TOFUDB_BIN"], "--build-info"], text=True
                )
            )
            if authority["backend"] == "tofudb" and os.environ.get("TOFUDB_BIN")
            else None
        )
        application = {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in [
                "lib/tasks_pkg/request_inspector.py",
                "lib/tasks_pkg/request_inspector_history.py",
                "lib/tasks_pkg/snapshot_delta.py",
            ]
        }
        artifact.with_suffix(".json").write_text(
            json.dumps(
                {
                    "storage_authority": authority,
                    "native_build_info": engine_info,
                    "application_sources": application,
                    "scope": "Synthetic events over real isolated native/HTTP/browser; no provider calls",
                    "width": width,
                    "task_id": tid,
                    "retained_event_bounds": client.query(
                        "event.bounds", {"task_id": tid, "include_read_guard": True}
                    ),
                    "fold_seconds": fold_seconds,
                    "event_read_bounds": doc["eventReadBounds"],
                    "read_bound_json_bytes": len(
                        json.dumps(
                            doc["eventReadBounds"], separators=(",", ":")
                        ).encode()
                    ),
                },
                indent=2,
            )
            + "\n"
        )
        # Fresh browser presentation must read the latest source independently;
        # it cannot borrow a previously rendered wire state.
        page.reload(wait_until="domcontentloaded")
        _dismiss_onboarding_modals(page)
        item = page.locator(f'.conv-item[data-conv-id="{cid}"]')
        expect(item).to_have_count(1, timeout=20000)
        if width < 500:
            page.locator(".toggle-sidebar-btn").click()
        item.click(position={"x": 12, "y": 12})
        page.locator("#debugToggle").evaluate("node => node.click()")
        expect(page.locator("#debugContent")).to_contain_text(
            "VERIFIED LATEST AFTER OVERFLOW", timeout=20000
        )
        expect(page.locator("#debugContent .ri-feed-truncated")).to_have_count(0)
        expect(page.locator("#debugContent")).not_to_contain_text(
            "CONTROLLED COMPLETE BEFORE OVERFLOW"
        )
        assert (
            page.locator("#debugContent .ri-feed-turn .ri-feed-turn-preview").count()
            == 1
        )
        assert not errors, errors
    finally:
        context.request.delete(f"{live_server}/api/v4/conversations/{cid}")
        ri.invalidate_task_cache(tid)
        context.close()
