"""Tests for the chat-artifact subsystem (Step 1).

Covers:
  • create_artifact persists and returns metadata
  • Dedupe: same conv + same content → reuse existing row
  • Different conv with same content → separate rows
  • get_artifact / get_artifact_meta semantics
  • list_artifacts ordering
  • delete_artifact soft-delete
  • set_pinned toggle
  • is_renderable_path / detect_format predicates
  • Size cap rejects oversize content
  • Routes: GET /api/artifacts/<id>, /raw, /conv/<id>; POST /pin; DELETE
  • /raw security headers (CSP sandbox + nosniff + no-referrer)
  • SSE event payload shape (no content blob)

Run:  pytest tests/test_artifacts_api.py -v
"""
from __future__ import annotations

import threading

import pytest


# ─── Helpers ──────────────────────────────────────────────────────────

@pytest.fixture()
def fake_task():
    """Minimal task dict that satisfies append_event's invariants."""
    return {
        'id':           '11111111-1111-1111-1111-111111111111',
        'convId':       'conv-test-fake',
        'events':       [],
        'events_lock':  threading.Lock(),
        'phase':        None,
    }


# ─── Predicates ───────────────────────────────────────────────────────

class TestPredicates:
    def test_is_renderable_path_md(self):
        from lib.artifacts import is_renderable_path
        assert is_renderable_path('report.md')
        assert is_renderable_path('REPORT.MD')
        assert is_renderable_path('a/b/c.markdown')

    def test_is_renderable_path_html(self):
        from lib.artifacts import is_renderable_path
        assert is_renderable_path('out.html')
        assert is_renderable_path('out.htm')

    def test_is_renderable_path_svg(self):
        from lib.artifacts import is_renderable_path
        assert is_renderable_path('chart.svg')

    def test_is_renderable_path_negative(self):
        from lib.artifacts import is_renderable_path
        assert not is_renderable_path('script.py')
        assert not is_renderable_path('data.json')
        assert not is_renderable_path('')
        assert not is_renderable_path('README')

    def test_detect_format(self):
        from lib.artifacts import detect_format
        assert detect_format('a.md') == 'markdown'
        assert detect_format('a.markdown') == 'markdown'
        assert detect_format('a.html') == 'html'
        assert detect_format('a.htm') == 'html'
        assert detect_format('a.svg') == 'svg'
        assert detect_format('a.txt') is None
        assert detect_format('') is None


# ─── Core CRUD ────────────────────────────────────────────────────────

class TestCreateAndDedupe:
    def test_create_returns_metadata(self, flask_app):
        from lib.artifacts import create_artifact
        with flask_app.app_context():
            meta = create_artifact(
                conv_id='conv-A',
                content='# Hello\n\nBody text.',
                format='markdown',
                source='write_file',
                title='hello.md',
                source_ref={'path': 'hello.md'},
            )
        assert meta['id']
        assert meta['conv_id'] == 'conv-A'
        assert meta['format'] == 'markdown'
        assert meta['source'] == 'write_file'
        assert meta['title'] == 'hello.md'
        assert meta['size_bytes'] == len('# Hello\n\nBody text.'.encode('utf-8'))
        assert meta['version'] == 1
        assert meta['pinned'] is False
        assert 'content' not in meta  # creator return strips content
        assert isinstance(meta['source_ref'], dict)
        assert meta['source_ref'] == {'path': 'hello.md'}

    def test_dedupe_same_conv_same_content(self, flask_app):
        from lib.artifacts import create_artifact
        body = '<html><body>Test ' + 'x' * 50 + '</body></html>'
        with flask_app.app_context():
            a = create_artifact(conv_id='conv-dup', content=body,
                                format='html', source='write_file', title='one.html')
            b = create_artifact(conv_id='conv-dup', content=body,
                                format='html', source='inline_doc', title='two.html')
        # Same id — second call returned the existing row.
        assert a['id'] == b['id']

    def test_no_dedupe_across_convs(self, flask_app):
        from lib.artifacts import create_artifact
        body = '# Same body for two convs\n'
        with flask_app.app_context():
            a = create_artifact(conv_id='conv-X', content=body,
                                format='markdown', source='write_file')
            b = create_artifact(conv_id='conv-Y', content=body,
                                format='markdown', source='write_file')
        assert a['id'] != b['id']
        assert a['content_sha256'] == b['content_sha256']

    def test_size_cap_enforced(self, flask_app):
        from lib.artifacts import ArtifactSizeError, create_artifact
        from lib.artifacts.core import _HARD_MAX_BYTES
        big = 'a' * (_HARD_MAX_BYTES + 1)
        with flask_app.app_context():
            with pytest.raises(ArtifactSizeError):
                create_artifact(conv_id='conv-big', content=big,
                                format='html', source='write_file')

    def test_invalid_format_rejected(self, flask_app):
        from lib.artifacts import create_artifact
        with flask_app.app_context():
            with pytest.raises(ValueError):
                create_artifact(conv_id='c', content='x',
                                format='pdf', source='write_file')

    def test_empty_conv_rejected(self, flask_app):
        from lib.artifacts import create_artifact
        with flask_app.app_context():
            with pytest.raises(ValueError):
                create_artifact(conv_id='', content='x',
                                format='markdown', source='write_file')


