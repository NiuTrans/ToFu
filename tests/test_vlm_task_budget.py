"""Resource, cancellation, and API-attempt contracts for VLM PDF parsing."""

from __future__ import annotations

import threading
import time

import pytest

from lib.agent_core.fair_work_lane import OwnerFairWorkLane
from lib.error_envelope import is_envelope
from lib.llm_errors import AbortedError


pytestmark = pytest.mark.unit


def _wait_for_task(tasks, task_id: str, status: str, timeout: float = 3) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = tasks.get_vlm_task(task_id, user_id=1)
        if task and task['status'] == status:
            return task
        time.sleep(0.01)
    raise AssertionError(f'VLM task {task_id} did not reach {status}')


@pytest.fixture()
def clean_vlm_registry():
    from lib.pdf_parser.vlm import _tasks

    with _tasks._vlm_lock:
        saved = dict(_tasks._vlm_tasks)
        _tasks._vlm_tasks.clear()
    try:
        yield _tasks
    finally:
        with _tasks._vlm_lock:
            _tasks._vlm_tasks.clear()
            _tasks._vlm_tasks.update(saved)


def test_owner_fair_lane_bounds_jobs_and_queue_cancel_releases_payload(
        clean_vlm_registry, monkeypatch):
    tasks = clean_vlm_registry
    lane = OwnerFairWorkLane(
        max_workers=1,
        queue_capacity=1,
        idle_seconds=0,
        thread_name_prefix='test-vlm',
        metric_pool='test-vlm-budget',
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_parse(_pdf_bytes, **_kwargs):
        entered.set()
        assert release.wait(3)
        return 'done'

    monkeypatch.setattr(tasks, '_vlm_lane', lane)
    monkeypatch.setattr(tasks, 'vlm_parse_pdf', blocking_parse)
    monkeypatch.setattr(tasks, 'vlm_task_timeout_seconds', lambda: 60)
    try:
        running_id = tasks.start_vlm_task(b'one', user_id=1)
        assert entered.wait(1)
        queued_id = tasks.start_vlm_task(b'two', user_id=1)

        with pytest.raises(tasks.VlmTaskQueueFull):
            tasks.start_vlm_task(b'three', user_id=2)

        snapshot = tasks.vlm_task_snapshot()
        assert snapshot['active'] == 1
        assert snapshot['queued'] == 1
        assert snapshot['retainedInputBytes'] == len(b'one') + len(b'two')
        assert len(tasks._vlm_tasks) == 2

        assert tasks.cancel_vlm_task(queued_id, user_id=2) is None
        assert tasks.cancel_vlm_task(queued_id, user_id=1) is True
        cancelled = tasks.get_vlm_task(queued_id, user_id=1)
        assert cancelled is not None and cancelled['status'] == 'error'
        assert is_envelope(cancelled['error'])
        assert cancelled['error']['kind'] == 'aborted'

        release.set()
        assert _wait_for_task(tasks, running_id, 'done')['result'] == 'done'
        assert tasks.vlm_task_snapshot()['retainedInputBytes'] == 0
    finally:
        release.set()
        lane.shutdown(wait=True, cancel_pending=True)


def test_running_cancel_reaches_parser_abort_check(
        clean_vlm_registry, monkeypatch):
    tasks = clean_vlm_registry
    lane = OwnerFairWorkLane(
        max_workers=1,
        queue_capacity=1,
        idle_seconds=0,
        thread_name_prefix='test-vlm-abort',
        metric_pool='test-vlm-abort',
    )
    entered = threading.Event()

    def cancellable_parse(_pdf_bytes, *, abort_check, **_kwargs):
        entered.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if abort_check():
                raise AbortedError('test cancellation')
            time.sleep(0.01)
        raise AssertionError('abort callback was never set')

    monkeypatch.setattr(tasks, '_vlm_lane', lane)
    monkeypatch.setattr(tasks, 'vlm_parse_pdf', cancellable_parse)
    monkeypatch.setattr(tasks, 'vlm_task_timeout_seconds', lambda: 60)
    try:
        task_id = tasks.start_vlm_task(b'pdf', user_id=1)
        assert entered.wait(1)
        assert tasks.cancel_vlm_task(task_id, user_id=1) is True
        task = _wait_for_task(tasks, task_id, 'error')
        assert task['error']['kind'] == 'aborted'
        assert tasks.cancel_vlm_task(task_id, user_id=1) is False
    finally:
        lane.shutdown(wait=True, cancel_pending=True)


def test_task_deadline_surfaces_as_retryable_timeout(
        clean_vlm_registry, monkeypatch):
    tasks = clean_vlm_registry
    lane = OwnerFairWorkLane(
        max_workers=1,
        queue_capacity=1,
        idle_seconds=0,
        thread_name_prefix='test-vlm-timeout',
        metric_pool='test-vlm-timeout',
    )

    def deadline_aware_parse(_pdf_bytes, *, abort_check, **_kwargs):
        assert abort_check()
        raise AbortedError('test deadline')

    monkeypatch.setattr(tasks, '_vlm_lane', lane)
    monkeypatch.setattr(tasks, 'vlm_parse_pdf', deadline_aware_parse)
    monkeypatch.setattr(tasks, 'vlm_task_timeout_seconds', lambda: 0)
    try:
        task_id = tasks.start_vlm_task(b'pdf', user_id=1)
        task = _wait_for_task(tasks, task_id, 'error')
        assert task['error']['kind'] == 'timeout'
        assert task['error']['retryable'] is True
    finally:
        lane.shutdown(wait=True, cancel_pending=True)


def test_terminal_registry_has_ttl_and_count_caps(
        clean_vlm_registry, monkeypatch):
    tasks = clean_vlm_registry
    now = time.time()
    with tasks._vlm_lock:
        for index in range(4):
            tasks._vlm_tasks[f'done-{index}'] = {
                'status': 'done',
                'created': now - 100,
                'finished': now - index,
                'user_id': 1,
                'filename': 'done.pdf',
                'progress': '1/1',
            }
        tasks._vlm_tasks['active'] = {
            'status': 'processing',
            'created': now - 10_000,
            'user_id': 1,
            'filename': 'active.pdf',
            'progress': 'queued',
        }
    monkeypatch.setattr(tasks, '_RESULT_CAPACITY', 2)
    monkeypatch.setattr(tasks, '_TASK_TTL', 1000)

    assert tasks._cleanup_old_tasks() == 2
    assert set(tasks._vlm_tasks) == {'done-0', 'done-1', 'active'}

    monkeypatch.setattr(tasks, '_TASK_TTL', 0)
    assert tasks._cleanup_old_tasks() == 2
    assert set(tasks._vlm_tasks) == {'active'}


def test_parser_caps_page_batches_and_propagates_attempt_budget(monkeypatch):
    from lib.pdf_parser.vlm import _parse

    rendered = []
    active = 0
    peak = 0
    lock = threading.Lock()
    attempts = []

    def fake_render(_pdf_bytes, *, dpi, max_pages, abort_check):
        rendered.append((dpi, max_pages, abort_check))
        return [b'p0', b'p1', b'p2', b'p3']

    def fake_call(images, label, model, max_tokens, *, abort_check,
                  max_429_attempts):
        nonlocal active, peak
        del images, model, max_tokens, abort_check
        attempts.append(max_429_attempts)
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return label

    monkeypatch.setattr(_parse, 'render_pdf_pages', fake_render)
    monkeypatch.setattr(_parse, '_get_vlm_models', lambda: ['vision-model'])
    monkeypatch.setattr(_parse, '_vlm_call_pages', fake_call)
    monkeypatch.setattr(_parse, 'vlm_call_workers', lambda: 2)
    monkeypatch.setattr(_parse, 'vlm_max_pages', lambda: 128)
    monkeypatch.setattr(_parse, 'vlm_max_429_attempts', lambda: 5)

    result = _parse.vlm_parse_pdf(
        b'pdf', batch_pages=1, max_workers=99, max_pages=999)

    assert rendered == [(150, 128, None)]
    assert peak == 2
    assert attempts == [5, 5, 5, 5]
    assert result.split('\n\n') == ['p.1', 'p.2', 'p.3', 'p.4']


def test_renderer_rejects_page_limit_before_first_pixmap(monkeypatch):
    from lib.pdf_parser.images import _render

    touched_pages = []

    class FakeDocument:
        def __len__(self):
            return 129

        def __getitem__(self, index):
            touched_pages.append(index)
            raise AssertionError('page allocation must not begin')

        def close(self):
            pass

    class FakePyMuPdf:
        @staticmethod
        def open(**_kwargs):
            return FakeDocument()

    monkeypatch.setattr(_render, 'pymupdf', FakePyMuPdf())

    with pytest.raises(_render.PdfPageLimitExceeded, match='129 pages'):
        _render.render_pdf_pages(b'pdf', max_pages=128)
    assert touched_pages == []


def test_vlm_wire_call_passes_abort_and_actual_429_attempt_budget(monkeypatch):
    from lib.pdf_parser.vlm import _parse
    import lib.llm_dispatch as dispatch

    captured = {}
    abort_check = lambda: False

    def fake_smart_chat(**kwargs):
        captured.update(kwargs)
        return 'markdown', {}

    monkeypatch.setattr(dispatch, 'smart_chat', fake_smart_chat)

    assert _parse._vlm_call_pages(
        [b'image'],
        'p.1',
        'vision-model',
        abort_check=abort_check,
        max_429_attempts=7,
    ) == 'markdown'
    assert captured['abort_check'] is abort_check
    assert captured['max_429_attempts'] == 7


def test_vlm_operator_overrides_remain_inside_domain_hard_ceilings(monkeypatch):
    from lib.pdf_parser.vlm import _policy

    monkeypatch.setenv('TOFU_PDF_VLM_TASK_WORKERS', '999999')
    monkeypatch.setenv('TOFU_PDF_VLM_QUEUE_CAPACITY', '999999')
    monkeypatch.setenv('TOFU_PDF_VLM_CALL_WORKERS', '999999')
    monkeypatch.setenv('TOFU_PDF_VLM_MAX_PAGES', '999999')
    monkeypatch.setenv('TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS', '999999')
    monkeypatch.setenv('TOFU_PDF_VLM_MAX_429_ATTEMPTS', '999999')
    monkeypatch.setenv('TOFU_PDF_VLM_WORKER_IDLE_SECONDS', '0')

    assert _policy.vlm_task_workers() == 16
    assert _policy.vlm_queue_capacity() == 256
    assert _policy.vlm_call_workers() == 16
    assert _policy.vlm_max_pages() == 2048
    assert _policy.vlm_task_timeout_seconds() == 86_400
    assert _policy.vlm_max_429_attempts() == 64
    assert _policy.vlm_worker_idle_seconds() == 0


def test_retained_frontend_cancels_removed_vlm_attachment():
    from tests._runtime_sections import runtime_section

    api = runtime_section('api.js')
    upload = runtime_section('upload.js')

    assert "vlmCancel: (taskId)" in api
    assert "method: 'DELETE'" in api
    assert 'Api.pdf.vlmCancel(entry._vlmTaskId)' in upload
    assert 'entry._vlmAlive = false' in upload
