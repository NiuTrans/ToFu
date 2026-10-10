"""Contract tests for the native client support-report intake surface.

Covers the authenticated ``POST/GET /api/v1/mobile/client-report`` wiring and
the bounded JSONL store behind it (``lib.client_report_store``): an untrusted
report is clipped, stamped, persisted, and read back newest-first.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.api


def _app():
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import client_support as route

    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_client_support_bp)
    return app


def _store(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv('TOFU_CLIENT_REPORTS_DIR', str(tmp_path))


def test_post_stores_then_get_returns_newest_first(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _store(monkeypatch, tmp_path)

    async def _run():
        client = _app().test_client()
        first = await (await client.post('/api/v1/mobile/client-report', json={
            'reportId': 'r-1', 'note': 'first',
            'app': {'versionName': '0.1.17', 'versionCode': 17},
        })).get_json()
        second = await (await client.post('/api/v1/mobile/client-report', json={
            'reportId': 'r-2', 'note': 'second',
        })).get_json()
        listing = await (await client.get(
            '/api/v1/mobile/client-report')).get_json()
        return first, second, listing

    first, second, listing = asyncio.run(_run())
    assert first['ok'] is True and second['ok'] is True
    assert first['reportId'] == 'r-1'
    assert second['reportId'] == 'r-2'

    payload = listing.get('data', listing)
    reports = payload['reports']
    assert [r['reportId'] for r in reports] == ['r-2', 'r-1']
    newest = reports[0]
    assert newest['note'] == 'second'
    assert newest['schema'] == 'tofu.client-report/v1'
    assert isinstance(newest['receivedAt'], int)
    # Unknown/absent fields are never invented; known ones survive.
    assert reports[1]['app'] == {'versionName': '0.1.17', 'versionCode': 17}


def test_post_generates_report_id_when_absent(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _store(monkeypatch, tmp_path)

    async def _run():
        response = await _app().test_client().post(
            '/api/v1/mobile/client-report', json={'note': 'no id'})
        return await response.get_json()

    body = asyncio.run(_run())
    assert body['ok'] is True
    assert len(body['reportId']) == 12


def test_post_clips_oversized_note_events_and_web(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _store(monkeypatch, tmp_path)
    events = [
        {'t': i, 'lvl': 'I', 'tag': 'TofuWebConsole',
         'msg': 'x' * 5000}
        for i in range(400)
    ]

    async def _run():
        client = _app().test_client()
        posted = await (await client.post(
            '/api/v1/mobile/client-report',
            json={'note': 'n' * 9000, 'events': events,
                  'web': 'w' * 600_000})).get_json()
        listing = await (await client.get(
            '/api/v1/mobile/client-report?limit=1')).get_json()
        return posted, listing

    posted, listing = asyncio.run(_run())
    assert posted['truncated'] is True
    record = listing.get('data', listing)['reports'][0]
    assert record['truncated'] is True
    assert len(record['note']) == 8000
    assert len(record['web']) == 512 * 1024
    assert len(record['events']) == 300
    # The newest events are the ones kept.
    assert record['events'][-1]['t'] == 399
    assert len(record['events'][-1]['msg']) == 2000


def test_post_rejects_empty_body(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _store(monkeypatch, tmp_path)

    async def _run():
        response = await _app().test_client().post(
            '/api/v1/mobile/client-report', json={})
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 400
    assert body['ok'] is False


def test_get_limit_is_bounded(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _store(monkeypatch, tmp_path)

    async def _run():
        client = _app().test_client()
        for i in range(5):
            await client.post('/api/v1/mobile/client-report',
                              json={'reportId': f'r-{i}'})
        capped = await (await client.get(
            '/api/v1/mobile/client-report?limit=999')).get_json()
        return capped.get('data', capped)

    payload = asyncio.run(_run())
    assert len(payload['reports']) == 5


def test_store_rotates_and_prunes_segments(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from lib import client_report_store

    _store(monkeypatch, tmp_path)
    monkeypatch.setattr(client_report_store, '_MAX_SEGMENT_BYTES', 1)
    monkeypatch.setattr(client_report_store, '_MAX_SEGMENTS', 2)

    for i in range(6):
        client_report_store.append_report({'reportId': f's-{i}'})

    segments = sorted(tmp_path.glob('client-reports.*.jsonl'))
    assert len(segments) <= 2
    # The current segment is always present and readable.
    assert client_report_store.current_path().is_file()
    assert client_report_store.read_recent(1)[0]['reportId'] == 's-5'


def test_store_roundtrip_is_jsonl(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from lib import client_report_store

    _store(monkeypatch, tmp_path)
    client_report_store.append_report({'reportId': 'a', 'note': 'ü'})
    client_report_store.append_report({'reportId': 'b', 'note': 'two'})

    lines = client_report_store.current_path().read_text(
        encoding='utf-8').splitlines()
    assert [json.loads(line)['reportId'] for line in lines] == ['a', 'b']
    assert client_report_store.read_recent(5)[0]['reportId'] == 'b'
