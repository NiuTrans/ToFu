"""Owned source settings through actual SQLite/native daemon/embedded app adapters."""
import pytest

from lib import credentials_vault as vault
from lib.automations.management import AutomationManagement
from lib.identity import PrincipalContext
from lib.skills.automation.source_settings import AutomationSkillSources
from tests.storage.test_storage_application_contract import application_storage as application_storage
from tests.storage.test_storage_application_contract import application_storage_owner_id as application_storage_owner_id

pytestmark = pytest.mark.api


def test_sealed_source_replay_and_retirement_through_application(application_storage, tmp_path, monkeypatch):
    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'source-vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / '.source-vault.key')
    monkeypatch.setattr(vault, '_fernet', None)
    principal = PrincipalContext.user(subject_id='source-app-owner', owner_user_id=1,
        tenant_id='source-app', scopes={'automations:read', 'automations:manage', 'skills:install', 'skills:read'})
    manager = AutomationManagement(principal)
    body = {'environment': 'prod', 'active': True, 'credential': 'private-app-fixture-0123456789',
            'expectedRevision': 0, 'requestId': 'source-app-register'}
    first = manager.update_skill_source(body)
    assert first == manager.skill_source() == manager.update_skill_source(body)
    assert first['revision'] == 1 and first['credentialConfigured'] is True
    source, credential = AutomationSkillSources(principal).credential()
    assert source.version == 1 and credential.token == body['credential']
    assert len(vault.list_entries()) == 1
    retired = manager.update_skill_source({**body, 'credential': None, 'active': False,
        'expectedRevision': 1, 'requestId': 'source-app-retire'})
    assert retired['revision'] == 2 and not retired['credentialConfigured']
    assert manager.skill_source() == retired
    assert vault.list_entries() == []
