"""External request cancellation must join only that request's children."""
import asyncio
import logging
from types import SimpleNamespace
import pytest
from lib.app_lifecycle import owned_asgi_http_connection_class
pytestmark=pytest.mark.unit


def test_parent_cancellation_joins_receiver_and_handler():
    async def scenario():
        started=[asyncio.Event(),asyncio.Event()];closed=[False,False];children=[]
        class Connection(owned_asgi_http_connection_class()):
            def _create_request_from_scope(self,send):return object()
            async def child(self,index):
                children.append(asyncio.current_task());started[index].set()
                try:await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0);closed[index]=True
            async def handle_messages(self,*args):await self.child(0)
            async def handle_request(self,*args):await self.child(1)
        connection=Connection(SimpleNamespace(logger=logging.getLogger(__name__)),{})
        parent=asyncio.create_task(connection(None,None))
        try:
            await asyncio.gather(*(event.wait() for event in started))
            parent.cancel()
            with pytest.raises(asyncio.CancelledError):await parent
            assert all(closed),'request parent returned before child cleanup'
            assert all(task.done() for task in children)
        finally:
            for task in children:task.cancel()
            await asyncio.gather(*children,return_exceptions=True)
    asyncio.run(scenario())


def test_second_child_creation_failure_rolls_back_first(monkeypatch):
    async def scenario():
        created=[];calls=0;original=asyncio.ensure_future
        def spawn(coroutine,*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:raise RuntimeError('injected second child creation failure')
            task=original(coroutine,*args,**kwargs);created.append(task);return task
        class Connection(owned_asgi_http_connection_class()):
            def _create_request_from_scope(self,send):return object()
            async def handle_messages(self,*args):await asyncio.Event().wait()
            async def handle_request(self,*args):await asyncio.Event().wait()
        monkeypatch.setattr(asyncio,'ensure_future',spawn)
        connection=Connection(SimpleNamespace(logger=logging.getLogger(__name__)),{})
        with pytest.raises(RuntimeError,match='second child creation'):
            await connection(None,None)
        assert len(created)==1 and created[0].done()
    asyncio.run(scenario())
