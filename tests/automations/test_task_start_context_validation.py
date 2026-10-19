"""Task-start admission matches the canonical full schema, including future fallback."""
import copy
import random
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator
from lib.automations import contract
from lib.automations.events import validate_start_context
from lib.automations.errors import AutomationError

pytestmark=pytest.mark.unit


def test_context_boundaries_and_nested_corruption_match_full_schema():
    schema=contract.storage_schema()['$defs']['taskStartedContext']
    oracle=Draft202012Validator(schema)
    valid={'tenantId':'租户😀','origin':'human','automationChain':['task-1'],'projectId':'project'}
    cases=[None,True,0,'',[],{},valid]
    for field in valid:
        missing=copy.deepcopy(valid);missing.pop(field);cases.append(missing)
        for value in [None,False,0,1.0,{},[],(),'', 'x'*257]:
            candidate=copy.deepcopy(valid);candidate[field]=value;cases.append(candidate)
    for length in [0,1,159,160,161,200,201,256,257]:
        for field in ['tenantId','projectId']:
            candidate=copy.deepcopy(valid);candidate[field]='😀'*length;cases.append(candidate)
        candidate=copy.deepcopy(valid);candidate['automationChain']=['😀'*length];cases.append(candidate)
    for count in [0,1,14,15,16]:
        candidate=copy.deepcopy(valid);candidate['automationChain']=['step']*count;cases.append(candidate)
    candidate=copy.deepcopy(valid);candidate['unknown']=True;cases.append(candidate)
    randomizer=random.Random(173)
    values=[None,False,True,0,1,'human','automation','',{},[],[''],['ok'],['ok',False]]
    for _ in range(500):
        candidate={key:copy.deepcopy(randomizer.choice(values)) for key in valid if randomizer.randrange(3)}
        cases.append(candidate)
    for value in cases:
        accepted=oracle.is_valid(value)
        assert contract.task_started_context_is_valid(value)==accepted,repr(value)
        if accepted:validate_start_context(value)
        else:
            with pytest.raises(AutomationError,match='Invalid task-start'):
                validate_start_context(value)


def test_future_unrecognized_schema_semantics_use_full_validator(monkeypatch):
    schema=copy.deepcopy(contract.storage_schema()['$defs']['taskStartedContext'])
    schema['properties']['tenantId']['pattern']='^allowed$'
    assert not contract._supports_small_context_schema(schema)
    monkeypatch.setattr(contract,'_task_started_context_schema',lambda:(schema,False))
    monkeypatch.setattr(contract,'storage_input_validator',lambda kind:Draft202012Validator(schema))
    assert contract.task_started_context_is_valid({'tenantId':'allowed','origin':'human','automationChain':[]})
    assert not contract.task_started_context_is_valid({'tenantId':'other','origin':'human','automationChain':[]})
    assert not contract._supports_small_context_schema({'type':'array','items':False})
    assert not contract._supports_small_context_schema({'required':['field']})
    assert not contract._supports_small_context_schema({'type':['string','null']})


def test_ordinary_start_context_does_not_import_optional_schema_engine():
    program="""
import sys
from lib.automations.events import validate_start_context,subscription_prefixes
value={'tenantId':'tenant','origin':'human','automationChain':[]}
validate_start_context(value)
assert subscription_prefixes(7,value)
assert 'jsonschema' not in sys.modules
"""
    result=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
