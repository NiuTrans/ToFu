"""Tests for Step 5 versioning + library + PDF export.

  • Versioning: same conv + same path + DIFFERENT content → version N+1
    with parent_id linking the previous row.  Same content still dedupes.
  • list_versions chain walks parent_id correctly.
  • Library listing: pinned first, then recent.
  • PDF export route 503s when Playwright is not available, 404s on
    missing artifact.

PDF rendering itself is exercised in an integration smoke test (skipped
when Chromium isn't installed) so unit suite stays hermetic.

Run:  pytest tests/test_artifacts_versioning.py -v
"""
from __future__ import annotations

import uuid

import pytest


# ─── Versioning ───────────────────────────────────────────────────────

class TestVersioning:
    def test_same_path_different_content_links_versions(self, flask_app):
        from lib.artifacts import create_artifact, list_versions
        cid = 'conv-ver-' + uuid.uuid4().hex[:8]
        path = 'report.md'
        with flask_app.app_context():
            v1 = create_artifact(
                conv_id=cid, content='# v1\n', format='markdown',
                source='write_file', source_ref={'path': path}, title=path,
            )
            v2 = create_artifact(
                conv_id=cid, content='# v2 — updated\n', format='markdown',
                source='write_file', source_ref={'path': path}, title=path,
            )
            v3 = create_artifact(
                conv_id=cid, content='# v3 — final\n', format='markdown',
                source='write_file', source_ref={'path': path}, title=path,
            )
        assert v1['version'] == 1
        assert v1['parent_id'] == ''
        assert v2['version'] == 2
        assert v2['parent_id'] == v1['id']
        assert v3['version'] == 3
        assert v3['parent_id'] == v2['id']

        with flask_app.app_context():
            chain = list_versions(v3['id'])
        assert [m['id'] for m in chain] == [v1['id'], v2['id'], v3['id']]

        # Anchor at the middle node — chain still rebuilds full
        with flask_app.app_context():
            chain2 = list_versions(v2['id'])
        assert [m['id'] for m in chain2] == [v1['id'], v2['id'], v3['id']]

    def test_same_path_same_content_dedupes(self, flask_app):
        """Versioning kicks in on different sha; identical content still dedupes."""
        from lib.artifacts import create_artifact
        cid = 'conv-ver-dedupe-' + uuid.uuid4().hex[:8]
        with flask_app.app_context():
            a = create_artifact(
                conv_id=cid, content='# same\n', format='markdown',
                source='write_file', source_ref={'path': 'r.md'}, title='r.md',
            )
            b = create_artifact(
                conv_id=cid, content='# same\n', format='markdown',
                source='write_file', source_ref={'path': 'r.md'}, title='r.md',
            )
        assert a['id'] == b['id']  # dedupe
        assert b['version'] == 1
        assert b['parent_id'] == ''

    def test_no_path_no_versioning(self, flask_app):
        """source_ref without 'path' (e.g. inline_fence) gets version 1."""
        from lib.artifacts import create_artifact
        cid = 'conv-ver-nopath-' + uuid.uuid4().hex[:8]
        with flask_app.app_context():
            a = create_artifact(
                conv_id=cid, content='# a\n', format='markdown',
                source='inline_fence', source_ref={'kind': 'fence', 'fence_index': 0},
            )
            b = create_artifact(
                conv_id=cid, content='# b\n', format='markdown',
                source='inline_fence', source_ref={'kind': 'fence', 'fence_index': 1},
            )
        assert a['version'] == 1
        assert b['version'] == 1
        assert a['parent_id'] == ''
        assert b['parent_id'] == ''

    def test_versions_route(self, flask_app, flask_client):
        from lib.artifacts import create_artifact
        cid = 'conv-ver-route-' + uuid.uuid4().hex[:8]
        with flask_app.app_context():
            v1 = create_artifact(
                conv_id=cid, content='one\n', format='markdown',
                source='write_file', source_ref={'path': 'x.md'}, title='x.md',
            )
            v2 = create_artifact(
                conv_id=cid, content='two\n', format='markdown',
                source='write_file', source_ref={'path': 'x.md'}, title='x.md',
            )
        r = flask_client.get(f'/api/artifacts/{v2["id"]}/versions')
        assert r.status_code == 200
        body = r.get_json()
        assert body['count'] == 2
        ids = [v['id'] for v in body['versions']]
        assert ids == [v1['id'], v2['id']]

    def test_versions_route_404(self, flask_client):
        r = flask_client.get('/api/artifacts/nope-xyz/versions')
        assert r.status_code == 404


