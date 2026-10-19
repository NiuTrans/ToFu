"""Actual pinned HTML HTTP ownership over the configured native test authority.

Run with TOFU_NATIVE_HTML_TEST_INSTALLATION and a source-matched TOFUDB_BIN.
The standard application fixture owns disposable data; no repository/private
corpus replacement participates in upload, failure, restart or deletion.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def native_http_authority(request):
    from runtime_guards import load_deployment_configuration

    installation = os.environ.get('TOFU_NATIVE_HTML_TEST_INSTALLATION')
    if not installation or load_deployment_configuration().storage_backend != 'tofudb':
        pytest.skip('explicit native HTML installation and native test storage required')
    request.getfixturevalue('flask_app')
    from lib.storage import storage_status
    assert storage_status()['ready'] and storage_status()['backend'] == 'tofudb'
    return json.loads(Path(installation).read_text())


@pytest.mark.auth_mode('open')
def test_native_html_http_exact_sources_reindex_failure_and_storage_restart(
        native_http_authority, tmp_path):
    from lib import knowledge
    from lib.knowledge.native_html import NativeHtmlProjector, NativeHtmlProjectionError
    from lib.storage import StorageError, start_storage, stop_storage, storage_status
    from tests.knowledge.test_html_application_http import app_for, payload, upload
    from tests.knowledge.test_html_projection_port import documents

    receipt = native_http_authority
    bindings, retained, drafts = [], [], []
    refuse = False
    native_calls = 0

    def factory():
        binding = NativeHtmlProjector.from_library(
            library=Path(receipt['html_library']), sha256=receipt['html_library_sha256'],
            binary_bytes=receipt['bytes'])
        native_project = binding.project

        def project(value):
            nonlocal native_calls
            if refuse:
                raise RuntimeError('explicit native parser failure')
            result = native_project(value)
            native_calls += 1
            return result

        binding.project = project
        bindings.append(binding)
        return binding

    async def ingest():
        nonlocal refuse
        app = app_for(tmp_path, factory)
        async with app.test_app():
            client = app.test_client()
            expected_calls = 0
            for raw, filename, projections in documents():
                response = await client.post('/api/v4/knowledge/documents',
                                             files=upload(raw, filename))
                assert response.status_code == 200
                result = await payload(response)
                assert not result['errors'] and len(result['indexed']) == 1
                document_id = result['indexed'][0]['id']
                path = f'/api/v4/knowledge/documents/{document_id}'
                response = await client.post(path + '/reindex')
                assert response.status_code == 200
                expected_calls += 2 * projections
                assert native_calls == expected_calls
                before = await payload(await client.get(path + '/content'))
                assert 'Exact source' in '\n'.join(row['content'] for row in before['chunks'])
                # The configured native authority is scoped to the real test
                # owner. A foreign principal is refused before document lookup.
                with pytest.raises(StorageError) as denied:
                    knowledge.get_document_content(document_id, user_id=2)
                assert denied.value.code == 'database_forbidden'
                assert knowledge.read_source_path(document_id, user_id=1).read_bytes() == raw
                refuse = True
                response = await client.post(path + '/reindex')
                assert response.status_code == 500
                refuse = False
                assert native_calls == expected_calls
                assert await payload(await client.get(path + '/content')) == before
                retained.append((document_id, raw, before))
            raw = b'<h1>Native draft source</h1><p>attachment &amp; exact bytes</p>'
            response = await client.post('/api/v4/media/attachments',
                                         files=upload(raw, 'draft.html'))
            assert response.status_code == 200 and native_calls == expected_calls + 1
            document_id = (await payload(response))['attachment']['attachmentId']
            source = await client.get(f'/api/v4/media/attachments/{document_id}/source')
            assert source.status_code == 200 and await source.get_data() == raw
            assert source.headers['Cache-Control'] == 'private, no-store'
            assert knowledge.get_document_metadata(document_id, user_id=1)['scope'] == 'draft'
            drafts.append((document_id, raw))
            assert len(bindings) == 1
        with pytest.raises(NativeHtmlProjectionError, match='closed'):
            bindings[0].project('<p>closed native owner</p>')

    asyncio.run(ingest())
    stop_storage()
    start_storage()
    assert storage_status()['ready'] and storage_status()['backend'] == 'tofudb'

    async def recover_and_delete():
        app = app_for(tmp_path)
        async with app.test_app():
            client = app.test_client()
            for document_id, raw, expected in retained:
                path = f'/api/v4/knowledge/documents/{document_id}'
                assert await payload(await client.get(path + '/content')) == expected
                assert knowledge.read_source_path(document_id, user_id=1).read_bytes() == raw
                response = await client.delete(path)
                assert response.status_code == 200
                assert knowledge.get_document_content(document_id, user_id=1) is None
                assert knowledge.read_source_path(document_id, user_id=1) is None
            for document_id, raw in drafts:
                path = f'/api/v4/media/attachments/{document_id}'
                source = await client.get(path + '/source')
                assert source.status_code == 200 and await source.get_data() == raw
                assert knowledge.get_document_metadata(document_id, user_id=1)['scope'] == 'draft'
                response = await client.delete(path)
                assert response.status_code == 200
                assert knowledge.get_document_metadata(document_id, user_id=1) is None
                assert knowledge.read_source_path(document_id, user_id=1) is None
        assert len(bindings) == 1

    asyncio.run(recover_and_delete())
