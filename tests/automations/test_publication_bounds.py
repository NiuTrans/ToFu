"""Cross-language publication byte proofs retain bounded JSON admission."""
import hashlib
import pytest
from lib.automations.definitions import prepare_definition
from lib.automations.contract import LIMITS
from lib.automations.publication import canonical
from lib.storage.errors import StorageError
from tests.automations.test_atomic_publication import setup,publication_payload

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('malformation',['unsafe_integer','inactive_source_overflow','changed_inactive_source'])
def test_publication_bounds_reject_complete_but_invalid_byte_proof(semantic_authority,malformation):
    factory,repo,rule=setup(semantic_authority)
    prepared=prepare_definition(rule)
    payload=publication_payload(repo,prepared)
    current=None
    if malformation == 'unsafe_integer':
        payload['record']['definition']['steps'][0]['when']={'source':{'from':'event','pointer':'/metric'},
            'operator':'eq','value':LIMITS['safeIntegerMax']+1}
        payload['definition_json']=canonical(payload['record']['definition'])
        payload['record']['compiled']['digest']=hashlib.sha256((payload['definition_json']+'\n'+payload['actions_json']).encode()).hexdigest()
    else:
        current=repo.publish(prepared,expected_version=0,now=100)
        disabled=prepared.definition;disabled['enabled']=False
        if malformation == 'changed_inactive_source': disabled['name']='Changed during pause'
        payload.update(mode='pause',record=None,expected_version=1,definition_json=canonical(disabled))
        if malformation == 'inactive_source_overflow': payload['definition_json']=' '*LIMITS['definitionBytes']+payload['definition_json']
    with pytest.raises(StorageError): factory(write=True).command('automation.definition.update',payload,None)
    saved=repo.get('definition',rule['id'])
    assert saved is None if current is None else saved.value == current.value
