"""Smoke test for new chatInner backend endpoints.

Covers:
  • PATCH /api/conversations/<id>/messages/<idx>
  • DELETE /api/conversations/<id>/messages/<idx>/branches/<bidx>
  • POST /api/chat/continue  (checkpoint scan + fallback decision only —
    the task-start path is exercised by existing regression tests)

Run with: python debug/test_chatinner_endpoints.py
"""

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Avoid starting background threads during import (server.py would start them).
os.environ.setdefault('CHATUI_DB_BACKEND', 'sqlite')

# The editable Flask dev snapshot in this environment (from the SWE-bench
# workdir) ships a werkzeug that lacks __version__; Flask's test_client()
# reads it, so shim it.
import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '0.0.0'

from flask import Flask  # noqa: E402
from flask_compress import Compress  # noqa: E402

from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg  # noqa: E402
from routes.conversations import conversations_bp  # noqa: E402
from routes.chat import chat_bp  # noqa: E402

app = Flask(__name__)
Compress(app)
app.register_blueprint(conversations_bp)
app.register_blueprint(chat_bp)
client = app.test_client()


def _create_conv(messages):
    """Insert a conversation directly into the DB and return its id."""
    conv_id = 'test-' + uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    db = get_thread_db(DOMAIN_CHAT)
    messages_json = json_dumps_pg(messages)
    db.execute(
        '''INSERT INTO conversations (id, user_id, title, messages, created_at, updated_at,
                                      settings, msg_count, search_text)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (conv_id, 1, 'Test', messages_json, now, now,
         '{}', len(messages), ''),
    )
    db.commit()
    return conv_id


def _load_conv(conv_id):
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT messages FROM conversations WHERE id=? AND user_id=1',
        (conv_id,),
    ).fetchone()
    if not row:
        return None
    return json.loads(row['messages'] or '[]')


def test_patch_message():
    conv_id = _create_conv([
        {'role': 'user', 'content': 'hello', 'timestamp': 1},
        {'role': 'assistant', 'content': 'hi there', 'timestamp': 2},
    ])
    # Edit message 0's content.
    r = client.patch(
        f'/api/conversations/{conv_id}/messages/0',
        json={'content': 'updated hello'},
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['ok'] is True
    assert body['msgCount'] == 2
    assert body['msg']['content'] == 'updated hello'

    # DB should reflect the change; other messages untouched.
    msgs = _load_conv(conv_id)
    assert msgs[0]['content'] == 'updated hello'
    assert msgs[1]['content'] == 'hi there'

    # Null-value → delete key.
    r = client.patch(
        f'/api/conversations/{conv_id}/messages/0',
        json={'originalContent': 'foo', '_showingTranslation': True},
    )
    assert r.status_code == 200
    assert _load_conv(conv_id)[0]['originalContent'] == 'foo'
    r = client.patch(
        f'/api/conversations/{conv_id}/messages/0',
        json={'originalContent': None},
    )
    assert r.status_code == 200
    msgs = _load_conv(conv_id)
    assert 'originalContent' not in msgs[0]
    assert msgs[0]['_showingTranslation'] is True

    # Non-whitelisted key → 400.
    r = client.patch(
        f'/api/conversations/{conv_id}/messages/0',
        json={'role': 'system'},  # tampering attempt
    )
    assert r.status_code == 400, r.get_json()
    assert 'unsupported_keys' in (r.get_json().get('error') or '')

    # Out-of-range index → 400.
    r = client.patch(
        f'/api/conversations/{conv_id}/messages/99',
        json={'content': 'x'},
    )
    assert r.status_code == 400

    # Unknown conv → 404.
    r = client.patch(
        '/api/conversations/does-not-exist/messages/0',
        json={'content': 'x'},
    )
    assert r.status_code == 404

    # Empty patch → 400.
    r = client.patch(
        f'/api/conversations/{conv_id}/messages/0',
        json={},
    )
    assert r.status_code == 400
    print('[OK] test_patch_message')


def test_delete_branch():
    branches = [
        {'title': 'A', 'messages': [{'role': 'user', 'content': 'branch A'}]},
        {'title': 'B', 'messages': [{'role': 'user', 'content': 'branch B'}]},
        {'title': 'C', 'messages': [{'role': 'user', 'content': 'branch C'}]},
    ]
    conv_id = _create_conv([
        {'role': 'user', 'content': 'hello', 'timestamp': 1},
        {'role': 'assistant', 'content': 'hi', 'timestamp': 2, 'branches': branches},
    ])
    # Delete branch at idx=1 (the 'B' branch).
    r = client.delete(
        f'/api/conversations/{conv_id}/messages/1/branches/1',
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['ok'] is True
    assert body['branchCount'] == 2

    msgs = _load_conv(conv_id)
    titles = [b.get('title') for b in msgs[1].get('branches') or []]
    assert titles == ['A', 'C'], titles

    # Out-of-range branch → 400.
    r = client.delete(
        f'/api/conversations/{conv_id}/messages/1/branches/5',
    )
    assert r.status_code == 400

    # Delete last branch → branches key removed.
    for _ in range(2):
        client.delete(f'/api/conversations/{conv_id}/messages/1/branches/0')
    msgs = _load_conv(conv_id)
    assert 'branches' not in msgs[1] or not msgs[1].get('branches')
    print('[OK] test_delete_branch')


def test_continue_fallback_no_checkpoint():
    """If no tool rounds, /api/chat/continue returns fallback=regenerate."""
    conv_id = _create_conv([
        {'role': 'user', 'content': 'ask something', 'timestamp': 1},
        {'role': 'assistant', 'content': 'partial answer', 'timestamp': 2},
    ])
    r = client.post(
        '/api/chat/continue',
        json={'convId': conv_id, 'config': {'model': 'gpt-4o'}},
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body.get('fallback') == 'regenerate', body
    assert body.get('reason') == 'no_checkpoint'
    print('[OK] test_continue_fallback_no_checkpoint')


def test_continue_empty_assistant():
    conv_id = _create_conv([
        {'role': 'user', 'content': 'ask something', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'timestamp': 2},
    ])
    r = client.post(
        '/api/chat/continue',
        json={'convId': conv_id, 'config': {'model': 'gpt-4o'}},
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body.get('fallback') == 'regenerate'
    assert body.get('reason') == 'empty_assistant'
    print('[OK] test_continue_empty_assistant')


def test_continue_rollback_scan():
    """Direct unit test of the checkpoint scan logic — does NOT start a task."""
    from routes.chat import _scan_continue_checkpoint
    assistant_msg = {
        'role': 'assistant',
        'content': 'preamble text alongside call 1\n\nfree text after last batch',
        'thinking': 'THOUGHTS-A' + 'THOUGHTS-TAIL',
        'toolRounds': [
            {
                'toolCallId': 'call_1',
                'toolName': 'web_search',
                'toolArgs': '{"query":"hi"}',
                'toolContent': 'result 1',
                'assistantContent': 'preamble text alongside call 1',
                'thinking': 'THOUGHTS-A',
                'status': 'done',
                'llmRound': 1,
                'roundNum': 1,
            },
            {
                'toolCallId': 'call_2',
                'toolName': 'read_file',
                'toolArgs': '{"path":"x"}',
                'toolContent': None,  # incomplete — checkpoint ends BEFORE this round
                'status': 'running',
                'llmRound': 2,
                'roundNum': 2,
            },
        ],
    }
    scan = _scan_continue_checkpoint(assistant_msg)
    assert scan is not None, 'expected a recoverable checkpoint'
    assert len(scan['kept_rounds']) == 1
    assert scan['discarded_rounds'] == 1
    assert scan['preserved_content'] == 'preamble text alongside call 1'
    assert scan['discarded_content'] > 0
    assert scan['preserved_thinking_chars'] == len('THOUGHTS-A')
    assert scan['discarded_thinking'] > 0
    assert len(scan['tool_history']) == 1
    tr = scan['tool_history'][0]
    assert tr['toolCalls'][0]['id'] == 'call_1'
    assert tr['toolResults'][0]['content'] == 'result 1'
    assert tr['thinking'] == 'THOUGHTS-A'
    print('[OK] test_continue_rollback_scan')


def main():
    test_patch_message()
    test_delete_branch()
    test_continue_fallback_no_checkpoint()
    test_continue_empty_assistant()
    test_continue_rollback_scan()
    print('\nAll smoke tests passed ✓')


if __name__ == '__main__':
    main()