class TestRetrieval:
    def test_get_artifact_includes_content(self, flask_app):
        from lib.artifacts import create_artifact, get_artifact
        body = '# get_artifact test\n\nbody'
        with flask_app.app_context():
            meta = create_artifact(conv_id='conv-get', content=body,
                                   format='markdown', source='write_file')
            full = get_artifact(meta['id'])
        assert full['content'] == body
        assert full['id'] == meta['id']

    def test_get_artifact_meta_no_content(self, flask_app):
        from lib.artifacts import create_artifact, get_artifact_meta
        body = '# meta only\n'
        with flask_app.app_context():
            meta = create_artifact(conv_id='conv-meta', content=body,
                                   format='markdown', source='write_file')
            slim = get_artifact_meta(meta['id'])
        assert 'content' not in slim
        assert slim['size_bytes'] == len(body.encode('utf-8'))

    def test_get_missing_raises(self, flask_app):
        from lib.artifacts import ArtifactNotFoundError, get_artifact
        with flask_app.app_context():
            with pytest.raises(ArtifactNotFoundError):
                get_artifact('00000000-0000-0000-0000-000000000000')

    def test_list_orders_newest_first(self, flask_app):
        import time
        from lib.artifacts import create_artifact, list_artifacts
        with flask_app.app_context():
            a = create_artifact(conv_id='conv-list', content='one\n',
                                format='markdown', source='write_file')
            time.sleep(0.005)  # ensure created_at differs
            b = create_artifact(conv_id='conv-list', content='two\n',
                                format='markdown', source='write_file')
            items = list_artifacts('conv-list')
        ids = [m['id'] for m in items]
        assert b['id'] in ids and a['id'] in ids
        assert ids.index(b['id']) < ids.index(a['id'])


class TestDelete:
    def test_soft_delete_hides_from_list(self, flask_app):
        from lib.artifacts import (
            ArtifactNotFoundError,
            create_artifact,
            delete_artifact,
            get_artifact,
            list_artifacts,
        )
        with flask_app.app_context():
            meta = create_artifact(conv_id='conv-del', content='go away\n',
                                   format='markdown', source='write_file')
            assert delete_artifact(meta['id']) is True
            assert all(m['id'] != meta['id']
                       for m in list_artifacts('conv-del'))
            with pytest.raises(ArtifactNotFoundError):
                get_artifact(meta['id'])

    def test_delete_idempotent(self, flask_app):
        from lib.artifacts import create_artifact, delete_artifact
        with flask_app.app_context():
            meta = create_artifact(conv_id='conv-del2', content='x\n',
                                   format='markdown', source='write_file')
            assert delete_artifact(meta['id']) is True
            # Already deleted row → second call returns False.
            assert delete_artifact(meta['id']) is False


class TestPin:
    def test_set_pinned_round_trip(self, flask_app):
        from lib.artifacts import create_artifact, get_artifact_meta, set_pinned
        with flask_app.app_context():
            meta = create_artifact(conv_id='conv-pin', content='pin me\n',
                                   format='markdown', source='write_file')
            assert meta['pinned'] is False
            assert set_pinned(meta['id'], True) is True
            assert get_artifact_meta(meta['id'])['pinned'] is True
            assert set_pinned(meta['id'], False) is True
            assert get_artifact_meta(meta['id'])['pinned'] is False


# ─── SSE event ────────────────────────────────────────────────────────

