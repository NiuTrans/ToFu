"""Independent JSON Schema consumer for the explicit persistent native transport."""
import json
from pathlib import Path
import jsonschema
import pytest

SCHEMA=json.loads((Path(__file__).resolve().parents[2]/'contracts/native_agent_stdio_v1.schema.json').read_text())


@pytest.mark.parametrize('kind',['worker_run','worker_control','worker_event','worker_ready','worker_shutdown','worker_closed'])
def test_worker_schema_has_closed_scope_and_accepts_its_declared_frame(kind):
    frame={'version':'tofu.native-agent-worker/v1','type':kind,'owner_user_id':7,'sequence':0 if kind=='worker_ready' else 1}
    if kind=='worker_run':frame['configuration']={'type':'configure'}
    if kind in ('worker_control','worker_event'):frame['frame']={'type':'cancel'}
    if kind=='worker_ready':frame['cached_encodings']=0
    validator=jsonschema.Draft202012Validator(SCHEMA['$defs'][kind])
    validator.validate(frame)
    for changed in [dict(frame,owner_user_id=0),dict(frame,sequence=-1),dict(frame,version='foreign'),dict(frame,foreign=True)]:
        with pytest.raises(jsonschema.ValidationError):validator.validate(changed)
    if kind!='worker_ready':
        with pytest.raises(jsonschema.ValidationError):validator.validate(dict(frame,sequence=0))


def test_worker_control_rejects_missing_scope_or_nonobject_inner_frame():
    validator=jsonschema.Draft202012Validator(SCHEMA['$defs']['worker_control'])
    base={'version':'tofu.native-agent-worker/v1','type':'worker_control','owner_user_id':7,'sequence':1,'frame':{}}
    for missing in ['owner_user_id','sequence','frame']:
        changed=dict(base);changed.pop(missing)
        with pytest.raises(jsonschema.ValidationError):validator.validate(changed)
    with pytest.raises(jsonschema.ValidationError):validator.validate(dict(base,frame='unframed'))