# ─── Library listing ─────────────────────────────────────────────────

class TestLibrary:
    def test_pinned_first_then_recent(self, flask_app, flask_client):
        from lib.artifacts import create_artifact, set_pinned
        # Make sure we have at least one pinned + one unpinned in the DB.
        cid = 'conv-lib-' + uuid.uuid4().hex[:8]
        with flask_app.app_context():
            a = create_artifact(conv_id=cid, content='unpinned\n',
                                format='markdown', source='write_file',
                                source_ref={'path': 'u.md'})
            b = create_artifact(conv_id=cid, content='pinned\n',
                                format='markdown', source='write_file',
                                source_ref={'path': 'p.md'})
            set_pinned(b['id'], True)
        r = flask_client.get('/api/artifacts/library?limit=80')
        assert r.status_code == 200
        body = r.get_json()
        ids = [m['id'] for m in body['artifacts']]
        # b is pinned → ranks above a regardless of timestamp.
        assert b['id'] in ids
        assert a['id'] in ids
        assert ids.index(b['id']) < ids.index(a['id'])


# ─── PDF export route (integration-light) ────────────────────────────

class TestPdfExportRoute:
    def test_pdf_404_on_missing(self, flask_client):
        r = flask_client.get('/api/artifacts/no-such-pdf/export?format=pdf')
        assert r.status_code in (404, 503)  # 503 if Playwright also missing

    def test_pdf_unsupported_format(self, flask_app, flask_client):
        from lib.artifacts import create_artifact
        cid = 'conv-pdf-ufmt-' + uuid.uuid4().hex[:8]
        with flask_app.app_context():
            meta = create_artifact(conv_id=cid, content='hi\n',
                                   format='markdown', source='write_file')
        r = flask_client.get(f'/api/artifacts/{meta["id"]}/export?format=docx')
        assert r.status_code == 400


# ─── Smoke test: PDF render path ─────────────────────────────────────
# Skipped when Playwright/Chromium isn't installed.  When Chromium is
# present, this exercises the route end-to-end and confirms a real PDF
# header in the returned bytes.

@pytest.mark.skipif(
    not __import__('importlib').util.find_spec('playwright'),
    reason='playwright not installed',
)
class TestPdfRenderSmoke:
    def test_real_pdf_header(self, flask_app, flask_client):
        # Quick probe: try to launch Chromium briefly.  If that fails
        # (binary missing on this host), skip.
        try:
            from tofu_search.fetch.playwright_pool import _pw_pool
            ok = _pw_pool._ensure_thread()
        except Exception as e:
            pytest.skip(f'playwright unavailable: {e}')
        if not ok:
            pytest.skip('playwright thread not ready (chromium binary missing?)')

        from lib.artifacts import create_artifact
        cid = 'conv-pdf-smoke-' + uuid.uuid4().hex[:8]
        with flask_app.app_context():
            meta = create_artifact(
                conv_id=cid,
                content='# Hello\n\nWorld.\n',
                format='markdown',
                source='write_file',
                source_ref={'path': 'r.md'},
                title='r.md',
            )
        r = flask_client.get(f'/api/artifacts/{meta["id"]}/export?format=pdf')
        assert r.status_code == 200
        assert r.headers['Content-Type'].startswith('application/pdf')
        body = r.get_data()
        assert body[:5] == b'%PDF-'
