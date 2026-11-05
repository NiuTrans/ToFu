"""Builtin faults behind chat, compatibility, review, paper and routing routes.

Only typed client faults answer 4xx. Each changed route has one representative
internal fault: a builtin raised inside its protected application call, which
these handlers used to answer as a 400 (or the paper review lookup as a 409)
carrying the raw text. Each also keeps its genuine client fault, with the
existing status and message. Collaborators are patched at their owning
modules, so no provider, browser, arXiv or model-routing authority is reached.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]

SECRET = 'internal wiring detail /srv/private/state'
MESSAGES = [{'role': 'user', 'content': 'hello'}]


def _raises(error):
    def fail(*_args, **_kwargs):
        raise error
    return fail


def _assert_redacted_500(response):
    body = response.get_data(as_text=True)
    assert response.status_code == 500, body
    assert SECRET not in body


def _assert_client_400(response, message, field):
    assert response.status_code == 400, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload['error'] == message
    assert payload['field'] == field


# ── OpenAI/Anthropic compatibility translators ─────────────────────

@pytest.mark.parametrize(('path', 'body'), [
    ('/v1/chat/completions', {'model': 'm', 'messages': MESSAGES}),
    ('/v1/chat/completions', {'model': 'm', 'messages': MESSAGES, 'tools': []}),
    ('/v1/messages', {'model': 'm', 'max_tokens': 8, 'messages': MESSAGES}),
    ('/v1/messages/count_tokens', {'model': 'm', 'messages': MESSAGES}),
], ids=['openai-task', 'openai-client-tools', 'anthropic', 'anthropic-count-tokens'])
def test_compat_translation_fault_is_a_redacted_500(flask_client, monkeypatch, path, body):
    import lib.agent_core.personal_scope as personal_scope

    monkeypatch.setattr(personal_scope, 'apply_headless_personal_defaults',
                        _raises(ValueError(SECRET)))
    _assert_redacted_500(flask_client.post(path, json=body))


@pytest.mark.parametrize(('path', 'body', 'message', 'field'), [
    ('/v1/chat/completions', {'messages': 'hello'}, 'messages must be an array', 'messages'),
    ('/v1/chat/completions', {'messages': MESSAGES, 'timeout_s': 'soon'},
     'timeout_s must be a number', 'timeout_s'),
    ('/v1/chat/completions', {'messages': MESSAGES, 'tools': {}},
     'tools must be an array', 'tools'),
    ('/v1/chat/completions', {'messages': MESSAGES, 'tools': [], 'n': 2},
     'only n=1 is supported', 'n'),
    ('/v1/messages', {'messages': 'hello'}, 'messages must be an array', 'messages'),
    ('/v1/messages/count_tokens', {'messages': MESSAGES, 'timeout_s': [30]},
     'timeout_s must be a number', 'timeout_s'),
], ids=['openai-messages', 'openai-timeout', 'openai-tools', 'openai-n',
        'anthropic-messages', 'anthropic-count-tokens-timeout'])
def test_compat_translation_client_fault_keeps_400(flask_client, path, body, message, field):
    _assert_client_400(flask_client.post(path, json=body), message, field)


# ── OpenAI-compatible embeddings ───────────────────────────────────

def test_embedding_execution_fault_is_a_redacted_500(flask_client, monkeypatch):
    import lib.model_routing.embedding_execution as service

    # e.g. a non-JSON provider body: ``response.json()`` raises ValueError.
    monkeypatch.setattr(service, 'execute_embeddings', _raises(ValueError(SECRET)))
    _assert_redacted_500(flask_client.post('/v1/embeddings', json={'input': 'hello'}))


def test_embedding_input_budget_keeps_400_before_routing(flask_client, monkeypatch):
    import lib.model_routing.embedding_execution as service

    monkeypatch.setattr(service, 'mint_capability_slot_group',
                        _raises(AssertionError('over-budget input must not be routed')))
    over_budget = ['x'] * (service.EMBEDDING_MAX_INPUT_ITEMS + 1)
    _assert_client_400(
        flask_client.post('/v1/embeddings', json={'input': over_budget}),
        f'input supports at most {service.EMBEDDING_MAX_INPUT_ITEMS} items', 'input')


def test_embedding_routing_refusal_keeps_its_typed_fields(flask_client, monkeypatch):
    import lib.model_routing.embedding_execution as service
    from lib.model_routing import ModelRoutingError

    monkeypatch.setattr(service, 'execute_embeddings', _raises(ModelRoutingError(
        'no embedding model matches', kind='model_not_found', field='model')))
    response = flask_client.post('/v1/embeddings', json={'input': 'hello'})
    _assert_client_400(response, 'no embedding model matches', 'model')
    assert response.get_json()['error_kind'] == 'model_not_found'


# ── Native chat and direct stream ──────────────────────────────────

@pytest.mark.parametrize(('module_name', 'path'), [
    ('routes.api_v1.chat', '/api/v4/chat/completions'),
    ('routes.api_v1.chat_direct', '/api/v4/chat/stream-direct'),
], ids=['completions', 'stream-direct'])
def test_native_chat_validation_fault_is_a_redacted_500(
        flask_client, monkeypatch, module_name, path):
    route = importlib.import_module(module_name)
    monkeypatch.setattr(route, '_validate_messages', _raises(ValueError(SECRET)))
    _assert_redacted_500(flask_client.post(path, json={'messages': MESSAGES}))


@pytest.mark.parametrize('path', [
    '/api/v4/chat/completions', '/api/v4/chat/stream-direct',
], ids=['completions', 'stream-direct'])
def test_native_chat_invalid_message_keeps_400(flask_client, path):
    response = flask_client.post(path, json={'messages': [{'role': 'wrong', 'content': 'x'}]})
    assert response.status_code == 400, response.get_data(as_text=True)
    assert "messages[0].role invalid: 'wrong'" in response.get_data(as_text=True)


def test_message_validator_names_the_messages_field():
    from lib.error_envelope import BadRequest
    from routes.api_v1.chat import _validate_messages

    for messages in (['text'], [{'role': 'wrong'}], [{'role': 'user'}]):
        with pytest.raises(BadRequest) as caught:
            _validate_messages(messages)
        assert caught.value.field == 'messages'


# ── Artifact owner review ──────────────────────────────────────────

REVIEW_COMMAND = {'action': 'requirements', 'requirementsRevision': 0,
                  'objective': 'Ship', 'criteria': []}


def test_artifact_review_internal_fault_is_a_redacted_500(flask_client, monkeypatch):
    import lib.artifacts.review as review

    monkeypatch.setattr(review, 'update_review', _raises(ValueError(SECRET)))
    _assert_redacted_500(
        flask_client.post('/api/v4/artifacts/artifact-1/review', json=REVIEW_COMMAND))


def test_artifact_review_typed_rejection_keeps_400(flask_client, monkeypatch):
    import lib.artifacts.review as review
    from lib.error_envelope import BadRequest

    message = 'A review note describing the evidence is required'
    monkeypatch.setattr(review, 'update_review', _raises(BadRequest(message, field='note')))
    response = flask_client.post('/api/v4/artifacts/artifact-1/review', json=REVIEW_COMMAND)
    assert response.status_code == 400
    assert message in response.get_data(as_text=True)


# ── Paper: arXiv fetch and OpenReview autofill ─────────────────────

class _ArxivResponse:
    headers = {'Content-Type': 'text/html'}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield b'<html><body>arXiv is busy, please retry</body></html>'

    def close(self):
        return None


@pytest.fixture
def arxiv_route(monkeypatch, tmp_path):
    import routes.paper_pkg._arxiv as arxiv

    monkeypatch.setattr(arxiv, 'PAPER_DIR', str(tmp_path))
    monkeypatch.setattr(arxiv, 'http_get', lambda *_args, **_kwargs: _ArxivResponse())
    return arxiv


def test_arxiv_download_internal_fault_is_a_redacted_500(flask_client, monkeypatch, arxiv_route):
    monkeypatch.setattr(arxiv_route, '_new_pdf_part', _raises(ValueError(SECRET)))
    _assert_redacted_500(
        flask_client.post('/api/v4/paper/fetch-arxiv', json={'url': '2301.00001'}))


def test_arxiv_unreadable_pdf_is_an_upstream_502(flask_client, monkeypatch, arxiv_route, tmp_path):
    import lib.pdf_parser.text as pdf_text

    monkeypatch.setattr(pdf_text, 'validate_pdf_bytes', lambda _raw: (False, 0, 'PDF has no pages'))
    response = flask_client.post('/api/v4/paper/fetch-arxiv', json={'url': '2301.00001'})
    assert response.status_code == 502, response.get_data(as_text=True)
    assert 'Downloaded file is not a readable PDF: PDF has no pages' in response.get_data(as_text=True)
    assert list(tmp_path.iterdir()) == []


def test_arxiv_unparseable_reference_keeps_400(flask_client, arxiv_route):
    response = flask_client.post('/api/v4/paper/fetch-arxiv', json={'url': 'not an arxiv reference'})
    assert response.status_code == 400
    assert 'Could not parse arXiv ID from URL' in response.get_data(as_text=True)


@pytest.fixture
def connected_review_bridge(monkeypatch):
    import lib.browser.queue as browser_queue

    monkeypatch.setattr(browser_queue, 'get_connected_clients',
                        lambda **_kwargs: [{'client_id': 'client-1', 'last_poll': 1}])
    monkeypatch.setattr(browser_queue, 'is_extension_connected', lambda *_args, **_kwargs: True)


def test_openreview_review_lookup_fault_is_not_a_missing_review(
        flask_client, monkeypatch, connected_review_bridge):
    from lib.paper.artifact_repository import PaperArtifactRepository

    monkeypatch.setattr(PaperArtifactRepository, 'get_report', _raises(ValueError(SECRET)))
    response = flask_client.post('/api/v4/paper/openreview/autofill', json={'paper_hash': 'paper-1'})
    _assert_redacted_500(response)
    assert 'No review found' not in response.get_data(as_text=True)


def test_openreview_absent_review_keeps_409(flask_client, monkeypatch, connected_review_bridge):
    from lib.paper.artifact_repository import PaperArtifactRepository

    monkeypatch.setattr(PaperArtifactRepository, 'get_report', lambda *_args, **_kwargs: None)
    response = flask_client.post('/api/v4/paper/openreview/autofill', json={'paper_hash': 'paper-1'})
    assert response.status_code == 409
    assert 'No review found for this paper yet' in response.get_data(as_text=True)


# ── Model-routing CAS replace ──────────────────────────────────────

def _put_model_routing(flask_client, monkeypatch, commit_error):
    import lib.model_routing.registration as registration
    import routes.api_v1.providers as providers

    class Repository:
        def get(self, _boundary):
            return SimpleNamespace(revision=3, document={})

        def compare_and_swap(self, *_args, **_kwargs):
            raise commit_error

    monkeypatch.setattr(providers, '_repository', Repository)
    monkeypatch.setattr(providers, '_contract_rejection', lambda *_args: None)
    monkeypatch.setattr(registration, 'prepare_registration_document',
                        lambda document, previous: document)
    return flask_client.put('/api/v4/model-routing',
                            json={'expected_revision': 3, 'model_routing': {}})


def test_model_routing_commit_cas_refusal_keeps_409(flask_client, monkeypatch):
    from lib.storage import StorageError

    response = _put_model_routing(flask_client, monkeypatch,
                                  StorageError('database_conflict', 'Current revision changed'))
    assert response.status_code == 409, response.get_data(as_text=True)


def test_model_routing_untyped_conflict_code_is_a_server_fault(flask_client, monkeypatch):
    class ProviderSdkError(Exception):
        code = 'resource_conflict'

    _assert_redacted_500(_put_model_routing(flask_client, monkeypatch, ProviderSdkError(SECRET)))
