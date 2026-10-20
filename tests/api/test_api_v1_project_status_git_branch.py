"""Conv-scoped project status keeps enrichment before a turn registers roots.

The per-conversation root registry in ``lib.project_mod.config`` is an
EPHEMERAL execution cache: it is populated when a turn runs and it does not
survive a restart. A conversation reopened from saved settings therefore has no
registry, and every field the turn rail renders from the root — the
checked-out git branch above all — used to be missing until the next turn.
"""

import subprocess
import uuid

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


def _create_conversation(settings):
    from lib.storage import get_storage_client

    conv_id = 'projstatus' + uuid.uuid4().hex[:10]
    get_storage_client(write=True).command('conversation.create', {
        'conv_id': conv_id, 'user_id': 1, 'title': 'Project status',
        'created_at': 1, 'updated_at': 1, 'settings': settings,
    }, conv_id + '-create')
    return conv_id


def test_saved_root_supplies_branch_before_any_turn(flask_client, chat_sidecar, tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', '-b', 'feature/rail', str(repo)], check=True)
    conv_id = _create_conversation({'projectPath': str(repo), 'projectPaths': [str(repo)]})

    response = flask_client.get('/api/v4/project/status',
                                query_string={'conv_id': conv_id})
    body = response.get_json()
    assert response.status_code == 200, body
    data = body.get('data', body)

    assert data['path'] == str(repo)
    assert data['gitBranch'] == 'feature/rail'
    assert data['gitBranches'] == [{'path': str(repo), 'branch': 'feature/rail'}]


def test_conversation_without_roots_keeps_the_global_view(flask_client, chat_sidecar):
    conv_id = _create_conversation({})

    response = flask_client.get('/api/v4/project/status',
                                query_string={'conv_id': conv_id})
    body = response.get_json()
    assert response.status_code == 200, body
    # No saved root and no registry: the payload stays the legacy global view,
    # which must never inherit a root from another conversation.
    assert body.get('data', body).get('gitBranch') is None
