"""Flow release selection preserves archive integrity and optional ABI boundaries."""
import ctypes
import hashlib
import json
import os
from unittest.mock import patch
import pytest
from scripts.build_native_runtime import SPEC,write_archive
from lib.agent_core.native_artifact import inspect_archive,seal_flow_archive,seal_token_count_archive
from lib.agent_core.native_process import NativeConfigurationError
from lib.orchestration.native_flow_contract import SPEC as FLOW_SPEC
from lib.agent_core.native_token_count_contract import TOKEN_COUNT_SPEC
from tests.release.test_native_runtime_archive import members

pytestmark=pytest.mark.unit


def archive(tmp_path,*,flow=True,token=False,mutation=None):
    files=members()
    for enabled,key,contract in ((flow,'optional_flow',FLOW_SPEC),(token,'optional_token_count',TOKEN_COUNT_SPEC)):
        if enabled:
            extension=SPEC[key]
            files[extension['member']]=b'\x7fELFlibrary'
            files[extension['contract_member']]=json.dumps(contract).encode()
    if mutation:mutation(files)
    manifest=json.loads(files['manifest.json'])
    manifest['files']={name:dict(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw)) for name,raw in files.items() if name!='manifest.json'}
    files['manifest.json']=json.dumps(manifest).encode()
    path=tmp_path/'release.tar.gz';write_archive(files,path)
    return path,hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('flow,token',[(True,False),(False,True),(True,True),(False,False)])
def test_optional_capability_selection_is_independent(tmp_path,flow,token):
    path,pin=archive(tmp_path,flow=flow,token=token)
    inspect_archive(path)
    for selected,seal in ((flow,seal_flow_archive),(token,seal_token_count_archive)):
        if not selected:
            with pytest.raises(NativeConfigurationError,match='selected .* missing'):
                seal(path,pin,'fixture',1024)
        else:
            with seal(path,pin,'fixture',1024) as image:
                assert image.read()==b'\x7fELFlibrary'
                with pytest.raises(PermissionError):os.write(image.fileno(),b'changed')


@pytest.mark.parametrize('mutation',[
    lambda f:f.pop(SPEC['optional_flow']['member']),
    lambda f:f.pop(SPEC['optional_flow']['contract_member']),
    lambda f:f.__setitem__(SPEC['optional_flow']['member'],b'bad ELF'),
    lambda f:f.__setitem__(SPEC['optional_flow']['contract_member'],json.dumps(dict(FLOW_SPEC,abi_version=99)).encode()),
    lambda f:f.__setitem__(SPEC['optional_flow']['contract_member'],json.dumps(dict(FLOW_SPEC,statuses={})).encode()),
])
def test_malformed_flow_extension_refuses_before_code_loading(tmp_path,mutation):
    path,pin=archive(tmp_path,mutation=mutation)
    with pytest.raises(NativeConfigurationError):seal_flow_archive(path,pin,'fixture',1024)


def test_flow_snapshot_survives_source_replacement_and_admission_failure_closes_descriptor(tmp_path):
    path,pin=archive(tmp_path)
    with seal_flow_archive(path,pin,'fixture',1024) as image:
        path.write_bytes(b'replaced')
        assert image.read()==b'\x7fELFlibrary'
    with pytest.raises(NativeConfigurationError,match='checksum'):seal_flow_archive(path,pin,'fixture',1024)


def test_flow_binding_releases_snapshot_after_loader_failure(tmp_path):
    from lib.orchestration.native_flow import NativeFlowLibrary
    path,pin=archive(tmp_path);descriptors=[]
    original=ctypes.CDLL
    def fail(path,*args,**kwargs):
        if not isinstance(path,str) or not path.startswith('/proc/self/fd/'):
            return original(path,*args,**kwargs)
        descriptors.append(int(path.rsplit('/',1)[1]));raise OSError('injected load failure')
    before=set(os.listdir('/proc/self/fd'))
    with patch('lib.orchestration.native_flow.c.CDLL',side_effect=fail):
        with pytest.raises(OSError,match='injected'):
            NativeFlowLibrary.from_archive(archive=path,sha256=pin,application_version='fixture',maximum_bytes=1024)
    assert len(descriptors)==1
    with pytest.raises(OSError):os.fstat(descriptors[0])
    assert set(os.listdir('/proc/self/fd'))==before


def test_installed_archive_shares_capabilities_and_joins_before_unload(monkeypatch):
    from pathlib import Path
    from lib.orchestration.native_flow import NativeFlowLibrary
    from lib.agent_core.native_token_count import NativeTokenCounter
    from tests.orchestration import test_native_flow_lifetime as lifetime
    path=os.environ.get('TOFU_NATIVE_FLOW_TEST_ARCHIVE')
    if not path:pytest.skip('explicit verified combined archive required')
    path=Path(path);manifest,_=inspect_archive(path)
    options=dict(archive=path,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        application_version=manifest['build']['application_version'])
    before=set(os.listdir('/proc/self/fd'))
    monkeypatch.setattr(lifetime,'library',lambda:NativeFlowLibrary.from_archive(**options,maximum_bytes=64<<20))
    lifetime.test_library_close_cannot_unload_a_graph_opened_by_an_accepted_call(monkeypatch)
    with monkeypatch.context() as scoped:
        scoped.setattr(NativeFlowLibrary,'_bind',lambda _: (_ for _ in ()).throw(RuntimeError('injected ABI bind failure')))
        with pytest.raises(RuntimeError,match='injected ABI'):
            NativeFlowLibrary.from_archive(**options,maximum_bytes=64<<20)
    for _ in range(3):
        runtime=NativeFlowLibrary.from_archive(**options,maximum_bytes=64<<20)
        counter=NativeTokenCounter(**options,binary_bytes=64<<20)
        try:
            assert counter.count_batch(['hello world'],encoding='cl100k_base')==[2]
        finally:counter.close();runtime.close()
    assert set(os.listdir('/proc/self/fd'))==before
