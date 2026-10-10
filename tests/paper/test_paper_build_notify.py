"""Failed-build inbox return: payload shape, dedupe, volatile-only delivery."""
from __future__ import annotations

import pytest

from lib.paper.builds import notify

pytestmark = pytest.mark.unit

_RECEIPT = {'schema_version': 1, 'build_id': 'b' * 32, 'document_id': 'd' * 64,
            'owner_user_id': 1, 'target': 'host', 'source_digest': 'a' * 64,
            'version_verified': True, 'main_file': 'main.tex',
            'engine': 'pdflatex', 'toolchain': 'TeX Live', 'status': 'failed',
            'started_at': 0, 'completed_at': 1000,
            'artifacts': [], 'diagnostics': [
                {'severity': 'error', 'code': 'tex_error', 'file': 'main.tex',
                 'line': 5, 'message': 'Undefined control sequence.'},
                {'severity': 'warning', 'code': 'tex_warning', 'file': '',
                 'line': None, 'message': 'There were undefined references.'}]}


@pytest.fixture(autouse=True)
def _clear_seen():
    with notify._seen_lock:
        notify._seen.clear()
    yield


def test_failed_build_reaches_live_conversation_inbox():
    delivered = []
    import lib.agent_inbox as inbox
    original = inbox.enqueue
    inbox.enqueue = lambda *a, **kw: delivered.append((a, kw)) or True
    try:
        assert notify.notify_build_failed(
            owner_user_id=1, conversation_id='conv-1', receipt=_RECEIPT)
    finally:
        inbox.enqueue = original
    (args, kwargs) = delivered[0]
    assert args[0] == 'conv-1'
    payload = args[1]
    assert payload.startswith('<paper-build-update>')
    assert 'main.tex:5' in payload
    assert 'Undefined control sequence.' in payload
    assert '<errors>1</errors>' in payload
    assert 'paper_build' in payload
    assert kwargs['mode'] == 'paper-build'
    assert kwargs['priority'] == 'next'


def test_same_broken_digest_is_reported_once():
    import lib.agent_inbox as inbox
    calls = []
    original = inbox.enqueue
    inbox.enqueue = lambda *a, **kw: calls.append(1) or True
    try:
        notify.notify_build_failed(owner_user_id=1, conversation_id='c',
                                   receipt=_RECEIPT)
        notify.notify_build_failed(owner_user_id=1, conversation_id='c',
                                   receipt=_RECEIPT)
        changed = {**_RECEIPT, 'source_digest': 'f' * 64}
        notify.notify_build_failed(owner_user_id=1, conversation_id='c',
                                   receipt=changed)
    finally:
        inbox.enqueue = original
    assert len(calls) == 2


def test_success_and_empty_conversation_never_deliver():
    assert not notify.notify_build_failed(
        owner_user_id=1, conversation_id='c',
        receipt={**_RECEIPT, 'status': 'success'})
    assert not notify.notify_build_failed(
        owner_user_id=1, conversation_id='', receipt=_RECEIPT)


def test_delivery_failure_is_advisory():
    import lib.agent_inbox as inbox
    original = inbox.enqueue
    def boom(*a, **kw):
        raise RuntimeError('inbox unavailable')
    inbox.enqueue = boom
    try:
        assert not notify.notify_build_failed(
            owner_user_id=1, conversation_id='c', receipt=_RECEIPT)
    finally:
        inbox.enqueue = original
