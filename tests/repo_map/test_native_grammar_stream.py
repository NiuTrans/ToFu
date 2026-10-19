"""Actual owned scans admit pinned grammar code and retire it with the worker."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def installed_scanner(tmp_path, monkeypatch):
    binary = os.environ.get('TOFU_NATIVE_STREAM_BINARY')
    directory = os.environ.get('TOFU_NATIVE_EXTRACTION_GRAMMARS')
    if not binary or not directory:
        pytest.skip('explicit current native binary and grammar fixture directory required')
    from lib.repo_map._native_stream import NativeStreamScanner, StreamLimits
    from tofu_repo_kernel._stream import StreamExecutable
    from tofu_repo_kernel._grammars import package_bundle
    source = tmp_path/'bundle-input';source.mkdir();entries=[]
    for path in sorted(Path(directory).glob('libtree_sitter_*.so')):
        shutil.copyfile(path, source/path.name)
        content = path.read_bytes()
        entries.append(dict(file=path.name, bytes=len(content), sha256=hashlib.sha256(content).hexdigest()))
    manifest = json.dumps(dict(format='tofu.repo-grammar-bundle/v1', registry_version='1.20.0', libraries=entries)).encode()
    pin = hashlib.sha256(manifest).hexdigest();(source/'grammars.json').write_bytes(manifest)
    (source/'NOTICES.txt').write_text('Local verification fixture from explicitly selected installed grammar libraries. Not a published distribution.\n')
    installed = tmp_path/'installed';descriptor = package_bundle(source, installed, pin)
    bundle = installed/'grammars'
    for name in list(os.environ):
        if name.startswith('GIT_'):monkeypatch.delenv(name)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull);monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    root = tmp_path/'project';root.mkdir();subprocess.run(['git','init','-q',str(root)], check=True)
    executable_pin = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
    images=[]
    def image():
        result = StreamExecutable(binary, executable_pin, installed_grammars=(bundle,pin,descriptor['bytes']))
        images.append(result);return result
    scanner = NativeStreamScanner(None, None, expected_sha256=None, limits=StreamLimits.from_launch_policy({'frame_maximum':8<<20}),
                                  strict=False, image_factory=image)
    return scanner, root, bundle, images


def test_installed_grammar_scan_matches_real_multilingual_owner(installed_scanner, monkeypatch):
    pytest.importorskip('tree_sitter_language_pack')
    from lib.repo_map import outline
    monkeypatch.syspath_prepend(str(ROOT/'benchmarks/repository_index'))
    from domain_fixtures import fixture
    scanner, root, _, images = installed_scanner
    for name, source in fixture('multilingual_extraction').items():(root/name).write_text(source)
    expected = asdict(outline.scan(str(root)))
    assert asdict(scanner(root)) == expected
    assert images and all(image._image.closed for image in images)


@pytest.mark.parametrize('failure', ['manifest', 'changed-library', 'missing-library'])
def test_selected_integrity_failure_never_publishes_partial_map(installed_scanner, monkeypatch, failure):
    from tests.repo_map.test_native_extraction_stream import observe_children, assert_joined
    scanner, root, bundle, images = installed_scanner
    (root/'a.py').write_text('def before_failure(): pass\n')
    (root/'b.rs').write_text('pub fn must_not_publish() {}\n')
    if failure=='manifest':(bundle/'grammars.json').write_text('{}')
    elif failure=='changed-library':(bundle/'libtree_sitter_rust.so').write_bytes(b'changed')
    else:(bundle/'libtree_sitter_rust.so').unlink()
    children=observe_children(monkeypatch)
    with pytest.raises((ValueError, RuntimeError)):
        scanner(root)
    assert_joined(children)
    assert all(image._image.closed for image in images)


def blocked_after_loaded(installed_scanner, monkeypatch):
    import lib.repo_map._native_stream as module
    scanner, root, bundle, images = installed_scanner
    (root/'a.rs').write_text('pub fn before() {}\n')
    blocked=root/'z.rs';os.mkfifo(blocked)
    entered=threading.Event();original=module._Channel.frame
    def frame(self, *args, **kwargs):
        value=original(self,*args,**kwargs)
        if value and value.startswith(b'{'):
            row=json.loads(value)
            if row.get('path_index')==0 and row.get('tags'):entered.set()
        return value
    monkeypatch.setattr(module._Channel,'frame',frame)
    return entered, blocked


def test_loaded_code_survives_replacement_but_next_worker_refuses(installed_scanner, monkeypatch):
    scanner, root, bundle, images = installed_scanner
    entered, blocked = blocked_after_loaded(installed_scanner,monkeypatch)
    results=[];errors=[];cancel=threading.Event()
    def run():
        try:results.append(scanner(root,cancellation=cancel,timeout=10))
        except BaseException as error:errors.append(error)
    thread=threading.Thread(target=run);thread.start()
    try:
        assert entered.wait(5)
        (bundle/'libtree_sitter_rust.so').write_bytes(b'replaced after first parse')
        deadline=time.monotonic()+2
        while True:
            try:descriptor=os.open(blocked,os.O_WRONLY|os.O_NONBLOCK);break
            except OSError:
                if time.monotonic()>=deadline:raise
                time.sleep(.01)
        try:os.write(descriptor,b'pub fn after() {}\n')
        finally:os.close(descriptor)
        thread.join(5);assert not thread.is_alive() and not errors
        assert {tag.name for tags in results[0].tags.values() for tag in tags} == {'before','after'}
        with pytest.raises(RuntimeError, match='grammar_admission'):scanner(root,timeout=5)
    finally:
        cancel.set();thread.join(5)
    assert not thread.is_alive() and all(image._image.closed for image in images)


def test_cancellation_after_grammar_load_joins_every_child(installed_scanner, monkeypatch):
    from tests.repo_map.test_native_extraction_stream import observe_children, assert_joined
    from lib.repo_map._native_stream import DiscoveryCancelled
    scanner, root, _, images = installed_scanner
    entered, _ = blocked_after_loaded(installed_scanner,monkeypatch)
    children=observe_children(monkeypatch);cancel=threading.Event();errors=[]
    def run():
        try:scanner(root,cancellation=cancel,timeout=10)
        except BaseException as error:errors.append(error)
    thread=threading.Thread(target=run);thread.start()
    try:
        assert entered.wait(5);cancel.set();thread.join(5)
        assert not thread.is_alive() and len(errors)==1 and isinstance(errors[0],DiscoveryCancelled)
    finally:cancel.set();thread.join(5)
    assert_joined(children)
    assert all(image._image.closed for image in images)
