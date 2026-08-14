"""Tests for Producer B — assistant-message inline scanner (Step 4).

Covers:
  • Fenced ```html / ```markdown / ```svg blocks above thresholds → artifact
  • Tiny code snippets → no artifact
  • Bare <!doctype html> document in prose → artifact
  • Bare <html> region in prose → artifact
  • A fenced HTML block does NOT also produce a "bare html" artifact
    (mask-then-scan ordering)
  • Non-renderable language tags (```python, ```bash) ignored
  • Unicode / large multi-byte content
  • Title extraction (markdown H1, html <title>)
  • Empty / non-string input handled
  • Backfill route round-trip
  • SSE event emitted when a task is supplied

Run:  pytest tests/test_artifacts_scanner.py -v
"""
from __future__ import annotations

import json
import threading

import pytest

pytest_plugins = ('tests._artifact_sidecar',)


# ─── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def fake_task():
    return {
        'id': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
        'convId': 'conv-scan',
        'events': [],
        'events_lock': threading.Lock(),
    }


def _last_artifact_event(task):
    for ev in reversed(task['events']):
        if ev.get('type') == 'artifact':
            return ev
    return None


def _make_long_html(repeats: int = 8) -> str:
    """Build an HTML doc reliably above the fence size threshold (2 KiB)."""
    body = ('<p>' + ('hello world ' * 40) + '</p>\n') * repeats
    return ('<!doctype html><html><head><title>Big Doc</title></head>'
            '<body>' + body + '</body></html>')


def _make_long_markdown(lines: int = 100) -> str:
    return '# Title\n\n' + '\n'.join(f'paragraph {i} content' for i in range(lines))


# ─── Detection ───────────────────────────────────────────────────────

class TestFenceDetection:
    def test_html_fence_above_threshold(self, flask_app, fake_task):
        from lib.artifacts import scan_message

        html_body = _make_long_html()
        msg = ('Here is the report.\n\n'
               '```html\n' + html_body + '\n```\n\nThanks.')
        with flask_app.app_context():
            created = scan_message('conv-fence-html', msg, task=fake_task)

        assert len(created) == 1
        assert created[0]['format'] == 'html'
        assert created[0]['source'] == 'inline_fence'
        assert 'Big Doc' in created[0]['title']
        ev = _last_artifact_event(fake_task)
        assert ev is not None and ev['format'] == 'html'

    def test_markdown_fence_above_line_threshold(self, flask_app):
        from lib.artifacts import scan_message
        md_body = _make_long_markdown(120)
        msg = 'Outline:\n\n```markdown\n' + md_body + '\n```\n'
        with flask_app.app_context():
            created = scan_message('conv-fence-md', msg)
        assert len(created) == 1
        assert created[0]['format'] == 'markdown'
        assert created[0]['title'] == 'Title'

    def test_md_alias_works(self, flask_app):
        from lib.artifacts import scan_message
        md_body = _make_long_markdown(120)
        msg = '```md\n' + md_body + '\n```'
        with flask_app.app_context():
            created = scan_message('conv-fence-md-alias', msg)
        assert len(created) == 1
        assert created[0]['format'] == 'markdown'

    def test_short_fence_skipped(self, flask_app):
        from lib.artifacts import scan_message
        # One-line HTML fence — well below thresholds.
        msg = '```html\n<b>hi</b>\n```'
        with flask_app.app_context():
            created = scan_message('conv-fence-short', msg)
        assert created == []

    def test_non_renderable_language_skipped(self, flask_app):
        from lib.artifacts import scan_message
        # Big python block — still skipped because format isn't allowed.
        msg = '```python\n' + ('print("x")\n' * 200) + '```'
        with flask_app.app_context():
            created = scan_message('conv-fence-py', msg)
        assert created == []


