"""Read-only catalog contracts: provider identity, bounds and no durable mutations."""
import pytest
from tofu_environments.jobs import JobService
from tofu_environments.contracts import Actor,EnvironmentError
pytestmark=pytest.mark.unit
class NoWrites:
    def __getattr__(self,name):raise AssertionError('Catalog must not access repository')
def payload():
    return {'version':1,'provider':'fixture','queues':[],'provider_options_schema':{},'provider_options_defaults':{},'availability_is_admission_guarantee':False}
def test_catalog_read_only_actor_bound():
    actor=Actor(7,'tenant','/project');seen=[]
    class Provider:
        def compute_catalog(self,a):seen.append(a);return payload()
    service=JobService(NoWrites(),lambda a,p:Provider())
    assert service.catalog(actor,'fixture')==payload() and seen==[actor]
@pytest.mark.parametrize('change',['identity','nan','oversize','guarantee'])
def test_invalid_catalog_rejected(change):
    value=payload()
    if change=='identity':value['provider']='foreign'
    if change=='nan':value['provider_options_defaults']={'bad':float('nan')}
    if change=='oversize':value['provider_options_defaults']={'bad':'x'*65536}
    if change=='guarantee':value['availability_is_admission_guarantee']=True
    class Provider:
        def compute_catalog(self,a):return value
    with pytest.raises(EnvironmentError) as error:JobService(NoWrites(),lambda *_:Provider()).catalog(Actor(7),'fixture')
    assert error.value.code=='catalog_invalid'
def test_old_provider_explicitly_unsupported():
    with pytest.raises(EnvironmentError) as error:JobService(NoWrites(),lambda *_:object()).catalog(Actor(7),'fixture')
    assert error.value.code=='catalog_unsupported'
