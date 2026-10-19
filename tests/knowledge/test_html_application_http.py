"""Real owner-scoped upload/reindex/attachment routes borrow one app parser."""
import asyncio
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import threading

import pytest
from werkzeug.datastructures import FileStorage

from lib.app.app_assembly import create_application
from lib.knowledge.native_html_service import HtmlProjectionClosed

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)


def app_for(tmp_path, factory=None):
    return create_application(
        'html-http', static_dir=str(tmp_path), logger=logging.getLogger('html-http'),
        secret_key='fixture-secret', config={'TESTING': True},
        native_html_factory=factory,
    )


def upload(raw, name):
    return {'file': FileStorage(stream=io.BytesIO(raw), filename=name)}


async def payload(response):
    body = await response.get_json()
    assert set(body) == {'data', 'meta'}
    return body['data']


@pytest.fixture(params=['python-fixture', 'native-installed'])
def binding_factory(request):
    loaded = []
    if request.param == 'native-installed':
        installation = os.environ.get('TOFU_NATIVE_HTML_TEST_INSTALLATION')
        if not installation:
            pytest.skip('explicit installed native HTML receipt required')
        receipt = json.loads(Path(installation).read_text())

    def factory():
        if request.param == 'native-installed':
            from lib.knowledge.native_html import NativeHtmlProjector
            binding = NativeHtmlProjector.from_library(
                library=Path(receipt['html_library']),
                sha256=receipt['html_library_sha256'], binary_bytes=receipt['bytes'])
        else:
            from lib.knowledge.html_text import extract_html_text

            class PythonBinding:
                profile = {'test_reference': True}

                def project(self, value):
                    return extract_html_text(value)

                def close(self):
                    pass

            binding = PythonBinding()
        loaded.append(binding)
        return binding

    return factory, loaded


@pytest.mark.auth_mode('open')
def test_http_upload_nested_attachment_and_reindex_share_owned_parser(
        isolated_knowledge, tmp_path, binding_factory):
    from tests.knowledge.test_html_projection_port import documents

    factory, loaded = binding_factory
    app = app_for(tmp_path, factory)
    owner = app.extensions['tofu_html_projection']

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            # A non-HTML document does not initialize the optional capability.
            plain = await client.post('/api/v4/media/attachments',
                                      files=upload(b'plain evidence', 'plain.txt'))
            assert plain.status_code == 200 and not loaded
            for raw, filename, _ in documents():
                response = await client.post('/api/v4/knowledge/documents',
                                             files=upload(raw, filename))
                assert response.status_code == 200
                result = await payload(response)
                assert not result['errors'] and len(result['indexed']) == 1
                document_id = result['indexed'][0]['id']
                content = await client.get(f'/api/v4/knowledge/documents/{document_id}/content')
                assert content.status_code == 200
                parsed = await payload(content)
                assert 'Exact source' in '\n'.join(row['content'] for row in parsed['chunks'])
                assert isolated_knowledge.get_document_content(document_id, user_id=2) is None
                reindexed = await client.post(f'/api/v4/knowledge/documents/{document_id}/reindex')
                assert reindexed.status_code == 200
                assert (await payload(reindexed))['reindexed']['id'] == document_id
            raw = b'<h1>Draft source</h1><p>attachment &amp; exact bytes</p>'
            response = await client.post('/api/v4/media/attachments',
                                         files=upload(raw, 'draft.html'))
            assert response.status_code == 200
            document_id = (await payload(response))['attachment']['attachmentId']
            source = await client.get(f'/api/v4/media/attachments/{document_id}/source')
            assert source.status_code == 200 and await source.get_data() == raw
            assert source.headers['Cache-Control'] == 'private, no-store'
            assert isolated_knowledge.get_document_metadata(document_id, user_id=1)['scope'] == 'draft'
            assert len(loaded) == 1 and owner.profile == loaded[0].profile

    asyncio.run(exercise())
    with pytest.raises(HtmlProjectionClosed):
        owner.project('closed')
    if 'native_sources' in loaded[0].profile:
        from lib.knowledge.native_html import NativeHtmlProjectionError
        with pytest.raises(NativeHtmlProjectionError, match='closed'):
            loaded[0].project('<p>unloaded</p>')


@pytest.mark.auth_mode('private')
def test_auth_rejection_never_initializes_the_parser(isolated_knowledge, tmp_path):
    loaded = []

    def forbidden_factory():
        loaded.append(True)
        raise AssertionError('unauthorized HTML load')

    app = app_for(tmp_path, forbidden_factory)

    async def exercise():
        async with app.test_app():
            for endpoint in ('knowledge/documents', 'media/attachments'):
                response = await app.test_client().post(
                    '/api/v4/' + endpoint, files=upload(b'<h1>private</h1>', 'private.html'))
                assert response.status_code in (401, 403)

    asyncio.run(exercise())
    assert not loaded and isolated_knowledge.get_status(user_id=1)['totals']['documents'] == 0


