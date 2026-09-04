"""Owner-scoped report lookup/cache resolution contract."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest
from quart import Quart

from lib.paper.artifact_repository import PaperReport, PaperReportReopen
from lib.paper.report_artifact_keys import (
    checkpoints_lang_key,
    insight_lang_key,
    termfill_lang_key,
)
from routes.paper_pkg import _report as report_routes


pytestmark = pytest.mark.unit


async def _invoke_lookup(app: Quart, payload: dict):
    async with app.test_request_context(
        '/api/v1/paper/report/lookup', method='POST', json=payload,
    ):
        raw_response = await report_routes.lookup_report_task()
        response = await app.make_response(raw_response)
        return await response.get_json()


async def _invoke_route(app: Quart, path: str, handler, payload: dict):
    async with app.test_request_context(path, method='POST', json=payload):
        raw_response = await handler()
        response = await app.make_response(raw_response)
        return await response.get_json()


def test_lookup_fuses_fallback_cache_into_one_owner_query(monkeypatch):
    app = Quart(__name__)
    repository_calls = []
    projection_calls = []

    class _Repository:
        def reopen_report(
            self, paper_hash, preferred_lang, fallback_lang, *,
            sibling_langs_by_base,
        ):
            repository_calls.append(
                (paper_hash, preferred_lang, fallback_lang,
                 sibling_langs_by_base))
            return PaperReportReopen(report=PaperReport(
                paper_hash, fallback_lang or preferred_lang, 'cached report',
                meta={'source': 'cache'}))

    def _project(row, paper_hash, lang, *, user_id, siblings, images=None):
        projection_calls.append(
            (row.lang, paper_hash, lang, user_id, siblings, images))
        return {
            'cached': True,
            'paper_hash': paper_hash,
            'lang': lang,
            'report': row.report,
            'meta': row.meta,
        }

    monkeypatch.setattr(report_routes, 'request_user_id', lambda: 73)
    monkeypatch.setattr(report_routes, '_report_index_get', lambda *_a, **_k: None)
    monkeypatch.setattr(
        report_routes, 'PaperArtifactRepository',
        lambda owner: _Repository() if owner == 73 else None,
    )
    monkeypatch.setattr(report_routes, '_cached_report_payload', _project)

    result = asyncio.run(_invoke_lookup(app, {
        'paper_hash': 'paper-hash',
        'lang': 'zh',
        'include_cache': True,
    }))

    assert result == {
        'ok': True,
        'cached': True,
        'paper_hash': 'paper-hash',
        'lang': 'en',
        'report': 'cached report',
        'meta': {'source': 'cache'},
    }
    assert repository_calls == [(
        'paper-hash', 'zh', 'en',
        {
            'zh': ('insight:zh', 'termfill:zh', 'checkpoints:zh'),
            'en': ('insight:en', 'termfill:en', 'checkpoints:en'),
        },
    )]
    assert projection_calls == [('en', 'paper-hash', 'en', 73, {}, None)]

    monkeypatch.setattr(report_routes, '_paper_hash', lambda text: f'hash:{len(text)}')
    review = asyncio.run(_invoke_lookup(app, {
        'paper_text': 'full paper text',
        'lang': 'review:neurips:en',
        'include_cache': True,
    }))
    assert review['ok'] and review['lang'] == 'review:neurips:en'
    assert repository_calls[-1] == (
        'hash:15', 'review:neurips:en', None, {})

    rebuttal = asyncio.run(_invoke_lookup(app, {
        'paper_hash': 'paper-hash',
        'lang': 'rebuttal:neurips:en',
        'include_cache': True,
    }))
    assert rebuttal['ok'] and rebuttal['lang'] == 'rebuttal:neurips:en'
    assert repository_calls[-1] == (
        'paper-hash', 'rebuttal:neurips:en', None, {})


def test_lookup_preserves_live_precedence_and_legacy_zero_read(monkeypatch):
    app = Quart(__name__)
    repository_owners = []

    class _Repository:
        def __init__(self, owner):
            repository_owners.append(owner)

        def reopen_report(self, *_args, **_kwargs):
            raise AssertionError('live/legacy lookup must not read report storage')

    monkeypatch.setattr(report_routes, 'request_user_id', lambda: 91)
    monkeypatch.setattr(report_routes, 'PaperArtifactRepository', _Repository)
    monkeypatch.setattr(
        report_routes,
        '_report_index_get',
        lambda *_a, **_k: {
            'task_id': 'task-running',
            'status': 'running',
        },
    )
    live = asyncio.run(_invoke_lookup(app, {
        'paper_hash': 'paper-hash',
        'lang': 'en',
        'include_cache': True,
    }))
    assert live == {
        'ok': True,
        'task_id': 'task-running',
        'status': 'running',
        'paper_hash': 'paper-hash',
    }
    assert repository_owners == []

    monkeypatch.setattr(report_routes, '_report_index_get', lambda *_a, **_k: None)
    legacy = asyncio.run(_invoke_lookup(app, {
        'paper_hash': 'paper-hash',
        'lang': 'review:neurips:en',
    }))
    assert legacy == {'ok': False}
    assert repository_owners == []


def test_reopen_bundle_projects_every_sibling_without_hidden_reads(monkeypatch):
    calls = []
    paper_hash = 'paper-bundle'
    base = PaperReport(
        paper_hash, 'en', '# Paper\n\nBase body.',
        meta={'terminologyAudit': {'missing': ['SFT']}},
    )
    siblings = {
        insight_lang_key('en'): PaperReport(
            paper_hash,
            insight_lang_key('en'),
            '## Insight markdown',
            meta={'items': {'anchor': {'summary': 'grounded'}}},
        ),
        termfill_lang_key('en'): PaperReport(
            paper_hash,
            termfill_lang_key('en'),
            '## Added definitions\n\nSFT means supervised fine-tuning.',
        ),
        checkpoints_lang_key('en'): PaperReport(
            paper_hash,
            checkpoints_lang_key('en'),
            '',
            meta={'items': [{'question': 'Why?', 'answer': 'Because.'}]},
        ),
    }

    class _Repository:
        def reopen_report(
            self, offered_hash, preferred_lang, fallback_lang, *,
            sibling_langs_by_base,
        ):
            calls.append(
                (offered_hash, preferred_lang, fallback_lang,
                 sibling_langs_by_base))
            return PaperReportReopen(report=base, siblings=siblings)

        def get_report(self, *_args, **_kwargs):
            raise AssertionError('bundle projection performed a hidden row read')

    monkeypatch.setattr(
        report_routes, 'inject_images_into_report',
        lambda body, *_args, **_kwargs: body,
    )
    monkeypatch.setattr(
        report_routes, 'ensure_title_heading',
        lambda body, *_args, **_kwargs: body,
    )
    payload = report_routes._resolve_cached_report_payload(
        _Repository(),
        paper_hash,
        'zh',
        user_id=73,
        fallback_lang='en',
        images=[],
    )

    assert len(calls) == 1
    assert calls[0][3] == {
        'zh': ('insight:zh', 'termfill:zh', 'checkpoints:zh'),
        'en': ('insight:en', 'termfill:en', 'checkpoints:en'),
    }
    assert payload['lang'] == 'en'
    assert payload['insight']['items']['anchor']['summary'] == 'grounded'
    assert payload['checkpoints']['items'][0]['question'] == 'Why?'
    assert 'Added definitions' in payload['report']
    assert payload['meta']['terminologyAudit'] is None


def test_start_and_cache_routes_share_the_single_reopen_owner(monkeypatch):
    app = Quart(__name__)
    calls = []
    repository = object()

    def _resolve(artifacts, phash, lang, **kwargs):
        calls.append((artifacts, phash, lang, kwargs))
        return {
            'cached': True,
            'paper_hash': phash,
            'lang': lang,
            'report': 'cached body',
            'meta': {},
        }

    monkeypatch.setattr(report_routes, 'request_user_id', lambda: 73)
    monkeypatch.setattr(
        report_routes, 'PaperArtifactRepository', lambda owner: repository)
    monkeypatch.setattr(report_routes, '_resolve_cached_report_payload', _resolve)
    monkeypatch.setattr(report_routes, 'load_image_manifest', lambda _hash: [])
    monkeypatch.setattr(
        report_routes,
        'PaperLibraryRepository',
        lambda _owner: (_ for _ in ()).throw(
            AssertionError('hash cache hit must not load the paper body')),
    )
    monkeypatch.setattr(
        report_routes,
        'paper_request_policy_telemetry',
        lambda **_kwargs: {
            'executionFingerprint': 'fingerprint',
            'cacheMode': 'canonical',
        },
    )
    paper_hash = 'a' * 32
    started = asyncio.run(_invoke_route(
        app,
        '/api/v1/paper/report/start',
        report_routes.start_report_task,
        {'paper_hash': paper_hash, 'lang': 'en'},
    ))
    cached = asyncio.run(_invoke_route(
        app,
        '/api/v1/paper/report/cache',
        report_routes.get_report_cache,
        {'paper_hash': paper_hash, 'lang': 'en'},
    ))

    assert started['ok'] and started['cached']
    assert cached['ok'] and cached['cached']
    assert len(calls) == 2
    assert calls[0][0] is repository and calls[0][3]['repair_library_title'] is True
    assert calls[1][0] is repository and 'repair_library_title' not in calls[1][3]


def test_hash_start_joins_live_work_before_cache_images_or_source(monkeypatch):
    app = Quart(__name__)
    paper_hash = 'b' * 32
    monkeypatch.setattr(report_routes, 'request_user_id', lambda: 73)
    monkeypatch.setattr(
        report_routes, 'PaperArtifactRepository', lambda _owner: object())
    monkeypatch.setattr(
        report_routes,
        'paper_request_policy_telemetry',
        lambda **_kwargs: {
            'executionFingerprint': 'fingerprint',
            'cacheMode': 'canonical',
        },
    )
    monkeypatch.setattr(
        report_routes,
        '_report_index_get',
        lambda *_args, **_kwargs: {
            'task_id': 'task-live', 'status': 'running',
        },
    )
    monkeypatch.setattr(
        report_routes,
        'load_image_manifest',
        lambda _hash: (_ for _ in ()).throw(
            AssertionError('live join must not load images')),
    )
    monkeypatch.setattr(
        report_routes,
        '_resolve_cached_report_payload',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('live join must not read cache')),
    )
    monkeypatch.setattr(
        report_routes,
        'PaperLibraryRepository',
        lambda _owner: (_ for _ in ()).throw(
            AssertionError('live join must not read source')),
    )

    result = asyncio.run(_invoke_route(
        app,
        '/api/v1/paper/report/start',
        report_routes.start_report_task,
        {'paper_hash': paper_hash, 'lang': 'en'},
    ))

    assert result == {
        'ok': True,
        'task_id': 'task-live',
        'paper_hash': paper_hash,
        'running': True,
        'existed': True,
    }


def test_hash_start_reads_only_bounded_owned_source_after_fast_path_miss(
    monkeypatch,
):
    app = Quart(__name__)
    paper_hash = 'c' * 32
    identity_calls = []
    task_calls = []
    spawn_calls = []

    class _Library:
        def __init__(self, owner):
            assert owner == 73

        def identity(self, offered_hash, *, max_text_chars=None):
            identity_calls.append((offered_hash, max_text_chars))
            return SimpleNamespace(
                title='Stored title',
                parsed_text='source evidence ' * 8_000,
                parsed_text_length=300_000,
            )

    def _new_task(*args, **kwargs):
        task_calls.append((args, kwargs))
        return {'task_id': args[0]}

    monkeypatch.setattr(report_routes, 'request_user_id', lambda: 73)
    monkeypatch.setattr(
        report_routes, 'PaperArtifactRepository', lambda _owner: object())
    monkeypatch.setattr(report_routes, 'PaperLibraryRepository', _Library)
    monkeypatch.setattr(report_routes, '_report_index_get', lambda *_a, **_k: None)
    monkeypatch.setattr(report_routes, 'load_image_manifest', lambda _hash: [])
    monkeypatch.setattr(
        report_routes, '_resolve_cached_report_payload',
        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        report_routes,
        'paper_request_policy_telemetry',
        lambda **_kwargs: {
            'executionFingerprint': 'fingerprint',
            'cacheMode': 'canonical',
        },
    )
    monkeypatch.setattr(report_routes, '_new_report_task', _new_task)
    monkeypatch.setattr(
        report_routes,
        '_report_runtime',
        SimpleNamespace(spawn=lambda *args: spawn_calls.append(args)),
    )

    result = asyncio.run(_invoke_route(
        app,
        '/api/v1/paper/report/start',
        report_routes.start_report_task,
        {'paper_hash': paper_hash, 'lang': 'en'},
    ))

    assert result['ok'] and result['running'] and not result['existed']
    assert identity_calls == [(paper_hash, 120_000)]
    assert len(task_calls) == 1 and task_calls[0][1]['client_title'] == 'Stored title'
    assert len(spawn_calls) == 1


def test_hash_force_missing_source_preserves_existing_task(monkeypatch):
    app = Quart(__name__)
    paper_hash = 'd' * 32
    abort_calls = []
    existing = {
        'task_id': 'task-existing',
        'status': 'running',
        'abort_event': SimpleNamespace(set=lambda: abort_calls.append(True)),
    }

    class _Library:
        def __init__(self, owner):
            assert owner == 73

        def identity(self, offered_hash, *, max_text_chars=None):
            assert (offered_hash, max_text_chars) == (paper_hash, 120_000)
            return None

    monkeypatch.setattr(report_routes, 'request_user_id', lambda: 73)
    monkeypatch.setattr(
        report_routes, 'PaperArtifactRepository', lambda _owner: object())
    monkeypatch.setattr(report_routes, 'PaperLibraryRepository', _Library)
    monkeypatch.setattr(
        report_routes, '_report_index_get', lambda *_a, **_k: existing)
    monkeypatch.setattr(report_routes, 'load_image_manifest', lambda _hash: [])
    monkeypatch.setattr(
        report_routes,
        'paper_request_policy_telemetry',
        lambda **_kwargs: {
            'executionFingerprint': 'fingerprint',
            'cacheMode': 'canonical',
        },
    )

    result = asyncio.run(_invoke_route(
        app,
        '/api/v1/paper/report/start',
        report_routes.start_report_task,
        {'paper_hash': paper_hash, 'lang': 'en', 'force': True},
    ))

    assert result['ok'] is False
    assert result['error_code'] == 'paper_source_required'
    assert abort_calls == []
    assert existing['status'] == 'running'


def test_cache_key_contract_does_not_import_generation_engines():
    probe = subprocess.run(
        [
            sys.executable,
            '-c',
            (
                'import sys; import routes.paper_pkg._common; '
                'blocked = ('
                '"lib.paper.checkpoint_engine", '
                '"lib.paper.terminology_backfill", '
                '"lib.paper.insight_engine._run"); '
                'assert not [name for name in blocked if name in sys.modules]'
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr or probe.stdout
