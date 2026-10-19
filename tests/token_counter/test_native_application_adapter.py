"""Native counts preserve cache multiplicity, receipts and app ownership."""
import asyncio
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import pytest
import tiktoken
from lib.app.app_factory import create_base_app
from lib.tasks_pkg.native_host import register_native_host
from lib.agent_core.native_process import NativeConfigurationError
from lib.token_counter import tiktoken_counter as module
from lib.token_counter.native import NativeTextEncoder
from tests.tasks_pkg.test_native_host import image

pytestmark = pytest.mark.unit


class Counter:
    def __init__(self):
        self.calls = []
        self.closed = False
    def count_batch(self, texts, *, encoding):
        self.calls.append((texts, encoding))
        oracle = tiktoken.get_encoding(encoding)
        return [len(oracle.encode(text, disallowed_special=())) for text in texts]
    def close(self):
        self.closed = True


@pytest.mark.parametrize('model', ['gpt-4', 'gpt-4o'])
def test_native_application_matches_full_request_cache_and_identity_evidence(model):
    async def run():
        module._reset_text_count_cache_for_tests()
        text = 'hello 世界 <|endoftext|> ' * 100
        messages = [{'role':'user', 'content':'ask\ud800\ud83d\ude00'},
                    {'role':'tool', 'content':text}, {'role':'tool', 'content':text}]
        tools = [{'type':'function', 'function':{'name':'read', 'parameters':{'type':'object'}}}]
        expected_receipt = {}
        expected = module.TiktokenCounter().count(messages, model=model, tools=tools, measurement_out=expected_receipt)
        module._reset_text_count_cache_for_tests()
        app = create_base_app(__name__)
        host = register_native_host(app, image=image(), token_count=True)
        backend = Counter()
        with patch('lib.agent_core.native_token_count.NativeTokenCounter.from_shared_archive', return_value=backend) as create:
            async with app.test_app():
                assert host._token_counter is None
                async with app.app_context():
                    assert isinstance(module._get_encoder(module.encoding_for_model(model)), NativeTextEncoder)
                    receipt = {}
                    with ThreadPoolExecutor(max_workers=1) as worker:
                        actual = await asyncio.wrap_future(worker.submit(copy_context().run,
                            lambda: module.TiktokenCounter().count(messages, model=model, tools=tools, measurement_out=receipt)))
                    assert actual == expected and receipt == expected_receipt
                    assert backend.calls[0][0].count(text) == 1
                    assert module.TiktokenCounter().count(messages, model=model, tools=tools) == expected
                    assert text not in backend.calls[1][0]
                    assert module.count_text('a\ud800\ud83d\ude00b', model) == len(tiktoken.get_encoding(module.encoding_for_model(model)).encode('a\ud800\ud83d\ude00b', disallowed_special=()))
                create.assert_called_once()
            assert backend.closed and host._closed
        assert not isinstance(module._get_encoder(module.encoding_for_model(model)), NativeTextEncoder)
        module._reset_text_count_cache_for_tests()
    asyncio.run(run())


def test_host_selection_is_lazy_and_cannot_change_after_registration():
    async def run():
        app = create_base_app(__name__)
        release = image()
        with patch('lib.agent_core.native_token_count.NativeTokenCounter.from_shared_archive') as create:
            host = register_native_host(app, image=release, token_count=True)
            assert register_native_host(app, image=release, token_count=True) is host
            with pytest.raises(NativeConfigurationError, match='differs'):
                register_native_host(app, image=release, token_count=False)
            with patch('lib.tasks_pkg.native_host.asyncio.to_thread') as shutdown_worker:
                await host.close()
                shutdown_worker.assert_not_called()
            create.assert_not_called()
    asyncio.run(run())


def test_host_close_waits_for_token_initialization_and_keeps_loop_responsive():
    import threading
    async def run():
        app = create_base_app(__name__)
        host = register_native_host(app, image=image(), token_count=True)
        entered, release = threading.Event(), threading.Event()
        backend = Counter()
        def create(**kwargs):
            entered.set()
            assert release.wait(3)
            return backend
        with patch('lib.agent_core.native_token_count.NativeTokenCounter.from_shared_archive', side_effect=create):
            loading = asyncio.create_task(asyncio.to_thread(host.token_counter))
            assert await asyncio.to_thread(entered.wait, 3)
            closing = asyncio.create_task(host.close())
            await asyncio.sleep(.01)
            assert host._closed and not closing.done() and not backend.closed
            release.set()
            assert await loading is backend
            await closing
            assert backend.closed
    asyncio.run(run())
