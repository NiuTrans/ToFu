"""Kubernetes liveness, readiness, and startup probe contracts."""

from __future__ import annotations

import asyncio

import pytest

from lib.app_factory import create_base_app


pytestmark = pytest.mark.unit


def _status(result) -> int:
    return int(result[1])


def test_liveness_is_low_sensitivity_and_dependency_independent():
    from routes import common

    app = create_base_app('probe-live', {'TESTING': True})

    async def exercise():
        async with app.test_request_context('/health/live'):
            response, status = common.liveness_check()
            payload = await response.get_json()
            assert status == 200
            assert payload == {'ok': True, 'status': 'live'}

    asyncio.run(exercise())


def test_readiness_and_startup_require_lifecycle_and_storage(monkeypatch):
    from routes import common

    app = create_base_app('probe-gates', {'TESTING': True})
    app.extensions['tofu_production_lifecycle'] = {
        'status': 'starting',
        'process_role': 'worker',
    }
    monkeypatch.setattr(
        common, '_storage_authority_status', lambda: {'ready': True})

    async def exercise():
        async with app.test_request_context('/health/ready'):
            assert _status(common.orchestrator_readiness_check()) == 503
        async with app.test_request_context('/health/startup'):
            assert _status(common.startup_check()) == 503

        app.extensions['tofu_production_lifecycle']['status'] = 'ready'
        async with app.test_request_context('/health/ready'):
            response, status = common.orchestrator_readiness_check()
            payload = await response.get_json()
            assert status == 200
            assert payload['ready'] is True
            assert payload['processRole'] == 'worker'
            assert payload['dependencies'] == {'storage': True}
        async with app.test_request_context('/health/startup'):
            assert _status(common.startup_check()) == 200

    asyncio.run(exercise())


def test_quiescence_reports_running_task_count(monkeypatch):
    from routes import common

    app = create_base_app('probe-quiescence', {'TESTING': True})
    rows = [{'taskId': 't1'}]
    monkeypatch.setattr(
        'lib.tasks_pkg.manager.list_running_tasks', lambda: list(rows))

    async def exercise():
        async with app.test_request_context('/health/quiescence'):
            response, status = common.quiescence_check()
            payload = await response.get_json()
            assert status == 200
            assert payload['quiet'] is False
            assert payload['runningTasks'] == 1
            rows.clear()
            response, status = common.quiescence_check()
            payload = await response.get_json()
            assert status == 200
            assert payload['quiet'] is True
            assert payload['runningTasks'] == 0

    asyncio.run(exercise())


def test_quiescence_registry_failure_reports_busy(monkeypatch):
    from routes import common

    app = create_base_app('probe-quiescence-error', {'TESTING': True})

    def _boom():
        raise RuntimeError('registry down')

    monkeypatch.setattr('lib.tasks_pkg.manager.list_running_tasks', _boom)

    async def exercise():
        async with app.test_request_context('/health/quiescence'):
            response, status = common.quiescence_check()
            payload = await response.get_json()
            assert status == 200
            assert payload['quiet'] is False
            assert payload['runningTasks'] is None

    asyncio.run(exercise())
