"""Quart owns lazy native images and joins client scopes before dependencies close."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

import pytest
from lib.app.app_factory import create_base_app
from lib.agent_core.native_image import NativeArchiveImage
from lib.agent_core.native_process import NativeConfigurationError
from lib.tasks_pkg.native_host import NativeHost,register_native_host
from tests.agent_core.test_native_process import principal,budget

pytestmark=pytest.mark.unit


def image():
    return NativeArchiveImage(archive=Path('/missing'),sha256='0'*64,
        application_version='fixture',budget=budget())


def test_registration_is_lazy_idempotent_and_application_owned():
    async def run():
        app=create_base_app(__name__);release=image()
        with patch('lib.agent_core.native_artifact.seal_archive') as seal:
            host=register_native_host(app,image=release)
            assert register_native_host(app,image=release) is host
            with pytest.raises(NativeConfigurationError):register_native_host(app,image=image())
            with pytest.raises(NativeConfigurationError):NativeHost(release)
            async with app.test_app():
                async with host.client(principal(),budget=budget()) as client:
                    assert client.principal==principal()
                    assert client.active_runs==0
                assert client._closed
            assert host._closed and release._closed
            assert host._clients=={}
            seal.assert_not_called()
            with pytest.raises(NativeConfigurationError):
                async with host.client(principal(),budget=budget()):pass
    asyncio.run(run())


def test_failed_quart_startup_closes_native_owner_without_archive_io():
    async def run():
        from lib.app.app_lifecycle import add_startup_handler
        app=create_base_app(__name__);release=image();host=register_native_host(app,image=release)
        def fail():raise RuntimeError('injected startup failure')
        add_startup_handler(app,fail,name='fixture.failed-startup')
        with patch('lib.agent_core.native_artifact.seal_archive') as seal:
            with pytest.raises(RuntimeError,match='injected startup failure'):await app.startup()
            assert host._closed and release._closed
            await app.shutdown()
            seal.assert_not_called()
    asyncio.run(run())


def test_host_shutdown_joins_all_client_closes_before_image_close():
    async def run():
        release=image();host=NativeHost(release)
        entered,finished=asyncio.Event(),asyncio.Event();order=[]
        class Client:
            async def close(self):
                entered.set();await finished.wait();order.append('client')
        with patch('lib.tasks_pkg.native_host.NativeProcessClient.from_shared_archive',return_value=Client()),\
                patch.object(release,'close',new=AsyncMock(side_effect=lambda:order.append('image'))):
            async with host.client(principal(),budget=budget()):
                with pytest.raises(NativeConfigurationError):await host.close()
                closing=asyncio.create_task(host.close())
                await entered.wait();closing.cancel();await asyncio.sleep(0)
                assert not closing.done() and order==[]
                finished.set();await closing
                assert order==['client','image']
            assert host._clients=={}
    asyncio.run(run())


def test_close_failure_attempts_every_owner_and_remains_visible():
    async def run():
        release=image();host=NativeHost(release);attempts=[]
        class Client:
            async def close(self):attempts.append('client');raise OSError('join failed')
        host._clients[Client()]=None
        with patch.object(release,'close',new=AsyncMock(side_effect=lambda:attempts.append('image'))):
            with pytest.raises(ExceptionGroup):await host.close()
            assert attempts==['client','image']
            with pytest.raises(ExceptionGroup):await host.close()
            assert attempts==['client','image']
    asyncio.run(run())


def test_production_shutdown_refuses_dependency_teardown_if_native_join_fails():
    async def run():
        from lib.server.server_shutdown import shutdown_production_runtime
        failure=OSError('native join failed');host=SimpleNamespace(close=AsyncMock(side_effect=failure))
        app=SimpleNamespace(extensions={'tofu_native_host':host})
        with patch('lib.scheduler.timer.shutdown_timer_watchers') as stop:
            with pytest.raises(OSError) as caught:await shutdown_production_runtime(None,app=app)
            assert caught.value is failure
            stop.assert_not_called()
    asyncio.run(run())


def test_scope_preserves_primary_failure_and_releases_host_reference():
    async def run():
        host=NativeHost(image());failure=ValueError('execution failed')
        class Client:
            close=AsyncMock(side_effect=[OSError('close failed'),None])
        client=Client()
        with patch('lib.tasks_pkg.native_host.NativeProcessClient.from_shared_archive',return_value=client):
            with pytest.raises(ValueError) as caught:
                async with host.client(principal(),budget=budget()):raise failure
            assert caught.value is failure
            assert 'native client cleanup failed: OSError' in failure.__notes__
            assert host._clients=={client:None}
            with pytest.raises(NativeConfigurationError):
                async with host.client(principal(),budget=budget()):pass
        await host.close()
        assert not host._clients
    asyncio.run(run())


def test_reuse_configuration_is_lazy_thread_safe_and_closed_owner_refuses():
    import concurrent.futures
    release=image();host=NativeHost(release)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _:host.enable_worker_reuse({'root_maximum':3}),range(20)))
    assert host._worker_pool.idle_workers==3 and not host._worker_pool.workers
    asyncio.run(host.close())
    with pytest.raises(NativeConfigurationError):host.enable_worker_reuse({'root_maximum':3})


def test_queued_preparation_is_withdrawn_on_shutdown_before_archive_io():
    async def run():
        host=NativeHost(image())
        with patch('lib.agent_core.native_artifact.seal_archive') as seal:
            host.prepare_image(asyncio.get_running_loop())
            await host.close()
            assert host._closed and host.image._closed
            seal.assert_not_called()
    asyncio.run(run())


def test_preparation_from_worker_is_joined_and_immutable(tmp_path):
    import threading
    path=tmp_path/'prepared';path.write_bytes(b'fixture');opened=open(path,'rb')
    entered=threading.Event();release=threading.Event()
    def seal(*args):entered.set();assert release.wait(5);return opened
    async def run():
        host=NativeHost(image());loop=asyncio.get_running_loop()
        with patch('lib.agent_core.native_artifact.seal_archive',side_effect=seal) as load:
            await asyncio.to_thread(host.prepare_image,loop)
            assert await asyncio.to_thread(entered.wait,3)
            assert not host._clients
            closing=asyncio.create_task(host.close());await asyncio.sleep(0)
            assert not closing.done()
            release.set();await closing
            assert opened.closed and load.call_count==1
    try:asyncio.run(run())
    finally:release.set()
