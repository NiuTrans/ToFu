"""Tests for artifact-meta field-level sanitization.

Regression guard for the previously no-op ``_strip_meta`` /
``_strip_meta_for_response`` placeholders. The artifact-meta row carries a
free-form internal ``meta`` dict (producer bookkeeping: word_count,
has_scripts, toolName, …) which the frontend never reads and which must NOT
leak over the public API. ``public_meta`` is a WHITELIST filter so any future
internal field is dropped by default.

Run:  pytest tests/test_artifacts_meta_sanitize.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

pytest_plugins = ('tests._artifact_sidecar',)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
class TestPublicMetaUnit:
    """public_meta whitelist behaviour (pure function, no DB)."""

    def test_strips_internal_meta_dict(self):
        from lib.artifacts import public_meta
        row = {
            'id': 'a1', 'conv_id': 'c1', 'format': 'markdown',
            'title': 'r.md', 'source_ref': {'path': 'r.md'},
            'meta': {'word_count': 42, 'has_scripts': True,
                     'toolName': 'write_file'},
        }
        out = public_meta(row)
        assert 'meta' not in out
        # Public fields survive untouched.
        assert out['id'] == 'a1'
        assert out['source_ref'] == {'path': 'r.md'}
        assert out['title'] == 'r.md'

    def test_whitelist_drops_unknown_future_field(self):
        """A NEW internal field must be dropped by default (whitelist, not
        blacklist) — this is the guarantee that stops silent leaks."""
        from lib.artifacts import public_meta
        row = {'id': 'a1', 'conv_id': 'c1',
               '_internal_secret_path': '/mnt/your-fs',
               'server_debug_blob': {'k': 'v'}}
        out = public_meta(row)
        assert '_internal_secret_path' not in out
        assert 'server_debug_blob' not in out
        assert out == {'id': 'a1', 'conv_id': 'c1'}

    def test_content_passes_through_when_present(self):
        from lib.artifacts import public_meta
        assert public_meta({'id': 'a', 'content': 'body'})['content'] == 'body'

    def test_empty_input(self):
        from lib.artifacts import public_meta
        assert public_meta({}) == {}
        assert public_meta(None) == {}

    def test_all_row_to_meta_fields_classified(self):
        """Every key _row_to_meta can emit is either whitelisted public OR a
        known internal field — no field is unclassified (forgotten)."""
        from lib.artifacts.core import _PUBLIC_META_FIELDS
        row_to_meta_keys = {
            'id', 'conv_id', 'task_id', 'msg_id', 'source', 'source_ref',
            'format', 'title', 'content_sha256', 'size_bytes', 'version',
            'parent_id', 'pinned', 'meta', 'created_at', 'content',
        }
        known_internal = {'meta'}
        leftover = row_to_meta_keys - _PUBLIC_META_FIELDS - known_internal
        assert leftover == set(), f'unclassified meta fields: {leftover}'


@pytest.mark.unit
class TestRouteSanitization:
    """The live API routes must apply public_meta — the internal meta dict
    must not appear in any artifact response."""

    def _make(self, flask_app, conv_id, **kw):
        from lib.artifacts import create_artifact
        with flask_app.app_context():
            return create_artifact(
                conv_id=conv_id, content=kw.pop('content', '# hi\n'),
                format='markdown', source='write_file',
                source_ref={'path': kw.pop('path', 'r.md')},
                title='r.md',
                meta={'word_count': 3, 'has_scripts': False,
                      'toolName': 'write_file'},
                **kw,
            )

    def test_get_meta_strips_internal_meta(self, flask_app, flask_client):
        m = self._make(flask_app, 'conv-sanit-get')
        r = flask_client.get(f'/api/v1/artifacts/{m["id"]}')
        assert r.status_code == 200
        body = r.get_json()
        assert 'meta' not in body
        assert body['id'] == m['id']
        assert body['source_ref'] == {'path': 'r.md'}

    def test_list_by_conv_strips_internal_meta(self, flask_app, flask_client):
        self._make(flask_app, 'conv-sanit-list')
        r = flask_client.get('/api/v1/artifacts?conv=conv-sanit-list')
        assert r.status_code == 200
        arts = r.get_json()['artifacts']
        assert arts
        assert all('meta' not in a for a in arts)

    def test_versions_strip_internal_meta(self, flask_app, flask_client):
        from lib.artifacts import create_artifact
        cid = 'conv-sanit-ver'
        with flask_app.app_context():
            create_artifact(conv_id=cid, content='v1\n', format='markdown',
                            source='write_file', source_ref={'path': 'x.md'},
                            meta={'word_count': 1})
            v2 = create_artifact(conv_id=cid, content='v2\n', format='markdown',
                                 source='write_file', source_ref={'path': 'x.md'},
                                 meta={'word_count': 2})
        r = flask_client.get(f'/api/v1/artifacts/{v2["id"]}/versions')
        assert r.status_code == 200
        versions = r.get_json()['versions']
        assert versions
        assert all('meta' not in v for v in versions)

    def test_pin_response_strips_internal_meta(self, flask_app, flask_client):
        m = self._make(flask_app, 'conv-sanit-pin')
        r = flask_client.post(f'/api/v1/artifacts/{m["id"]}/pin',
                              json={'pinned': True})
        assert r.status_code == 200
        body = r.get_json()
        assert 'meta' not in body
        assert body['pinned'] is True
