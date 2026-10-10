"""Presentation publication through the actual daemon/embedded app adapters."""
import time
import pytest
from tests.storage.test_storage_application_contract import application_storage as application_storage
from tests.automations.test_time_publication import prepared, query

pytestmark=pytest.mark.api


def test_owned_presentation_through_selected_application_storage(application_storage,monkeypatch):
    from lib.storage import get_storage_client
    factory,repo,_,dispatch,completed,fence,created,attempt=prepared(
        (get_storage_client,1,None),monkeypatch)
    assert query(factory,repo,created)['items']==[]
    repo.advance(dispatch,completed,fence,now=time.time())
    item=query(factory,repo,created)['items'][0]
    assert item['attemptId']==attempt and item['output']==completed['steps']['show']['output']
