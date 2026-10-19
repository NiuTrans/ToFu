"""Application ownership for an admitted native image and active client scopes.

register_native_host uses the existing Quart shutdown registry. NativeHost.client
creates one principal-bound transient client over the shared image and joins its
close. Host shutdown rejects new scopes and joins clients before image release.
No client cache, executor, timer, task registry or model/backend selection.
"""
import asyncio
from contextlib import asynccontextmanager
import weakref
import sys
import threading

from lib.agent_core.native_image import NativeArchiveImage, NativeExecutableImage
from lib.agent_core.native_process import Budget, NativeProcessClient, NativeConfigurationError, _joined


async def _close_on_worker(close):
    """Join one synchronous close without Task-level control-exception escape."""
    async def settle():
        try:
            await asyncio.to_thread(close)
        except BaseException as failure:
            return failure
        return None

    operation = settle()
    try:
        cleanup = asyncio.create_task(operation)
    except BaseException:
        operation.close()
        raise
    failure = await _joined(cleanup)
    if failure is not None:
        raise failure


class NativeHost:
    __slots__=('image','_loop','_clients','_closed','_closing','_worker_pool','_reuse_lock','_token_lock','_token_counter','token_count_enabled','_flow_lock','_flow_scheduler','__weakref__')

    def __init__(self,image,*,token_count=False):
        if not isinstance(image,(NativeArchiveImage,NativeExecutableImage)):
            raise NativeConfigurationError('native host requires an explicit release image')
        if type(token_count) is not bool:
            raise NativeConfigurationError('native token-count selection must be boolean')
        if token_count and isinstance(image,NativeExecutableImage) and image.token_library is None:
            raise NativeConfigurationError('native token count requires an archive or pinned installed library')
        previous=getattr(image,'_application_owner',None)
        if previous is not None and previous() is not None:
            raise NativeConfigurationError('native image already has an application owner')
        self.image=image
        self._loop=None;self._clients={};self._closed=False;self._closing=None;self._worker_pool=None;self._reuse_lock=threading.Lock()
        self._token_lock=threading.Lock();self._token_counter=None;self.token_count_enabled=token_count
        self._flow_lock=threading.Lock();self._flow_scheduler=None
        image._application_owner=weakref.ref(self)

    def token_counter(self):
        # Called on the existing synchronous preparation lane. Initialization
        # and retirement share this lock; the serving loop never acquires it.
        with self._token_lock:
            if self._closed or not self.token_count_enabled:
                raise NativeConfigurationError('native token counter unavailable')
            if self._token_counter is None:
                from lib.agent_core.native_token_count import NativeTokenCounter
                if isinstance(self.image,NativeExecutableImage):
                    self._token_counter=NativeTokenCounter.from_library(library=self.image.token_library,
                        sha256=self.image.token_library_sha256,binary_bytes=self.image.budget.binary_bytes)
                else:
                    self._token_counter=NativeTokenCounter.from_shared_archive(image=self.image)
            return self._token_counter

    def _close_token_counter(self):
        with self._token_lock:
            if self._token_counter is not None:
                self._token_counter.close()

    def flow_scheduler(self):
        """Lazy release admission on the existing synchronous task binding lane."""
        with self._flow_lock:
            if self._closed or not isinstance(self.image,NativeArchiveImage):
                raise NativeConfigurationError('native Flow scheduler requires an open archive image')
            if self._flow_scheduler is None:
                from lib.orchestration.native_flow import NativeFlowLibrary
                self._flow_scheduler=NativeFlowLibrary.from_shared_archive(image=self.image)
            return self._flow_scheduler

    def _close_flow_scheduler(self):
        with self._flow_lock:
            if self._flow_scheduler is not None:
                self._flow_scheduler.close(wait_for_graphs=True)

    def _bind_loop(self):
        loop=asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise NativeConfigurationError('native application host belongs to another event loop')
        self._loop=loop

    def prepare_image(self,loop):
        """Schedule one owned image admission from a task's preparation lane."""
        if (self._closed or not isinstance(loop,asyncio.AbstractEventLoop)
                or loop.is_closed() or not loop.is_running()
                or (self._loop is not None and self._loop is not loop)):
            raise NativeConfigurationError('native image preparation requires the live owning loop')
        if self.image.admission_started:return
        def prepare():
            # A shutdown racing a queued preparation withdraws it before I/O.
            if self._closed:return
            self._bind_loop()
            self.image.prepare()
        loop.call_soon_threadsafe(prepare)

    def enable_worker_reuse(self,launch_policy):
        # Preparation runs on the existing sync executor; this step owns no IO.
        # Actual process/image acquisition binds to the serving loop later.
        from collections.abc import Mapping
        if not isinstance(launch_policy,Mapping) or type(launch_policy.get('root_maximum')) is not int or launch_policy['root_maximum']<=0:
            raise NativeConfigurationError('native reuse requires issued idle capacity policy')
        with self._reuse_lock:
            if self._closed:raise NativeConfigurationError('native application host is closed')
            if self._worker_pool is None:
                from lib.agent_core.native_worker_pool import NativeWorkerPool
                self._worker_pool=NativeWorkerPool(image=self.image,idle_workers=launch_policy['root_maximum'])

    @asynccontextmanager
    async def client(self,principal,*,budget:Budget):
        self._bind_loop()
        if self._closed:raise NativeConfigurationError('native application host is closed')
        client=NativeProcessClient.from_shared_image(image=self.image,principal=principal,budget=budget)
        client._worker_pool=self._worker_pool
        client._token_count_host=self if self.token_count_enabled else None
        self._clients[client]=asyncio.current_task()
        try:
            yield client
        finally:
            primary=sys.exception()
            closed=False
            try:
                await client.close()
                closed=True
            except BaseException as error:
                self._closed=True  # Unknown cleanup must not accumulate more native owners.
                if primary is None:raise
                primary.add_note('native client cleanup failed: '+type(error).__name__)
            finally:
                if closed:self._clients.pop(client,None)
                else:self._clients[client]=None  # Preserve unresolved cleanup ownership.

    async def _close_client(self,client):
        try:
            await client.close()
        except BaseException as failure:
            # gather creates a Task per client. Control exceptions must reach
            # the aggregate owner as data so sibling cleanup can still finish.
            return failure
        self._clients.pop(client,None)
        return None

    async def _close(self):
        failures=[]
        if self._clients:
            cleanups=[]
            for client in tuple(self._clients):
                operation=self._close_client(client)
                try:
                    cleanups.append(asyncio.create_task(operation))
                except BaseException as failure:
                    operation.close()
                    failures.append(failure)
            if cleanups:
                results=await _joined(asyncio.gather(*cleanups,return_exceptions=True))
                failures.extend(result for result in results if isinstance(result,BaseException))
        if self._worker_pool is not None:
            try:await self._worker_pool.close()
            except BaseException as error:failures.append(error)
        if self.token_count_enabled and (self._token_lock.locked() or self._token_counter is not None):
            try:await _close_on_worker(self._close_token_counter)
            except BaseException as error:failures.append(error)
        if self._flow_lock.locked() or self._flow_scheduler is not None:
            try:await _close_on_worker(self._close_flow_scheduler)
            except BaseException as error:failures.append(error)
        try:await self.image.close()
        except BaseException as error:failures.append(error)
        if failures:raise BaseExceptionGroup('native application resources did not close cleanly',failures)

    async def close(self):
        self._bind_loop()
        if asyncio.current_task() in self._clients.values():
            raise NativeConfigurationError('native client scope cannot close its own host')
        with self._reuse_lock:self._closed=True
        if self._closing is None:
            operation = self._close()
            try:
                self._closing = asyncio.create_task(operation)
            except BaseException:
                operation.close()
                raise
        await _joined(self._closing)


def register_native_host(app,*,image,token_count=False):
    from lib.app.app_lifecycle import add_shutdown_handler,register_app_lifecycle
    previous=app.extensions.get('tofu_native_host')
    if previous is not None:
        if previous.image is not image or previous.token_count_enabled!=token_count:raise NativeConfigurationError('application native image differs')
        return previous
    register_app_lifecycle(app)
    if app.extensions['tofu_lifecycle']['status']!='created':
        raise NativeConfigurationError('native host must register before application startup')
    host=NativeHost(image,token_count=token_count)
    if not add_shutdown_handler(app,host.close,name='tofu.native-host'):
        raise NativeConfigurationError('native host shutdown owner already registered')
    app.extensions['tofu_native_host']=host
    return host
