"""Actual native transport and both deployed images fence application cohorts."""
import hashlib
import pytest
from benchmarks.native_application_authority import (
    authority_witness,check_authority_files,observe_runtime_authority,check_authority,
)

pytestmark=pytest.mark.unit


def authority(transport):
    result=dict(backend='sqlite' if transport=='sidecar' else 'tofudb',
        transport=transport,runtime_observation_required=True)
    if transport=='embedded':result.update(engine_sha256='a'*64,library_sha256='b'*64)
    return result


@pytest.mark.parametrize('transport',['sidecar','daemon','embedded'])
def test_observed_ready_transport_survives_receipt_validation(transport):
    expected=authority(transport)
    state=dict(ready=True,backend=expected['backend'],pid=None if transport=='embedded' else 123)
    if transport=='embedded':state['transport']='embedded'
    observed=observe_runtime_authority(state,expected)
    check_authority(dict(storage_authority=expected,storage_runtime=observed,storage_runtime_closed=True),expected)
    with pytest.raises(ValueError,match='runtime observation'):
        check_authority(dict(storage_authority=expected),expected)


@pytest.mark.parametrize('change',[
    {'ready':False},{'backend':'sqlite'},{'transport':'daemon'}, {'pid':123},
])
def test_embedded_observation_rejects_missing_or_different_runtime(change):
    status=dict(ready=True,backend='tofudb',transport='embedded',pid=None)
    status.update(change)
    with pytest.raises(ValueError,match='actual storage runtime'):
        observe_runtime_authority(status,authority('embedded'))


@pytest.mark.parametrize('pid',[None,False,0,-1,'123'])
def test_daemon_observation_requires_an_actual_child(pid):
    with pytest.raises(ValueError,match='actual storage runtime'):
        observe_runtime_authority(dict(ready=True,backend='tofudb',pid=pid),authority('daemon'))


@pytest.mark.parametrize('backend',['daemon','embedded'])
def test_native_requires_explicit_engine(backend,tmp_path):
    with pytest.raises(ValueError,match='explicit storage engine'):
        authority_witness(backend,None,tmp_path)


def test_embedded_pair_is_validated_and_both_pins_are_frozen(tmp_path,monkeypatch):
    from serverctl_pkg import native_engine
    binary=tmp_path/'tofu-db';binary.write_bytes(b'binary')
    library=tmp_path/'libtofu_db.so';library.write_bytes(b'library')
    calls=[]
    monkeypatch.setattr(native_engine,'verify_current',lambda binary,root:calls.append('source'))
    monkeypatch.setattr(native_engine,'verify_library',lambda binary:calls.append('pair'))
    expected=authority_witness('embedded',binary,tmp_path)
    assert calls==['source','pair']
    assert expected['library_sha256']==hashlib.sha256(b'library').hexdigest()
    check_authority_files(binary,expected)
    links=tmp_path/'links';links.mkdir()
    linked=links/'tofu-db';linked.symlink_to(binary)
    check_authority_files(linked,expected)
    library.write_bytes(b'changed')
    with pytest.raises(ValueError,match='embedded storage library changed'):
        check_authority_files(binary,expected)
    binary.write_bytes(b'changed')
    with pytest.raises(ValueError,match='storage engine changed'):
        check_authority_files(binary,expected)


def test_embedded_pair_refusal_is_not_a_daemon_fallback(tmp_path,monkeypatch):
    from serverctl_pkg import native_engine
    binary=tmp_path/'tofu-db';binary.write_bytes(b'binary')
    monkeypatch.setattr(native_engine,'verify_current',lambda *_:None)
    def refuse(*_):raise ValueError('injected pair mismatch')
    monkeypatch.setattr(native_engine,'verify_library',refuse)
    with pytest.raises(ValueError,match='pair mismatch'):
        authority_witness('embedded',binary,tmp_path)


def test_memory_rejects_unobserved_embedded_selection():
    from benchmarks.native_root_memory import summary
    from tests.meta.test_native_root_memory import rows
    data=rows();expected=authority('embedded')
    for row in data:
        row.update(storage_authority=expected,storage_runtime_closed=True,
            storage_runtime=dict(ready=True,backend='tofudb',transport='embedded',pid=None))
    assert summary(data,100,20,storage_authority=expected)['verdict']=='qualified_memory_component'
    data[0].pop('storage_runtime')
    assert summary(data,100,20,storage_authority=expected)['verdict']=='invalid'


def test_historical_declared_authority_remains_recomputable():
    expected=dict(backend='tofudb',transport='daemon',engine_sha256='a'*64)
    check_authority(dict(storage_authority=expected),expected)


@pytest.mark.parametrize('closed',[None,False,1])
def test_declared_ready_runtime_cannot_hide_unjoined_close(closed):
    expected=authority('embedded')
    row=dict(storage_authority=expected,storage_runtime_closed=closed,
        storage_runtime=dict(ready=True,backend='tofudb',transport='embedded',pid=None))
    with pytest.raises(ValueError,match='close was not witnessed'):
        check_authority(row,expected)


@pytest.mark.parametrize('field',['engine_sha256','library_sha256','runtime_observation_required'])
def test_legacy_receipt_cannot_be_relabelled_as_embedded(field):
    expected=authority('embedded');expected.pop(field)
    with pytest.raises(ValueError,match='both pins and runtime observation'):
        check_authority(dict(storage_authority=expected),expected)
