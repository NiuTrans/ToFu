"""Owner-isolation contracts for capability-specific task HTTP surfaces.

Generic task routes already enforce ``TaskRuntime.get_owned``.  These tests
cover the custom payload and file-delivery routes that cannot use the generic
adapter, so a future hand-written endpoint cannot expose or mutate a task by
opaque id alone.
"""

from __future__ import annotations

import asyncio
import importlib
import uuid

import pytest
from quart import Quart


pytestmark = pytest.mark.unit


def _status_code(response) -> int:
    if isinstance(response, tuple):
        return int(response[1])
    return int(response.status_code)


@pytest.mark.parametrize(
    ("module_name", "runtime_name", "handler_name", "path"),
    (
        (
            "routes.paper_pkg._qa_translate",
            "_qa_runtime",
            "poll_qa_task",
            "/api/v1/paper/qa/poll",
        ),
        (
            "routes.paper_pkg._qa_translate",
            "_translate_runtime",
            "poll_translate_task",
            "/api/v1/paper/translate/poll",
        ),
        (
            "routes.paper_pkg._recommend",
            "_recommend_runtime",
            "poll_recommend_task",
            "/api/v1/paper/recommend/poll",
        ),
        (
            "routes.paper_pkg._report",
            "_report_runtime",
            "poll_report_task",
            "/api/v1/paper/report/poll",
        ),
    ),
)
def test_paper_poll_routes_hide_foreign_tasks(
    monkeypatch,
    module_name,
    runtime_name,
    handler_name,
    path,
):
    module = importlib.import_module(module_name)
    runtime = getattr(module, runtime_name)
    handler = getattr(module, handler_name)
    task_id = f"owner_audit_{uuid.uuid4().hex}"
    owner_user_id = 41
    runtime.create(user_id=owner_user_id, task_id=task_id)
    monkeypatch.setattr(module, "request_user_id", lambda: 42)
    app = Quart(__name__)

    async def request_as_foreign_owner():
        async with app.test_request_context(
            f"{path}?task_id={task_id}&cursor=0"
        ):
            return await handler()

    try:
        response = asyncio.run(request_as_foreign_owner())
        assert _status_code(response) == 404
        assert runtime.get_owned(task_id, user_id=owner_user_id) is not None
    finally:
        runtime.remove_owned(task_id, user_id=owner_user_id)


def test_recommend_abort_never_mutates_a_foreign_task(monkeypatch):
    from routes.paper_pkg import _recommend as route

    task_id = f"owner_audit_{uuid.uuid4().hex}"
    owner_user_id = 41
    task = route._recommend_runtime.create(
        user_id=owner_user_id,
        task_id=task_id,
    )
    monkeypatch.setattr(route, "request_user_id", lambda: 42)
    app = Quart(__name__)

    async def abort_as_foreign_owner():
        async with app.test_request_context(
            "/api/v1/paper/recommend/abort",
            method="POST",
            json={"task_id": task_id},
        ):
            return await route.abort_recommend_task()

    try:
        response = asyncio.run(abort_as_foreign_owner())
        assert _status_code(response) == 404
        assert not task["abort_event"].is_set()
    finally:
        route._recommend_runtime.remove_owned(
            task_id,
            user_id=owner_user_id,
        )


def test_slides_workdir_requires_owner_for_live_and_disk_tasks(
    monkeypatch,
    tmp_path,
):
    from lib.production.jobs import write_manifest
    from lib.slides import engine
    from lib.slides.runtime import _slides_runtime
    from routes.api_v1.slides import _task_workdir

    owner_user_id = 41
    live_task_id = f"slides_{uuid.uuid4().hex[:16]}"
    live_workdir = tmp_path / "live"
    live_task = _slides_runtime.create(
        user_id=owner_user_id,
        task_id=live_task_id,
        meta={"workdir": str(live_workdir)},
    )
    _slides_runtime.update_fields(
        live_task_id,
        fields={"workdir": str(live_workdir)},
    )

    try:
        assert _task_workdir(
            live_task_id,
            user_id=owner_user_id,
        ) == str(live_workdir)
        assert _task_workdir(live_task_id, user_id=42) == ""
        assert live_task["_userId"] == owner_user_id
    finally:
        _slides_runtime.remove_owned(
            live_task_id,
            user_id=owner_user_id,
        )

    slides_root = tmp_path / "slides-root"
    disk_task_id = f"slides_{uuid.uuid4().hex[:16]}"
    disk_workdir = slides_root / "jobs" / disk_task_id
    write_manifest(
        str(disk_workdir),
        {"task_id": disk_task_id, "user_id": owner_user_id},
        fields=("task_id", "user_id"),
        kind="slides-deck",
        state="done",
        log_label="SlidesTest",
    )
    monkeypatch.setattr(engine, "slides_root", lambda: str(slides_root))

    assert _task_workdir(
        disk_task_id,
        user_id=owner_user_id,
    ) == str(disk_workdir)
    assert _task_workdir(disk_task_id, user_id=42) == ""


def test_vlm_registry_scopes_direct_and_filename_lookups_by_owner():
    from lib.pdf_parser.vlm import _tasks

    owner_task_id = f"vlm_{uuid.uuid4().hex}"
    foreign_task_id = f"vlm_{uuid.uuid4().hex}"
    created = 123.0
    with _tasks._vlm_lock:
        _tasks._vlm_tasks[owner_task_id] = {
            "status": "processing",
            "progress": "0/?",
            "filename": "same.pdf",
            "created": created,
            "user_id": 41,
            "error": None,
        }
        _tasks._vlm_tasks[foreign_task_id] = {
            "status": "processing",
            "progress": "0/?",
            "filename": "same.pdf",
            "created": created + 1,
            "user_id": 42,
            "error": None,
        }

    try:
        assert _tasks.get_vlm_task(owner_task_id, user_id=41) is not None
        assert _tasks.get_vlm_task(owner_task_id, user_id=42) is None
        assert [
            item["taskId"]
            for item in _tasks.find_vlm_tasks_by_filename(
                "same.pdf",
                user_id=41,
            )
        ] == [owner_task_id]
    finally:
        with _tasks._vlm_lock:
            _tasks._vlm_tasks.pop(owner_task_id, None)
            _tasks._vlm_tasks.pop(foreign_task_id, None)