@pytest.mark.auth_mode('open')
def test_failed_native_admission_preserves_error_and_default_application(
        isolated_knowledge, tmp_path):
    loaded = []

    def refused():
        loaded.append(True)
        raise RuntimeError('explicit pinned image refused')

    app = app_for(tmp_path, refused)
    default = app_for(tmp_path)

    async def exercise():
        async with app.test_app(), default.test_app():
            for endpoint in ('knowledge/documents', 'media/attachments'):
                response = await app.test_client().post(
                    '/api/v4/' + endpoint, files=upload(b'<p>refused</p>', 'refused.html'))
                if endpoint == 'knowledge/documents':
                    result = await payload(response)
                    assert response.status_code == 200 and not result['indexed']
                    assert result['errors'] == [{'name': 'refused.html', 'error': 'Unexpected indexing failure'}]
                else:
                    assert response.status_code == 500
            assert loaded == [True]
            assert isolated_knowledge.get_status(user_id=1)['totals']['documents'] == 0
            response = await default.test_client().post(
                '/api/v4/knowledge/documents', files=upload(b'<p>default still works</p>', 'default.html'))
            assert response.status_code == 200 and not (await payload(response))['errors']
            assert 'tofu_html_projection' not in default.extensions

    asyncio.run(exercise())


@pytest.mark.auth_mode('open')
def test_reindex_refusal_keeps_previous_visible_evidence(
        isolated_knowledge, tmp_path, binding_factory):
    factory, loaded = binding_factory
    refused = threading.Event()

    def selected():
        binding = factory()
        project = binding.project

        def checked(value):
            if refused.is_set():
                raise RuntimeError('explicit projection refused')
            return project(value)

        binding.project = checked
        return binding

    app = app_for(tmp_path, selected)
    raw = b'<h1>Retained evidence</h1><p>original bytes</p>'

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            created = await client.post('/api/v4/knowledge/documents', files=upload(raw, 'retained.html'))
            document_id = (await payload(created))['indexed'][0]['id']
            path = f'/api/v4/knowledge/documents/{document_id}'
            before = await payload(await client.get(path + '/content'))
            refused.set()
            response = await client.post(path + '/reindex')
            assert response.status_code == 500
            after = await payload(await client.get(path + '/content'))
            assert before == after
            assert isolated_knowledge.read_source_path(document_id, user_id=1).read_bytes() == raw
            assert len(loaded) == 1

    asyncio.run(exercise())


@pytest.mark.auth_mode('open')
@pytest.mark.parametrize('endpoint', ['knowledge/documents', 'media/attachments'])
def test_http_disconnect_joins_accepted_ingestion_before_releasing_request(
        isolated_knowledge, tmp_path, binding_factory, endpoint):
    factory, loaded = binding_factory
    entered, release = threading.Event(), threading.Event()

    def blocked_factory():
        binding = factory()
        project = binding.project

        def blocked(value):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test did not release accepted parser')
            return project(value)

        binding.project = blocked
        return binding

    app = app_for(tmp_path, blocked_factory)
    raw = b'<h1>Disconnect evidence</h1><p>complete accepted upload</p>'
    boundary = 'owned-html-disconnect'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="disconnect.html"\r\nContent-Type: text/html\r\n\r\n').encode()
    body += raw + f'\r\n--{boundary}--\r\n'.encode()

    async def exercise():
        async with app.test_app():
            connection = app.test_client().request(
                '/api/v4/' + endpoint, method='POST',
                headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
            try:
                async with connection:
                    try:
                        await connection.send(body)
                        await connection.send_complete()
                        assert await asyncio.to_thread(entered.wait, 5)
                        await connection.disconnect()
                        done, _ = await asyncio.wait({connection._task}, timeout=0.05)
                        assert not done, 'request abandoned its active synchronous ingestion'
                    finally:
                        release.set()
            finally:
                release.set()
            from lib.knowledge.repository import KnowledgeRepository
            document = KnowledgeRepository(1).document_by_digest(hashlib.sha256(raw).hexdigest())
            assert document is not None
            document_id = document['id']
            assert isolated_knowledge.read_source_path(document_id, user_id=1).read_bytes() == raw
            page = isolated_knowledge.get_document_content(document_id, user_id=1)
            assert 'Disconnect evidence' in '\n'.join(row['content'] for row in page['chunks'])
            assert len(loaded) == 1

    asyncio.run(exercise())
