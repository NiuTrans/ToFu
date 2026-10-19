"""Already-encoded worker envelopes preserve identity, bytes and refusal fences."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.agent_core.native_process import (
    NativeConfigurationError, NativeProcessClient, NativeProcessError,
    _EncodedObjectFrame,
)
from lib.agent_core.native_worker_pool import _Input, _wire
from tests.agent_core.test_native_process import budget, principal

pytestmark = pytest.mark.unit


def encode(value, limit=65536):
    client = NativeProcessClient(binary=Path('/not-acquired'), sha256='0'*64,
        protocol_version='fixture/v1', principal=principal(), budget=budget())
    return client._encode(value, limit)


def stream(limit=65536, *, first=True):
    packets=[]
    worker=SimpleNamespace(owner=7, budget=SimpleNamespace(
        configuration_bytes=limit, frame_bytes=limit),
        process=SimpleNamespace(stdin=SimpleNamespace(write=packets.append)))
    lease=SimpleNamespace(worker=worker,sequence=3,done=SimpleNamespace(done=lambda:False))
    result=_Input(lease);result.first=first
    return result,packets


@pytest.mark.parametrize('value', [
    {'type':'configure','owner_user_id':7,'text':'汉字🙂\\\"\n'},
    {'type':'configure','owner_user_id':7,'numbers':[1,1.2345,1e-30,-0.0,None,True]},
    {'type':'configure','owner_user_id':7,'nested':{'a':[{'b':'payload'}]}},
])
def test_encoded_frame_has_exact_canonical_envelope_and_no_source_alias(value):
    raw=encode(value);original=json.loads(raw)
    value['mutated']='after encoding'
    output,packets=stream(len(raw)-1)
    output.write(raw)
    assert packets==[_wire('worker_run',7,3,original)]
    assert output.first is False
    with pytest.raises(TypeError):_EncodedObjectFrame(b'{}\n')
    with pytest.raises(AttributeError):raw.identity='forged'


def test_control_envelope_uses_validated_bytes_without_json_redecoding(monkeypatch):
    from lib.agent_core import native_worker_pool
    value={'type':'round_decision','messages':[{'content':'汉字🙂'}]}
    raw=encode(value);expected=_wire('worker_control',7,3,value)
    output,packets=stream(len(raw)-1,first=False)
    def forbid(*_args,**_kwargs):raise AssertionError('validated bytes decoded again')
    monkeypatch.setattr(native_worker_pool.json,'loads',forbid)
    output.write(raw)
    assert packets==[expected]


@pytest.mark.parametrize('change',[{'owner_user_id':8},{'type':'cancel'}])
def test_encoded_first_frame_still_checks_actual_identity(change):
    value=dict(type='configure',owner_user_id=7);value.update(change)
    output,packets=stream()
    with pytest.raises(NativeProcessError,match='owned initial configuration'):
        output.write(encode(value))
    assert not packets and output.first


@pytest.mark.parametrize('encoded',[False,True])
@pytest.mark.parametrize('first',[False,True])
def test_budget_refusal_does_not_consume_the_initial_frame(encoded,first):
    value=dict(type='configure',owner_user_id=7,payload='汉字🙂')
    raw=encode(value)
    if not encoded:raw=bytes(raw)
    output,packets=stream(len(raw)-2,first=first)
    with pytest.raises(NativeProcessError,match='original budget'):
        output.write(raw)
    assert not packets and output.first is first


@pytest.mark.parametrize('raw,error',[
    (b'[]\n',NativeProcessError), (b'{bad}\n',ValueError),
    (b'{"x":NaN}\n',ValueError), (b'{"x":1e999}\n',ValueError),
    (b'{"x":"\\ud800"}\n',UnicodeError), (b'{}',NativeProcessError),
])
def test_unmarked_bytes_keep_full_validation_and_encoding(raw,error):
    output,packets=stream(first=False)
    with pytest.raises(error):output.write(raw)
    assert not packets


@pytest.mark.parametrize('value', [{'x':float('nan')},{'x':float('inf')},{'x':'\ud800'}])
def test_invalid_values_cannot_acquire_encoding_provenance(value):
    with pytest.raises(NativeConfigurationError,match='cannot be encoded'):
        encode(value)


def test_original_structure_and_exact_utf8_budgets_remain_enforced():
    value={'x':'汉字🙂'};raw=encode(value)
    assert encode(value,len(raw)-1)==raw
    with pytest.raises(NativeConfigurationError,match='budget'):
        encode(value,len(raw)-2)
    cycle={};cycle['child']=cycle
    with pytest.raises(NativeConfigurationError,match='structure'):
        encode(cycle)
    assert type(encode([1,2])) is bytes


def test_transport_write_failure_keeps_initial_admission_unconsumed():
    output,packets=stream()
    def fail(_raw):raise BrokenPipeError('injected write failure')
    output.lease.worker.process.stdin.write=fail
    with pytest.raises(BrokenPipeError):output.write(encode(dict(type='configure',owner_user_id=7)))
    assert output.first and not packets
