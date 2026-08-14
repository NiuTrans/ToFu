"""Server-owned subscription providers survive stale Settings snapshots."""

import pytest

pytestmark = pytest.mark.unit

from routes.config import _merge_server_owned_providers


def test_stale_snapshot_cannot_delete_or_mutate_managed_providers():
    oauth = {'id': 'oauth_codex', 'oauth': 'codex',
             'models': [{'model_id': 'gpt-5.6-sol'}]}
    adapter = {'id': 'adapter_deadbeef',
               'adapter': {'agent_id': 'deadbeef'},
               'models': [{'model_id': 'claude-opus-5'}]}
    existing = [{'id': 'old-user', 'models': []}, oauth, adapter]
    incoming = [
        {'id': 'new-user', 'models': [{'model_id': 'x'}]},
        {'id': 'oauth_codex', 'models': []},
        {'id': 'adapter_deadbeef', 'adapter': {'agent_id': 'wrong'},
         'models': []},
    ]
    merged = _merge_server_owned_providers(existing, incoming)
    assert [p['id'] for p in merged] == [
        'new-user', 'oauth_codex', 'adapter_deadbeef']
    assert merged[1] == oauth
    assert merged[2] == adapter


def test_reserved_missing_managed_id_cannot_be_forged_by_browser():
    merged = _merge_server_owned_providers([], [
        {'id': 'oauth_claude', 'models': [{'model_id': 'evil'}]},
        {'id': 'adapter_fake', 'models': [{'model_id': 'evil'}]},
        {'id': 'normal', 'models': []},
    ])
    assert merged == [{'id': 'normal', 'models': []}]
