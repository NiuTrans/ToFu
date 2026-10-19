"""Shared executable bytes retain independent descriptor and close ownership."""
import asyncio
import os
from pathlib import Path
import threading
from unittest.mock import patch

import pytest
from lib.agent_core.native_image import NativeArchiveImage, NativeExecutableImage
from lib.agent_core.native_process import NativeConfigurationError
from tests.agent_core.test_native_process import budget

pytestmark = pytest.mark.unit


@pytest.fixture(params=['archive', 'executable'])
def source(request):
    if request.param == 'archive':
        return (lambda: NativeArchiveImage(archive=Path('/missing'), sha256='0'*64,
                    application_version='fixture', budget=budget()),
                'lib.agent_core.native_artifact.seal_archive')
    return (lambda: NativeExecutableImage(binary=Path('/missing'), sha256='0'*64,
                application_version='fixture', budget=budget()),
            'lib.agent_core.native_artifact.seal_executable')


def test_concurrent_borrowers_share_one_load_and_close_independently(tmp_path, source):
    path=tmp_path/'image';path.write_bytes(b'executable')
    image=open(path,'rb')
    async def run():
        shared=source[0]()
        with patch(source[1],return_value=image) as seal:
            copies=await asyncio.gather(*(shared.duplicate(100) for _ in range(8)))
            assert seal.call_count==1
            assert len({os.fstat(copy.fileno()).st_ino for copy in copies})==1
            assert len({copy.fileno() for copy in copies})==8
            assert all(not os.get_inheritable(copy.fileno()) for copy in copies)
            copies[0].close()
            await shared.close();await shared.close()
            assert image.closed
            assert all(not copy.closed for copy in copies[1:])
            assert os.pread(copies[1].fileno(),10,0)==b'executable'
            with pytest.raises(NativeConfigurationError):await shared.duplicate(100)
            for copy in copies:copy.close()
    asyncio.run(run())


def test_cancelled_borrow_and_close_join_started_acquisition(tmp_path, source):
    path=tmp_path/'image';path.write_bytes(b'executable')
    image=open(path,'rb');entered=threading.Event();release=threading.Event()
    def seal(*args):entered.set();assert release.wait(3);return image
    async def run():
        shared=source[0]()
        with patch(source[1],side_effect=seal):
            borrow=asyncio.create_task(shared.duplicate(100))
            assert await asyncio.to_thread(entered.wait,2)
            borrow.cancel()
            with pytest.raises(asyncio.CancelledError):await borrow
            closing=asyncio.create_task(shared.close());await asyncio.sleep(0)
            closing.cancel();await asyncio.sleep(0)
            assert not closing.done()
            release.set();await closing
            assert image.closed
    asyncio.run(run())


def test_failed_admission_is_not_retried_or_replaced(source):
    async def run():
        shared=source[0]()
        with patch(source[1],side_effect=NativeConfigurationError('bad pin')) as seal:
            for _ in range(2):
                with pytest.raises(NativeConfigurationError):await shared.duplicate(100)
            assert seal.call_count==1
            await shared.close()
    asyncio.run(run())


def test_client_binary_limit_refuses_without_closing_shared_image(tmp_path, source):
    path=tmp_path/'image';path.write_bytes(b'executable');image=open(path,'rb')
    async def run():
        shared=source[0]()
        with patch(source[1],return_value=image):
            with pytest.raises(NativeConfigurationError):await shared.duplicate(1)
            assert not image.closed
            copy=await shared.duplicate(10);copy.close();await shared.close()
    asyncio.run(run())


def test_unused_close_is_lazy_and_cross_loop_reuse_refuses(source):
    shared=source[0]()
    with patch(source[1]) as seal:
        asyncio.run(shared.close());seal.assert_not_called()
    with pytest.raises(NativeConfigurationError):asyncio.run(shared.close())


def test_preparation_is_one_owned_load_and_close_joins_before_borrow(tmp_path, source):
    path=tmp_path/'prepared';path.write_bytes(b'executable');image=open(path,'rb')
    entered=threading.Event();release=threading.Event()
    def seal(*args):entered.set();assert release.wait(5);return image
    async def run():
        shared=source[0]()
        with patch(source[1],side_effect=seal) as load:
            for _ in range(8):shared.prepare()
            assert shared.admission_started
            assert await asyncio.to_thread(entered.wait,3)
            closing=asyncio.create_task(shared.close());await asyncio.sleep(0)
            assert not closing.done()
            release.set();await closing
            assert image.closed and load.call_count==1
            with pytest.raises(NativeConfigurationError):shared.prepare()
    try:asyncio.run(run())
    finally:release.set()


def test_prepared_failure_remains_exact_for_each_borrower(source):
    failure=NativeConfigurationError('prepared bad pin')
    async def run():
        shared=source[0]()
        with patch(source[1],side_effect=failure) as load:
            shared.prepare()
            for _ in range(2):
                with pytest.raises(NativeConfigurationError) as caught:await shared.duplicate(100)
                assert caught.value is failure
            assert load.call_count==1
            await shared.close()
    asyncio.run(run())
