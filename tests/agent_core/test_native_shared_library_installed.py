"""Real token and Flow ABIs borrow one admitted release member through the host."""
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest

from lib.agent_core import native_artifact
from lib.agent_core.native_image import NativeArchiveImage
from lib.agent_core.native_process import Budget
from lib.tasks_pkg.native_host import NativeHost
from tests.agent_core.test_native_process import principal

pytestmark = pytest.mark.api


@pytest.mark.parametrize('first', ['token', 'flow'])
def test_real_host_shares_library_after_archive_removal_and_closes_both_abis(tmp_path, first):
    source = os.environ.get('TOFU_NATIVE_FLOW_TEST_ARCHIVE')
    if not source:
        pytest.skip('explicit combined native archive required')
    source = Path(source)
    manifest, _ = native_artifact.inspect_archive(source)
    pin = hashlib.sha256(source.read_bytes()).hexdigest()
    before = set(os.listdir('/proc/self/fd'))
    for repeat in range(3):
        archive = tmp_path / f'combined-{repeat}.tar.gz'
        shutil.copyfile(source, archive)
        image = NativeArchiveImage(archive=archive, sha256=pin,
            application_version=manifest['build']['application_version'],
            budget=Budget(1, 64 << 20, 65536, 65536, 5))
        host = NativeHost(image, token_count=True)
        graph = None
        try:
            with patch.object(native_artifact, 'inspect_archive', wraps=native_artifact.inspect_archive) as inspect:
                if first == 'token':
                    counter = host.token_counter()
                    archive.unlink()
                    flow = host.flow_scheduler()
                else:
                    flow = host.flow_scheduler()
                    archive.unlink()
                    counter = host.token_counter()
                assert inspect.call_count == 1
            assert os.fstat(counter._image.fileno()).st_ino == os.fstat(flow._fd).st_ino
            assert counter._library._handle == flow._library._handle
            assert counter.count_batch(['hello world', ''], encoding='cl100k_base') == [2, 0]
            assert counter.count_batch(['hello world', ''], encoding='o200k_base') == [2, 0]
            graph = flow.open_graph(principal=principal(), task_id='shared-library',
                nodes={'start': {'id': 'start', 'type': 'role'}}, edges=[])
            assert graph.find_start() == 'start'
            if first == 'token':
                counter.close()
                assert graph.find_start() == 'start'
                graph.close()
            else:
                graph.close()
                flow.close()
                assert counter.count_batch(['still available'], encoding='cl100k_base') == [2]
        finally:
            if graph is not None:
                graph.close()
            asyncio.run(host.close())
        assert not image._libraries and host._closed
        assert counter._image.closed and flow._fd is None
    assert set(os.listdir('/proc/self/fd')) == before


def test_shared_library_abi_bind_failure_releases_only_its_loan(tmp_path, monkeypatch):
    from lib.agent_core.native_token_count import NativeTokenCounter
    from lib.orchestration.native_flow import NativeFlowLibrary
    source = os.environ.get('TOFU_NATIVE_FLOW_TEST_ARCHIVE')
    if not source:
        pytest.skip('explicit combined native archive required')
    source = Path(source)
    manifest, _ = native_artifact.inspect_archive(source)
    image = NativeArchiveImage(archive=source, sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        application_version=manifest['build']['application_version'],
        budget=Budget(1, 64 << 20, 65536, 65536, 5))
    before = set(os.listdir('/proc/self/fd'))
    counter = NativeTokenCounter.from_shared_archive(image=image)
    try:
        acquired = set(os.listdir('/proc/self/fd'))
        with monkeypatch.context() as scoped:
            def fail(_): raise RuntimeError('injected shared ABI fault')
            scoped.setattr(NativeFlowLibrary, '_bind', fail)
            with pytest.raises(RuntimeError, match='shared ABI fault'):
                NativeFlowLibrary.from_shared_archive(image=image)
        assert set(os.listdir('/proc/self/fd')) == acquired
        assert counter.count_batch(['hello world'], encoding='cl100k_base') == [2]
        flow = NativeFlowLibrary.from_shared_archive(image=image)
        flow.close()
    finally:
        counter.close()
        asyncio.run(image.close())
    assert set(os.listdir('/proc/self/fd')) == before