class TestBareHtmlDetection:
    def test_doctype_html_promoted(self, flask_app):
        from lib.artifacts import scan_message
        body = _make_long_html()
        msg = 'Here is what I made:\n\n' + body + '\n\nLet me know.'
        with flask_app.app_context():
            created = scan_message('conv-bare-doctype', msg)
        assert len(created) == 1
        assert created[0]['format'] == 'html'
        assert created[0]['source'] == 'inline_doc'

    def test_bare_html_tag_promoted(self, flask_app):
        from lib.artifacts import scan_message
        body = '<html><head><title>NoDoctype</title></head><body>' + ('x' * 500) + '</body></html>'
        msg = body
        with flask_app.app_context():
            created = scan_message('conv-bare-html', msg)
        assert len(created) == 1
        assert created[0]['title'] == 'NoDoctype'

    def test_truncated_doctype_still_captured(self, flask_app):
        """Streamed truncation: <!doctype html>...<body> with no </html>."""
        from lib.artifacts import scan_message
        body = '<!doctype html><html><body>' + ('content ' * 100)
        with flask_app.app_context():
            created = scan_message('conv-trunc', body)
        assert len(created) == 1
        assert created[0]['format'] == 'html'

    def test_fenced_html_not_double_detected(self, flask_app):
        """A fenced ```html block must NOT also fire the bare-html detector."""
        from lib.artifacts import scan_message
        body = _make_long_html()
        msg = '```html\n' + body + '\n```'
        with flask_app.app_context():
            created = scan_message('conv-no-double', msg)
        # Exactly one artifact, sourced as inline_fence (not inline_doc).
        assert len(created) == 1
        assert created[0]['source'] == 'inline_fence'

    def test_html_inside_python_fence_not_detected(self, flask_app):
        """Regression: technical prose with ``<html>`` inside a ```python
        block must NOT be promoted as a bare-HTML artifact, even when
        the closing tag also appears elsewhere in prose.

        See memory ``artifacts-scanner-prose-false-positive``.
        """
        from lib.artifacts import scan_message
        # Body big enough to clear _MIN_DOC_BYTES if naively detected.
        msg = (
            'Here is some explanation about full-doc HTML wrapping.\n\n'
            '```python\n'
            "body = '<html>" + ('hello world ' * 60) + "</html>'\n"
            'print(body)\n'
            '```\n\n'
            'Note that we mention `</html>` again in inline prose.\n'
        )
        with flask_app.app_context():
            created = scan_message('conv-py-fence-html', msg)
        # No artifacts.  The python fence is non-renderable and the only
        # ``<html>`` / ``</html>`` occurrences are inside it / inline code.
        assert created == [], (
            f'False positive: scanner created {len(created)} artifacts, '
            f'first source={created[0]["source"] if created else None}'
        )

    def test_inline_backtick_html_tags_not_detected(self, flask_app):
        """Regression: ``<html>`` written as inline ``` `<html>` ``` in
        prose must NOT anchor a bare-HTML capture.
        """
        from lib.artifacts import scan_message
        msg = (
            'In documentation we frequently mention tags like `<html>` '
            'and `</html>` followed by ' + ('a paragraph of body text. ' * 30) +
            ' more discussion.\n'
        )
        with flask_app.app_context():
            created = scan_message('conv-inline-tag-mention', msg)
        assert created == []

    def test_explanatory_text_with_tag_mentions_no_artifact(self, flask_app):
        """End-to-end regression of the production bug: an assistant
        reply explaining the artifact subsystem mentioning ``<html>`` /
        ``</html>`` in backticks AND inside a ```python``` example must
        produce zero artifacts.
        """
        from lib.artifacts import scan_message
        msg = (
            "## Why the trick works\n\n"
            "For full `<!doctype html>` documents, KaTeX is injected just "
            "before `</html>` so the artifact's own structure stays intact. "
            "Strips model `<script>` tags before injecting:\n\n"
            "```python\n"
            "if '<html' in body:\n"
            "    return body[:idx] + head + body[idx:]\n"
            "```\n\n"
            "Otherwise we wrap the fragment in our own template, where "
            "`<html>` and `</html>` are inserted around the body content."
        )
        with flask_app.app_context():
            created = scan_message('conv-explainer-prose', msg)
        assert created == []


