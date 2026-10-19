"""Pinned archive loading owns exact bytes, bounded parsing and joined disposal."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import threading
from unittest.mock import patch

import pytest
from lib.agent_core.native_process import NativeProcessClient,NativeConfigurationError
from lib.agent_core.native_artifact import inspect_archive
from scripts.build_native_runtime import write_archive
from tests.release.test_native_runtime_archive import members
from tests.agent_core.test_native_process import principal,budget

pytestmark=pytest.mark.unit


def test_factory_requires_pin_and_version_but_does_not_open_archive():
    for version in ('',None):
        with pytest.raises(NativeConfigurationError):
            NativeProcessClient.from_archive(archive=Path('/missing'),sha256='0'*64,application_version=version,principal=principal(),budget=budget())
    client=NativeProcessClient.from_archive(archive=Path('/missing'),sha256='0'*64,application_version='fixture',principal=principal(),budget=budget())
    asyncio.run(client.close())


def test_pin_refusal_precedes_archive_decode_and_executable_acquisition(tmp_path):
    path=tmp_path/'archive';path.write_bytes(b'not an archive')
    with patch('lib.agent_core.native_artifact._memfd') as memfd:
        with pytest.raises(NativeConfigurationError,match='checksum'):
            inspect_archive(path,archive_sha256='0'*64,seal_image=True)
    memfd.assert_not_called()


@pytest.mark.parametrize('mode',['version','target','metadata','bool_size','binary_limit'])
def test_artifact_admission_refuses_without_executing(tmp_path,mode):
    files=members();manifest=json.loads(files['manifest.json'])
    if mode=='target':manifest['target']={}
    if mode=='metadata':files['contracts/native_agent_stdio_v1.schema.json']=b'[]'
    if mode=='bool_size':manifest['files']['LICENSE']['bytes']=True
    for name in manifest['files']:
        if mode!='bool_size':manifest['files'][name]={'sha256':hashlib.sha256(files[name]).hexdigest(),'bytes':len(files[name])}
    files['manifest.json']=json.dumps(manifest).encode();path=tmp_path/'archive.tar.gz';write_archive(files,path)
    with patch('lib.agent_core.native_artifact._self_check') as execute:
        with pytest.raises(NativeConfigurationError):
            inspect_archive(path,application_version='other' if mode=='version' else 'fixture',binary_bytes=4 if mode=='binary_limit' else None,seal_image=True)
    execute.assert_not_called()


def test_extended_metadata_bomb_is_refused_before_materialization(tmp_path):
    path=tmp_path/'metadata.tar.gz'
    with tarfile.open(path,'w:gz',format=tarfile.USTAR_FORMAT) as archive:
        entry=tarfile.TarInfo('pax');entry.type=tarfile.XHDTYPE;entry.size=1048577
        archive.addfile(entry,io.BytesIO(b'x'*entry.size))
    with pytest.raises(NativeConfigurationError,match='metadata'):
        inspect_archive(path)


def test_cancelled_archive_acquisition_joins_then_close_reclaims_image():
    async def run():
        entered=threading.Event();release=threading.Event();image=open('/dev/null','rb')
        def acquire(*args):entered.set();release.wait(3);return image
        client=NativeProcessClient.from_archive(archive=Path('/missing'),sha256='0'*64,application_version='fixture',principal=principal(),budget=budget())
        configuration=dict(version='tofu.native-agent-stdio/v1',type='configure',owner_user_id=7,task_id='task',tools={})
        with patch('lib.agent_core.native_artifact.seal_archive',side_effect=acquire),patch('asyncio.create_subprocess_exec') as spawn:
            task=asyncio.create_task(client.run(configuration,callbacks={},working_directory=Path('/tmp')))
            assert await asyncio.to_thread(entered.wait,2)
            task.cancel();await asyncio.sleep(.01)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):await task
            await client.close()
            assert image.closed and client.active_runs==0
            spawn.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('mode',['output','timeout'])
def test_self_check_bounds_output_and_joins_failed_child(tmp_path,mode):
    from lib.agent_core.native_artifact import _self_check
    import subprocess
    real_popen=subprocess.Popen
    processes=[]
    def start(*args,**kwargs):
        process=real_popen(['/usr/bin/yes'] if mode=='output' else ['/usr/bin/sleep','3'],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        processes.append(process)
        return process
    with open('/dev/null','rb') as image,patch('lib.agent_core.native_artifact.subprocess.Popen',side_effect=start):
        with pytest.raises(NativeConfigurationError):_self_check(image,'check-session',tmp_path,.03,1024)
    assert len(processes)==1 and processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_sparse_header_is_refused_before_sparse_table_processing(tmp_path):
    path=tmp_path/'sparse.tar.gz'
    with tarfile.open(path,'w:gz',format=tarfile.GNU_FORMAT) as archive:
        entry=tarfile.TarInfo('sparse');entry.type=tarfile.GNUTYPE_SPARSE;entry.size=0
        archive.addfile(entry,io.BytesIO())
    with pytest.raises(NativeConfigurationError,match='sparse'):
        inspect_archive(path)


def test_high_compression_refuses_before_unbounded_decoded_allocation():
    import gzip
    from lib.agent_core.native_artifact import _BoundedGzipReader
    compressed=gzip.compress(b'\0'*(32<<20))
    with patch.dict('lib.agent_core.native_artifact.SPEC',maximum_unpacked_bytes=1024,maximum_metadata_bytes=512,maximum_members=2):
        reader=_BoundedGzipReader(io.BytesIO(compressed))
        with pytest.raises(NativeConfigurationError,match='decoded archive byte bound'):reader.read(65536)
        assert reader.decoded==reader.maximum+1 and reader.decoded<65536
        # Retrying a rejected inflater must not turn max_length=0 into an
        # unlimited zlib expansion.
        for _ in range(2):
            with pytest.raises(NativeConfigurationError,match='decoded archive byte bound'):reader.read(65536)
        assert reader.decoded==reader.maximum+1


@pytest.mark.parametrize('corruption',['crc','size','truncate'])
def test_gzip_trailer_refuses_before_any_executable_check(tmp_path,corruption):
    source=tmp_path/'archive.tar.gz';write_archive(members(),source)
    raw=bytearray(source.read_bytes())
    if corruption=='truncate':del raw[-4:]
    else:raw[-8 if corruption=='crc' else -4]^=1
    source.write_bytes(raw)
    with patch('lib.agent_core.native_artifact._self_check') as check:
        with pytest.raises(NativeConfigurationError,match='compressed stream'):
            inspect_archive(source,archive_sha256=hashlib.sha256(raw).hexdigest(),seal_image=True,verify_run=True)
        check.assert_not_called()


def test_bounded_reader_preserves_full_high_ratio_content():
    import gzip
    from lib.agent_core.native_artifact import _BoundedGzipReader
    body=(b'fixture\0'*200000)
    reader=_BoundedGzipReader(io.BytesIO(gzip.compress(body)))
    chunks=[]
    while value:=reader.read(65536):
        assert len(value)<=65536;chunks.append(value)
    assert b''.join(chunks)==body
    reader.finish()
