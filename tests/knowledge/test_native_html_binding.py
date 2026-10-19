"""Actual sealed HTML library parity, error propagation and joined unloading."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import threading
import pytest

from lib.knowledge.native_html import NativeHtmlProjector, NativeHtmlProjectionError

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def library():
    value = os.environ.get('TOFU_NATIVE_HTML_TEST_LIBRARY')
    if not value:
        pytest.skip('explicit built native HTML library required')
    path = Path(value).resolve()
    return dict(library=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                binary_bytes=path.stat().st_size)


def test_lazy_service_first_use_and_repeated_retirement_with_real_code(library):
    from lib.knowledge.native_html_service import LazyHtmlProjector, HtmlProjectionClosed
    before = set(os.listdir('/proc/self/fd'))
    acquired = []
    def factory():
        acquired.append(True)
        return NativeHtmlProjector.from_library(**library)
    with LazyHtmlProjector(factory): pass
    assert not acquired
    for _ in range(20):
        with LazyHtmlProjector(factory) as service:
            assert service.profile is None
            assert service.project('<h1>actual &amp; sealed</h1>') == '# actual & sealed'
            assert service.profile['native_sources']
        with pytest.raises(HtmlProjectionClosed): service.project('late')
    assert len(acquired) == 20
    assert set(os.listdir('/proc/self/fd')) == before


def test_installed_html_admission_does_not_load_task_or_archive_machinery(library):
    script = '''
import json, sys
from pathlib import Path
from lib.knowledge.native_html import NativeHtmlProjector
options=json.loads(sys.argv[1]); options['library']=Path(options['library'])
with NativeHtmlProjector.from_library(**options) as projector:
    assert projector.project('<p>sealed</p>') == 'sealed'
assert 'lib.agent_core.native_process' not in sys.modules
assert 'lib.agent_core.native_artifact' not in sys.modules
assert 'ctypes' not in sys.modules
assert '_ctypes' not in sys.modules
assert 'cffi' not in sys.modules
assert 'pycparser' not in sys.modules
'''
    command = shlex.split(os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    result = subprocess.run([*command,'-c',script,json.dumps(library,default=str)],
        cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0,result.stdout+result.stderr


def test_actual_library_matches_all_goldens_and_compiled_sources(library):
    fixtures = json.loads((ROOT / 'packages/tofu-runtime/tests/fixtures/python_html_projection.json').read_text())
    with NativeHtmlProjector.from_library(**library) as native:
        for name, digest in native.profile['native_sources'].items():
            assert hashlib.sha256((ROOT / 'packages/tofu-runtime/src' / name).read_bytes()).hexdigest() == digest
        for case in fixtures['cases']:
            if 'error' in case:
                with pytest.raises(NativeHtmlProjectionError, match='NUMERIC_REFERENCE'):
                    native.project(case['source'])
            else:
                assert native.project(case['source']) == case['expected']


def test_per_call_failures_release_leases_and_never_fallback(library, monkeypatch):
    with NativeHtmlProjector.from_library(**library) as native:
        assert native.project('', projection_budget=0) == ''
        assert native.project('<p>a\0b</p>') == 'a\0b'
        with pytest.raises(NativeHtmlProjectionError, match='PROJECTION_BUDGET'):
            native.project('<h1>visible</h1>', projection_budget=1)
        with pytest.raises(UnicodeEncodeError):
            native.project('\ud800')
        for budget in (-1, True, 2**100, 1.5):
            with pytest.raises(ValueError):
                native.project('', projection_budget=budget)
        with pytest.raises(TypeError):
            native.project(b'bytes')
        failure = KeyboardInterrupt('injected sink interruption')
        with monkeypatch.context() as scoped:
            def fail(*_): raise failure
            scoped.setattr(native, '_consume', fail)
            with pytest.raises(KeyboardInterrupt) as caught:
                native.project('valid')
            assert caught.value is failure
        assert native.project('<p>still usable</p>') == 'still usable'


def test_concurrent_calls_close_join_and_no_new_admission(library, monkeypatch):
    native = NativeHtmlProjector.from_library(**library)
    before_fd = native._image.fileno()
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(native.project, [f'<p>{i}</p>' for i in range(64)])) == [str(i) for i in range(64)]
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    consume = native._consume
    results, errors = [], []
    def hold(*args):
        entered.set()
        if not release.wait(5): raise TimeoutError('test sink not released')
        return consume(*args)
    monkeypatch.setattr(native, '_consume', hold)
    def work():
        try: results.append(native.project('<h1>held</h1>'))
        except BaseException as error: errors.append(error)
    def close():
        try: native.close(); closed.set()
        except BaseException as error: errors.append(error)
    worker = threading.Thread(target=work)
    closer = threading.Thread(target=close)
    try:
        worker.start()
        assert entered.wait(3)
        closer.start()
        with native._condition:
            assert native._condition.wait_for(lambda: native._state == 'closing', 3)
        assert not closed.is_set()
        os.fstat(before_fd)
        with pytest.raises(NativeHtmlProjectionError, match='closed'):
            native.project('late')
    finally:
        release.set()
        worker.join(5)
        if closer.ident is not None: closer.join(5)
        native.close()
    assert not worker.is_alive() and not closer.is_alive()
    assert not errors and results == ['# held'] and closed.is_set()
    with pytest.raises(OSError): os.fstat(before_fd)


def test_sealed_image_survives_path_replacement_and_repeated_close_releases_descriptors(library, tmp_path):
    from lib.agent_core.native_process import NativeConfigurationError
    path = tmp_path / 'library.so'
    shutil.copyfile(library['library'], path)
    options = dict(library, library=path)
    before = set(os.listdir('/proc/self/fd'))
    for _ in range(4):
        with NativeHtmlProjector.from_library(**options) as native:
            assert native.project('<p>valid</p>') == 'valid'
    native = NativeHtmlProjector.from_library(**options)
    try:
        path.write_bytes(b'replaced')
        assert native.project('<p>sealed</p>') == 'sealed'
        with pytest.raises(NativeConfigurationError): NativeHtmlProjector.from_library(**options)
    finally: native.close()
    assert set(os.listdir('/proc/self/fd')) == before


def test_loader_and_profile_failure_close_admitted_descriptor(library, monkeypatch):
    from lib.knowledge import native_html
    before = set(os.listdir('/proc/self/fd'))
    with monkeypatch.context() as scoped:
        def fail(*args, **kwargs): raise OSError('injected loader failure')
        scoped.setattr(native_html, '_load_library', fail)
        with pytest.raises(OSError, match='injected loader'):
            NativeHtmlProjector.from_library(**library)
    with monkeypatch.context() as scoped:
        scoped.setattr(native_html, 'PROFILE', dict(native_html.PROFILE, owner_sha256='0'*64))
        with pytest.raises(NativeHtmlProjectionError, match='profile differs'):
            NativeHtmlProjector.from_library(**library)
    assert set(os.listdir('/proc/self/fd')) == before


def test_unload_failure_remains_visible_and_keeps_the_owned_image(library, monkeypatch):
    from lib.knowledge import native_html
    native = NativeHtmlProjector.from_library(**library)
    descriptor = native._image.fileno()
    original = native_html._close_library
    failure = OSError('injected unload failure')
    try:
        with monkeypatch.context() as scoped:
            def fail(_): raise failure
            scoped.setattr(native_html, '_close_library', fail)
            with pytest.raises(OSError) as caught: native.close()
            assert caught.value is failure
            assert native._library is not None and not native._image.closed
            os.fstat(descriptor)
            with pytest.raises(NativeHtmlProjectionError, match='resources retained'): native.close()
            with pytest.raises(NativeHtmlProjectionError, match='closed'): native.project('late')
    finally:
        # The test owns the injected fault and its explicitly retained resources.
        original(native._library)
        native._library = None
        native._image.close()
