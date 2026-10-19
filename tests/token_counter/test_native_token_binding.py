"""Pinned binding joins borrowed calls before handle release and code unload."""
import ctypes
import threading
from pathlib import Path
from unittest.mock import patch
import pytest
from lib.agent_core.native_token_count import NativeTokenCounter, NativeTokenCountError

pytestmark = pytest.mark.unit


class Function:
    def __init__(self, function):
        self.function = function
    def __call__(self, *args):
        return self.function(*args)


class Image:
    closed = False
    def fileno(self):
        return 91
    def close(self):
        self.closed = True


class Library:
    _handle = 12
    def __init__(self):
        self.events = []
        self.tofu_token_count_abi_version = Function(lambda: 1)
        self.tofu_token_count_open = Function(self.open)
        self.tofu_token_count_batch = Function(self.batch)
        self.tofu_token_count_close = Function(lambda handle: self.events.append('close') or 0)
    def open(self, encoding, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64))[0] = encoding + 1
        self.events.append('open')
        return 0
    def batch(self, handle, spans, length, output, capacity):
        for index in range(length):
            raw = ctypes.string_at(spans[index].data, spans[index].length)
            output[index] = len(raw)
        return 0


@pytest.fixture(params=["archive", "installed"])
def bound(request):
    image, library = Image(), Library()
    with patch('lib.agent_core.native_process._seal', return_value=image), \
         patch('lib.agent_core.native_token_count.seal_token_count_archive', return_value=image), \
         patch('lib.agent_core.native_token_count.ctypes.CDLL', return_value=library), \
         patch('lib.agent_core.native_token_count._ctypes.dlclose', side_effect=lambda _: library.events.append('unload')):
        counter = (NativeTokenCounter(archive='archive', sha256='pin', application_version='v', binary_bytes=1024)
            if request.param == 'archive' else NativeTokenCounter.from_library(
                library=Path('/installed'), sha256='0'*64, binary_bytes=1024))
        yield counter, image, library
        if counter._state == 'open':
            counter.close()


def test_lazy_handles_buffer_lengths_embedded_null_and_closed_refusal(bound):
    counter, image, library = bound
    assert library.events == []
    assert counter.count_batch(['世界', '', 'a\x00b'], encoding='cl100k_base') == [6, 0, 3]
    assert counter.count_batch([], encoding='cl100k_base') == []
    assert library.events == ['open']
    counter.close()
    counter.close()
    assert library.events == ['open', 'close', 'unload'] and image.closed
    with pytest.raises(NativeTokenCountError, match='closed'):
        counter.count_batch([], encoding='cl100k_base')


def test_close_joins_accepted_call_then_unloads_and_rejects_new_calls(bound):
    counter, image, library = bound
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = library.tofu_token_count_batch.function
    def block(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    library.tofu_token_count_batch.function = block
    result = []
    worker = threading.Thread(target=lambda: result.extend(counter.count_batch(['abc'], encoding='o200k_base')))
    closer = threading.Thread(target=lambda: (counter.close(), closed.set()))
    worker.start()
    assert entered.wait(3)
    closer.start()
    with counter._condition:
        assert counter._condition.wait_for(lambda: counter._state == 'closing', 3)
    assert not closed.is_set() and not image.closed and 'unload' not in library.events
    with pytest.raises(NativeTokenCountError, match='closed'):
        counter.count_batch([], encoding='o200k_base')
    release.set()
    worker.join(3); closer.join(3)
    assert not worker.is_alive() and not closer.is_alive()
    assert result == [3] and closed.is_set() and image.closed
    assert library.events == ['open', 'close', 'unload']


def test_input_and_call_errors_release_lease_without_fallback(bound):
    counter, _, library = bound
    for texts in ['text', [1], [None]]:
        with pytest.raises(TypeError):
            counter.count_batch(texts, encoding='cl100k_base')
    with pytest.raises(ValueError):
        counter.count_batch([], encoding='other')
    with pytest.raises(UnicodeEncodeError):
        counter.count_batch(['\ud800'], encoding='cl100k_base')
    library.tofu_token_count_batch.function = lambda *args: 5
    with pytest.raises(NativeTokenCountError, match='ENCODER'):
        counter.count_batch(['secret'], encoding='cl100k_base')
    assert counter._active == 0


def test_teardown_failure_retains_mapping_and_never_claims_close(bound):
    counter, image, library = bound
    counter.count_batch([], encoding='cl100k_base')
    library.tofu_token_count_close.function = lambda *args: 6
    with pytest.raises(NativeTokenCountError, match='PANIC'):
        counter.close()
    assert not image.closed and 'unload' not in library.events
    with pytest.raises(NativeTokenCountError, match='teardown failed'):
        counter.close()


def test_version_failure_unloads_before_descriptor_release():
    image, library = Image(), Library()
    library.tofu_token_count_abi_version.function = lambda: 99
    with patch('lib.agent_core.native_token_count.seal_token_count_archive', return_value=image), \
         patch('lib.agent_core.native_token_count.ctypes.CDLL', return_value=library), \
         patch('lib.agent_core.native_token_count._ctypes.dlclose') as unload:
        with pytest.raises(NativeTokenCountError, match='version'):
            NativeTokenCounter(archive='archive', sha256='pin', application_version='v', binary_bytes=1024)
        unload.assert_called_once_with(12)
        assert image.closed


def test_interrupted_close_wait_reopens_admission_before_any_native_close(bound):
    counter, image, library = bound
    counter._active = 1
    with patch.object(counter._condition, 'wait', side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            counter.close()
    assert counter._state == 'open' and not image.closed
    assert 'close' not in library.events and 'unload' not in library.events
    counter._active = 0
    assert counter.count_batch(['again'], encoding='cl100k_base') == [5]


@pytest.mark.parametrize('pin,limit', [('bad', 100), ('0'*64, True), ('0'*64, 3)])
def test_installed_library_refuses_invalid_identity_before_loading(pin, limit):
    with patch('lib.agent_core.native_token_count.ctypes.CDLL') as load:
        with pytest.raises(ValueError):
            NativeTokenCounter.from_library(library=Path('/missing'),sha256=pin,binary_bytes=limit)
        load.assert_not_called()


def test_installed_checksum_refusal_never_loads_code(tmp_path):
    source=tmp_path/'library';source.write_bytes(b'\x7fELFtampered')
    original=ctypes.CDLL
    mapped=[]
    def load(path,*args,**kwargs):
        if isinstance(path,str) and path.startswith('/proc/self/fd/'):
            mapped.append(path)
            raise AssertionError('unverified library loaded')
        return original(path,*args,**kwargs)
    with patch('lib.agent_core.native_token_count.ctypes.CDLL',side_effect=load):
        with pytest.raises(ValueError,match='checksum'):
            NativeTokenCounter.from_library(library=source,sha256='0'*64,binary_bytes=100)
    assert not mapped