class TestSseEvent:
    def test_emit_event_no_content(self, flask_app, fake_task):
        from lib.artifacts import create_artifact, emit_artifact_event
        with flask_app.app_context():
            meta = create_artifact(conv_id='conv-sse', content='# evt\n',
                                   format='markdown', source='write_file',
                                   task_id=fake_task['id'], title='evt.md')
            emit_artifact_event(fake_task, meta)
        assert len(fake_task['events']) >= 1
        ev = fake_task['events'][-1]
        assert ev['type'] == 'artifact'
        assert ev['id'] == meta['id']
        assert ev['format'] == 'markdown'
        assert ev['source'] == 'write_file'
        assert ev['size_bytes'] == meta['size_bytes']
        assert ev['url'] == f'/api/artifacts/{meta["id"]}/raw'
        assert 'content' not in ev  # MUST NOT carry the body

    def test_emit_event_no_task_is_safe(self, flask_app):
        from lib.artifacts import create_artifact, emit_artifact_event
        with flask_app.app_context():
            meta = create_artifact(conv_id='conv-sse2', content='# x\n',
                                   format='markdown', source='write_file')
            # No exception even when task is None
            emit_artifact_event(None, meta)


# ─── Routes ───────────────────────────────────────────────────────────

class TestRoutes:
    def test_get_meta_404(self, flask_client):
        resp = flask_client.get('/api/artifacts/does-not-exist')
        assert resp.status_code == 404

    def test_full_round_trip(self, flask_app, flask_client):
        from lib.artifacts import create_artifact
        body = '# Routed\n\nHello.'
        with flask_app.app_context():
            meta = create_artifact(conv_id='conv-route', content=body,
                                   format='markdown', source='write_file',
                                   title='r.md')

        # GET meta
        r = flask_client.get(f'/api/artifacts/{meta["id"]}')
        assert r.status_code == 200
        assert r.get_json()['id'] == meta['id']
        assert 'content' not in r.get_json()

        # GET raw — content + security headers
        r = flask_client.get(f'/api/artifacts/{meta["id"]}/raw')
        assert r.status_code == 200
        assert r.headers['Content-Type'].startswith('text/markdown')
        assert 'sandbox' in r.headers['Content-Security-Policy']
        assert "default-src 'none'" in r.headers['Content-Security-Policy']
        assert r.headers['X-Content-Type-Options'] == 'nosniff'
        assert r.headers['Referrer-Policy'] == 'no-referrer'
        assert r.get_data(as_text=True) == body

        # GET list
        r = flask_client.get('/api/artifacts/conv/conv-route')
        assert r.status_code == 200
        payload = r.get_json()
        assert payload['count'] == 1
        assert payload['artifacts'][0]['id'] == meta['id']

        # POST pin
        r = flask_client.post(f'/api/artifacts/{meta["id"]}/pin',
                              json={'pinned': True})
        assert r.status_code == 200
        assert r.get_json()['pinned'] is True

        # DELETE
        r = flask_client.delete(f'/api/artifacts/{meta["id"]}')
        assert r.status_code == 200
        assert r.get_json()['deleted'] is True

        # After delete: 404 on raw + meta
        r = flask_client.get(f'/api/artifacts/{meta["id"]}')
        assert r.status_code == 404
        r = flask_client.get(f'/api/artifacts/{meta["id"]}/raw')
        assert r.status_code == 404

    def test_raw_disposition_handles_unicode_title(self, flask_app, flask_client):
        """Regression: HTTP headers are ISO-8859-1 — non-ASCII titles
        must be RFC 5987-encoded or the response writer raises
        ``UnicodeEncodeError`` and werkzeug returns a 500.

        Production hit: a Chinese-titled artifact tripped this in
        ``Content-Disposition: inline; filename="<CJK>"``.
        """
        from lib.artifacts import create_artifact
        with flask_app.app_context():
            meta = create_artifact(
                conv_id='conv-cjk', content='# 中文标题\n',
                format='markdown', source='write_file',
                title='周报告_v1.md',  # CJK + emoji-adjacent characters
            )
        r = flask_client.get(f'/api/artifacts/{meta["id"]}/raw')
        assert r.status_code == 200, r.get_data(as_text=True)
        cd = r.headers['Content-Disposition']
        # ASCII fallback present and ALL characters are latin-1 safe.
        cd.encode('latin-1')  # would raise if non-ASCII leaked through
        # RFC 5987 extension present so modern browsers preserve unicode.
        assert "filename*=UTF-8''" in cd
        # Body intact.
        assert '中文标题' in r.get_data(as_text=True)

    def test_view_route_injects_katex(self, flask_app, flask_client):
        """The /view endpoint wraps the artifact with our trusted KaTeX
        bundle and applies a CSP that allows ONLY same-origin scripts
        — so model `<script>` is stripped + blocked but math renders."""
        from lib.artifacts import create_artifact
        body = (
            '<html><body>'
            '<script>alert("evil")</script>'
            '<p>Inline math: $E = mc^2$</p>'
            '<a href="#anchor">jump</a>'
            '<h2 id="anchor">target</h2>'
            '</body></html>'
        )
        with flask_app.app_context():
            meta = create_artifact(
                conv_id='conv-view-katex', content=body, format='html',
                source='write_file', title='math.html',
            )
        r = flask_client.get(f'/api/artifacts/{meta["id"]}/view')
        assert r.status_code == 200
        assert r.headers['Content-Type'].startswith('text/html')
        csp = r.headers['Content-Security-Policy']
        # Same-origin scripts allowed (so KaTeX can run)…
        assert "script-src 'self'" in csp
        # …but inline / unsafe-inline NOT allowed.
        assert "script-src 'self' 'unsafe-inline'" not in csp
        # Sandbox directive present.
        assert 'sandbox allow-scripts' in csp

        # Model <script> stripped at the source.
        page = r.get_data(as_text=True)
        assert '<script>alert("evil")</script>' not in page
        # Trusted KaTeX bundle injected.
        assert '/static/vendor/katex/katex.min.js' in page
        assert '/static/vendor/katex/tofu-auto-render.js' in page
        # Original content preserved (the parts that matter).
        assert 'E = mc^2' in page
        assert 'href="#anchor"' in page

    def test_view_rejects_markdown(self, flask_app, flask_client):
        """/view is only meaningful for html/svg; markdown returns 400."""
        from lib.artifacts import create_artifact
        with flask_app.app_context():
            meta = create_artifact(
                conv_id='conv-view-md-reject', content='# title',
                format='markdown', source='write_file',
            )
        r = flask_client.get(f'/api/artifacts/{meta["id"]}/view')
        assert r.status_code == 400

    def test_view_404_on_missing(self, flask_client):
        r = flask_client.get('/api/artifacts/nonexistent-view-xyz/view')
        assert r.status_code == 404

    def test_view_full_doc_keeps_structure(self, flask_app, flask_client):
        """Full <!doctype html> documents have KaTeX injected into <head>
        without losing the rest of the document."""
        from lib.artifacts import create_artifact
        body = (
            '<!doctype html><html><head><title>Big</title></head>'
            '<body><h1>Hello</h1><p>$$x+y$$</p></body></html>'
        )
        with flask_app.app_context():
            meta = create_artifact(
                conv_id='conv-view-fulldoc', content=body, format='html',
                source='write_file', title='big.html',
            )
        r = flask_client.get(f'/api/artifacts/{meta["id"]}/view')
        assert r.status_code == 200
        page = r.get_data(as_text=True)
        # Original head + body preserved.
        assert '<title>Big</title>' in page
        assert '<h1>Hello</h1>' in page
        # KaTeX injected before </head>.
        assert '/static/vendor/katex/katex.min.js' in page
        head_close_idx = page.lower().find('</head>')
        katex_idx = page.find('katex.min.js')
        assert 0 <= katex_idx < head_close_idx

    def test_raw_html_csp_blocks_scripts(self, flask_app, flask_client):
        from lib.artifacts import create_artifact
        # Even if HTML contains <script>, the CSP sandbox response header
        # ensures a top-level navigation can't execute it.  We assert the
        # header — runtime enforcement is the browser's job.
        body = '<html><body><script>alert(1)</script>hi</body></html>'
        with flask_app.app_context():
            meta = create_artifact(conv_id='conv-csp', content=body,
                                   format='html', source='write_file')
        r = flask_client.get(f'/api/artifacts/{meta["id"]}/raw')
        assert r.status_code == 200
        assert r.headers['Content-Type'].startswith('text/html')
        csp = r.headers['Content-Security-Policy']
        assert 'sandbox' in csp
        # No allow-scripts in the sandbox directive.
        assert 'allow-scripts' not in csp
        # No script-src 'unsafe-inline' relaxation.
        assert "script-src 'unsafe-inline'" not in csp
