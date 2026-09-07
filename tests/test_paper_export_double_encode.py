"""Paper export normalizes encoded composite language keys at the HTTP edge."""

from __future__ import annotations

import asyncio
import os

import pytest

from lib.paper.artifact_repository import PaperArtifactRepository, PaperReport


pytest_plugins = ('tests._artifact_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('open')]

_PAPER_HASH = 'e' * 64
_REVIEW_LANG = 'review:neurips:en'
_REVIEW_BODY = '# Test Paper\n\nStored review body.\n'


def _load_app():
    os.environ.setdefault('TOFU_AUTH_MODE', 'open')
    from lib.storage import start_storage
    import server

    start_storage()
    server.app.config['TESTING'] = True
    return server.app


def _seed_reports() -> None:
    repository = PaperArtifactRepository(1)
    assert repository.put_report(
        PaperReport(_PAPER_HASH, _REVIEW_LANG, _REVIEW_BODY),
        command_id='paper-export-seed-review',
    )
    assert repository.put_report(
        PaperReport(_PAPER_HASH, 'en', '# Plain report\n\nbody\n'),
        command_id='paper-export-seed-plain',
    )


def test_encoded_review_language_variants_export_the_same_report():
    app = _load_app()
    _seed_reports()
    base = (
        f'/api/v1/paper/report/export?paper_hash={_PAPER_HASH}'
        '&format=md&lang='
    )
    cases = (
        ('review%253Aneurips%253Aen', 200),
        ('review%3Aneurips%3Aen', 200),
        ('review:neurips:en', 200),
        ('en', 200),
        ('review%253Aiclr%253Aen', 404),
    )

    async def run():
        async with app.test_client() as client:
            for language, expected_status in cases:
                response = await client.get(base + language)
                assert response.status_code == expected_status
                if expected_status == 200:
                    assert await response.get_data()

    asyncio.run(run())


def test_malformed_encoded_language_never_becomes_a_server_error():
    app = _load_app()
    _seed_reports()

    async def run():
        async with app.test_client() as client:
            response = await client.get(
                f'/api/v1/paper/report/export?paper_hash={_PAPER_HASH}'
                '&format=md&lang=review%25ZZneurips'
            )
            assert response.status_code == 404

    asyncio.run(run())


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_storage

    guard_standalone_storage('test_paper_export_double_encode.standalone')
    raise SystemExit(pytest.main([__file__, '-v']))
