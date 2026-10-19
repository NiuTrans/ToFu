"""Native executable admission rejects non-files without trapping joined close."""
import hashlib
import multiprocessing
import os
from pathlib import Path
from unittest.mock import patch
import pytest
from lib.agent_core.native_process import NativeConfigurationError,_seal

pytestmark=pytest.mark.unit


def _try_source(path,ready,result):
    ready.send(True);ready.close()
    try:
        with _seal(Path(path),'0'*64,1024):pass
    except NativeConfigurationError as error:result.send(str(error))
    finally:result.close()


def test_fifo_without_writer_is_refused_without_blocking_acquisition(tmp_path):
    path=tmp_path/'source';os.mkfifo(path)
    context=multiprocessing.get_context('fork')
    ready,started=context.Pipe(duplex=False);result,send=context.Pipe(duplex=False)
    worker=context.Process(target=_try_source,args=(str(path),started,send))
    worker.start();started.close();send.close()
    try:
        assert ready.poll(5) and ready.recv() is True
        assert result.poll(2),'non-file source blocked joined acquisition'
        assert 'regular file' in result.recv()
        worker.join(5);assert worker.exitcode==0
    finally:
        if worker.is_alive():worker.kill()
        worker.join();worker.close();ready.close();result.close()


def test_oversize_and_failed_identity_release_both_descriptors(tmp_path):
    path=tmp_path/'binary';raw=b'\x7fELF'+b'x'*100;path.write_bytes(raw)
    before=set(os.listdir('/proc/self/fd'))
    for limit,digest,reason in [(10,hashlib.sha256(raw).hexdigest(),'byte budget'),(1024,'0'*64,'checksum')]:
        with pytest.raises(NativeConfigurationError,match=reason):_seal(path,digest,limit)
        assert set(os.listdir('/proc/self/fd'))==before


def test_opened_file_is_the_identity_source_after_path_replacement(tmp_path):
    path=tmp_path/'binary';original=b'\x7fELForiginal';path.write_bytes(original)
    replacement=tmp_path/'replacement';replacement.write_bytes(b'\x7fELFdifferent')
    real_fstat=os.fstat
    def replaced(fd):
        info=real_fstat(fd)
        if replacement.exists():os.replace(replacement,path)
        return info
    with patch('lib.agent_core.native_process.os.fstat',side_effect=replaced):
        with _seal(path,hashlib.sha256(original).hexdigest(),1024) as image:
            assert os.pread(image.fileno(),1024,0)==original
            with pytest.raises(PermissionError):os.write(image.fileno(),b'changed')