class TestEdgeCases:
    def test_empty_input(self, flask_app):
        from lib.artifacts import scan_message
        with flask_app.app_context():
            assert scan_message('conv-empty', '') == []
            assert scan_message('', 'something') == []
            assert scan_message('conv', None) == []  # type: ignore[arg-type]

    def test_unicode_content(self, flask_app):
        from lib.artifacts import scan_message
        body = '# 标题\n\n' + ('中文段落 ' * 200)
        msg = '```markdown\n' + body + '\n```'
        with flask_app.app_context():
            created = scan_message('conv-unicode', msg)
        assert len(created) == 1
        assert created[0]['title'] == '标题'

    def test_dedupe_across_scan_calls(self, flask_app):
        """Same message scanned twice → second call sees dedupe, no new rows."""
        from lib.artifacts import scan_message
        body = _make_long_html()
        msg = '```html\n' + body + '\n```'
        with flask_app.app_context():
            first = scan_message('conv-dedupe-scan', msg)
            second = scan_message('conv-dedupe-scan', msg)
        assert len(first) == 1
        # Same id returned — proof of dedupe.
        assert second and second[0]['id'] == first[0]['id']

    def test_no_task_no_event(self, flask_app):
        from lib.artifacts import scan_message
        body = _make_long_html()
        msg = body  # bare doctype html
        with flask_app.app_context():
            created = scan_message('conv-no-task', msg, task=None)
        # Created the row but no SSE event was emitted (task=None path).
        assert len(created) == 1


# ─── Backfill route ──────────────────────────────────────────────────

class TestBackfillRoute:
    def _seed_conv(self, flask_client, conv_id, messages):
        # Use the real API to seed: PUT /api/v1/conversations/<id>
        payload = {
            'title':     'scan-test',
            'messages':  messages,
            'createdAt': 1700000000000,
            'updatedAt': 1700000000000,
            'settings':  {},
        }
        r = flask_client.put(f'/api/v1/conversations/{conv_id}', json=payload)
        assert r.status_code in (200, 201), r.get_data(as_text=True)

    def test_backfill_round_trip(self, flask_client):
        import uuid as _uuid
        big_html = _make_long_html()
        msg_id_1 = str(_uuid.uuid4())
        msgs = [
            {'role': 'user', 'content': 'make me a page'},
            {'role': 'assistant',
             '_msgId': msg_id_1,
             'content': 'Here you go:\n\n```html\n' + big_html + '\n```'},
            {'role': 'user', 'content': 'thanks'},
            {'role': 'assistant',
             '_msgId': str(_uuid.uuid4()),
             'content': 'Plain reply with no artifact.'},
        ]
        conv_id = 'conv-backfill-' + _uuid.uuid4().hex[:8]
        self._seed_conv(flask_client, conv_id, msgs)

        r = flask_client.post('/api/v1/artifacts/scan', json={'conv_id': conv_id})
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body['conv_id'] == conv_id
        assert body['scanned'] == 2
        assert body['created'] >= 1
        assert any(a['msg_id'] == msg_id_1 for a in body['artifacts'])

        # Idempotent: a second backfill creates 0 new rows (dedupe).
        flask_client.post('/api/v1/artifacts/scan', json={'conv_id': conv_id})
        listing = flask_client.get(f'/api/v1/artifacts?conv={conv_id}').get_json()
        # Only the assistant-1 message produced an artifact.
        assistant_1_arts = [a for a in listing['artifacts']
                            if a['msg_id'] == msg_id_1]
        assert len(assistant_1_arts) == 1

    def test_backfill_404_on_missing_conv(self, flask_client):
        r = flask_client.post('/api/v1/artifacts/scan',
                              json={'conv_id': 'no-such-conv-xyz'})
        assert r.status_code == 404
