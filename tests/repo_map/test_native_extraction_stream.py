"""Owned native stream parity and retirement under blocked reads and interrupts."""

# Synthetic repository/package fixtures, never shipped-source anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = ('tests/spec.js',)
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import threading

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def stream(tmp_path, monkeypatch):
    binary = os.environ.get('TOFU_NATIVE_STREAM_BINARY')
    grammars = os.environ.get('TOFU_NATIVE_EXTRACTION_GRAMMARS')
    if not binary or not grammars:
        pytest.skip('explicit native stream binary and grammar directory required')
    pytest.importorskip('tofu_repo_kernel')
    monkeypatch.syspath_prepend(str(ROOT / 'benchmarks/repository_index'))
    from lib.repo_map._native_stream import NativeStreamScanner, StreamLimits
    for name in list(os.environ):
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    config = tmp_path / 'global-git-config'
    config.write_text('')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    root = tmp_path / 'project'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    limits = StreamLimits(path_bytes=8 << 20, request_bytes=8 << 20,
                          file_frame_bytes=32 << 20, source_read_bytes=16 << 20)
    scanner = NativeStreamScanner(binary, grammars, expected_sha256=hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
                                limits=limits)
    return scanner, root


def observe_children(monkeypatch):
    import lib.repo_map._native_stream as module
    original = module.subprocess.Popen
    children = []
    def start(*args, **kwargs):
        assert 'cwd' not in kwargs
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(module.subprocess, 'Popen', start)
    return children


def assert_joined(children):
    assert children
    for child in children:
        assert child.returncode is not None and child.poll() is not None
        assert all(stream.closed for stream in (child.stdin, child.stdout, child.stderr))


def test_stream_matches_canonical_files_tags_references_and_source_options(stream):
    pytest.importorskip('tree_sitter_language_pack')
    from domain_fixtures import fixture
    from lib.repo_map import outline
    scanner, root = stream
    for name, source in fixture('multilingual_extraction').items():
        (root / name).write_text(source)
    (root / 'generated.min.js').write_text('function ignored() {}')
    (root / 'upper.PY').write_text('def uppercase(): pass\n')
    (root / 'data.json').write_text('{"ignored":1}')
    (root / 'tests').mkdir()
    (root / 'tests/spec.js').write_text('function testOnly() {}')
    scanner.strict = False  # Canonical upper.PY grammar degradation remains file-local.
    for options in ({}, dict(focus='MODULE_1'), dict(path='module_1.js'),
                    dict(include_refs=False), dict(include_tests=True)):
        assert asdict(scanner(root, **options)) == asdict(outline.scan(str(root), **options))
    assert scanner.calls == scanner.discovery_calls == 5


def test_stream_source_decoding_matches_text_io_newlines_and_reference_prefix(stream, monkeypatch):
    from lib.repo_map import outline
    scanner, root = stream
    raw_cases = [b'', b'\xef\xbb\xbfdef bom(): pass\r\n', b'def first(): pass\rdef second(): pass\r\n',
                 b'#\xed\xa0\x80\xe2\x82\xff\r\ndef invalid(): pass\n',
                 ('# ' + '\r\n界😀' * 100000 + '\ndef outside(): pass\n').encode()]
    rng = random.Random(106151)
    raw_cases += [b'#' + bytes(rng.randrange(256) for _ in range(500)) + b'\r\ndef sampled(): pass\n' for _ in range(80)]
    for index, raw in enumerate(raw_cases):
        (root / f'source_{index:03}.py').write_bytes(raw)
    ast_inputs, reference_inputs = {}, []
    ast, identifiers = outline._extract_python_tags, outline.extract_identifiers
    def observed_ast(name, text):
        ast_inputs[name] = text
        return ast(name, text)
    def observed_references(text):
        reference_inputs.append(text)
        return identifiers(text)
    monkeypatch.setattr(outline, '_extract_python_tags', observed_ast)
    monkeypatch.setattr(outline, 'extract_identifiers', observed_references)
    expected = outline.scan(str(root))
    expected_ast, expected_refs = ast_inputs.copy(), reference_inputs.copy()
    ast_inputs.clear(); reference_inputs.clear()
    assert asdict(scanner(root)) == asdict(expected)
    assert ast_inputs == expected_ast and reference_inputs == expected_refs
    assert scanner.calls == 0 and scanner.discovery_calls == 1


@pytest.mark.parametrize('failure', ['timeout', 'cancel'])
def test_blocked_source_read_is_killed_joined_and_never_publishes_partial_map(stream, monkeypatch, failure):
    import lib.repo_map._native_stream as module
    from lib.repo_map import outline
    scanner, root = stream
    (root / 'a.py').write_text('def completed(): pass\n')
    blocked = root / 'z.py'
    blocked.write_text('def blocked(): pass\n')
    subprocess.run(['git', '-C', str(root), 'add', 'z.py'], check=True)
    blocked.unlink(); os.mkfifo(blocked)
    children = observe_children(monkeypatch)
    cancel = threading.Event()
    observed = []
    original = outline._extract_python_tags
    def first(name, text):
        observed.append(name)
        if failure == 'cancel':
            cancel.set()
        return original(name, text)
    monkeypatch.setattr(outline, '_extract_python_tags', first)
    expected = module.DiscoveryTimeout if failure == 'timeout' else module.DiscoveryCancelled
    with pytest.raises(expected) as caught:
        scanner(root, timeout=1, cancellation=cancel)
    assert observed == ['a.py']
    assert caught.value.returncode == children[0].returncode
    assert caught.value.cancelled == (failure == 'cancel')
    assert scanner.calls == scanner.discovery_calls == 0
    assert_joined(children)


def test_primary_error_survives_repeated_cleanup_interrupts(stream, monkeypatch):
    import lib.repo_map._native_stream as module
    scanner, root = stream
    (root / 'sample.py').write_text('def sample(): pass\n')
    original = module.subprocess.Popen
    children, counts = [], dict(kill=0, wait=0)
    def start(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        kill, wait = child.kill, child.wait
        def interrupted_kill():
            counts['kill'] += 1
            if counts['kill'] <= 2:
                raise KeyboardInterrupt()
            return kill()
        def interrupted_wait(*args, **kwargs):
            counts['wait'] += 1
            if counts['wait'] <= 2:
                raise KeyboardInterrupt()
            return wait(*args, **kwargs)
        child.kill, child.wait = interrupted_kill, interrupted_wait
        return child
    monkeypatch.setattr(module.subprocess, 'Popen', start)
    primary = LookupError('injected request preparation failure')
    def refuse(*args):
        raise primary
    monkeypatch.setattr(module._Channel, 'send_request', refuse)
    with pytest.raises(LookupError) as caught:
        scanner(root)
    assert caught.value is primary
    assert counts['kill'] >= 3 and counts['wait'] >= 3
    assert any('KeyboardInterrupt' in note for note in primary.__notes__)
    assert_joined(children)


def test_empty_git_root_and_plain_directory_are_distinct(stream, monkeypatch, tmp_path):
    scanner, root = stream
    assert scanner(root).files == []
    plain = tmp_path / 'plain'
    plain.mkdir()
    (plain / 'sample.py').write_text('def sample(): pass\n')
    children = observe_children(monkeypatch)
    assert scanner(plain).files == ['sample.py']
    assert_joined(children)


def test_stream_preserves_real_tool_publication_and_owner_boundaries(stream):
    pytest.importorskip('tree_sitter_language_pack')
    from check_native_extraction_tool import check
    from lib.repo_map._native_stream import NativeStreamScanner
    scanner, _ = stream
    def factory(binary, grammar_directory, *, strict=True):
        return NativeStreamScanner(binary, grammar_directory, strict=strict,
                                   expected_sha256=scanner.expected_sha256, limits=scanner.limits)
    result = check(scanner.binary, scanner.grammar_directory, scanner_type=factory, initialize_git=True)
    assert result['joined_cleanup'] and result['foreign_root_refused']
    assert result['foreign_owner_refused'] and result['admission_and_file_local_failure_parity']


def test_canonical_tag_corpus_passes_through_streaming_file_reads(stream):
    pytest.importorskip('tree_sitter_language_pack')
    from check_native_extraction import corpus
    from lib.repo_map import outline
    scanner, root = stream
    for index, row in enumerate(corpus()):
        path = root / f'case_{index}' / row['rel_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(row['source'].encode('utf8'))
    assert asdict(scanner(root)) == asdict(outline.scan(str(root)))


def test_channel_close_failure_preserves_primary_and_joins(stream, monkeypatch):
    import lib.repo_map._native_stream as module
    scanner, root = stream
    children = observe_children(monkeypatch)
    primary = LookupError('injected primary')
    def fail(*args):
        raise primary
    original = module._Channel.close
    def close(channel):
        original(channel)
        raise OSError('injected close failure')
    monkeypatch.setattr(module._Channel, 'send_request', fail)
    monkeypatch.setattr(module._Channel, 'close', close)
    with pytest.raises(LookupError) as caught:
        scanner(root)
    assert caught.value is primary
    assert any('channel close: OSError' in note for note in primary.__notes__)
    assert_joined(children)


def test_expired_cancelled_requests_never_start_a_child(stream, monkeypatch):
    import lib.repo_map._native_stream as module
    scanner, root = stream
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid request launched a child')
    monkeypatch.setattr(module.subprocess, 'Popen', forbidden)
    cancel = threading.Event(); cancel.set()
    with pytest.raises(module.DiscoveryCancelled) as caught:
        scanner(root, timeout=0, cancellation=cancel)
    assert caught.value.timed_out and caught.value.cancelled and caught.value.returncode is None
    with pytest.raises(module.DiscoveryTimeout):
        scanner(root, timeout=0)


def test_sealed_stream_image_survives_replacement_and_next_call_refuses(stream, tmp_path, monkeypatch):
    import lib.repo_map._native_stream as module
    scanner, root = stream
    copy = tmp_path / 'worker-copy'
    shutil.copyfile(scanner.binary, copy)
    scanner.binary = copy
    (root / 'sample.py').write_text('def sealedImage(): pass\n')
    children = observe_children(monkeypatch)
    original = module._StreamImage.start
    def replace(image, *args):
        copy.write_bytes(b'replaced after sealing')
        return original(image, *args)
    monkeypatch.setattr(module._StreamImage, 'start', replace)
    assert scanner(root).tags['sample.py'][0].name == 'sealedImage'
    with pytest.raises(ValueError, match='digest'):
        scanner(root)
    assert len(children) == 1
    assert_joined(children)


@pytest.mark.parametrize('policy', ['explicit', 'launch'])
def test_source_exceeding_read_budget_refuses_whole_stream(stream, monkeypatch, policy):
    from dataclasses import replace
    from lib.repo_map._native_limits import StreamLimits
    import lib.repo_map._native_stream as module
    from lib.repo_map import outline
    scanner, root = stream
    scanner.limits = (StreamLimits.from_launch_policy({'frame_maximum': 128 << 20}) if policy == 'launch'
                      else replace(scanner.limits, source_read_bytes=outline._MAX_SOURCE_BYTES))
    source = root / 'growing.py'
    source.write_text('')
    subprocess.run(['git', '-C', str(root), 'add', 'growing.py'], check=True)
    source.unlink(); os.mkfifo(source)
    errors = []
    def writer():
        try:
            with source.open('wb', buffering=0) as output:
                output.write(b'x' * (outline._MAX_SOURCE_BYTES + 1))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=writer)
    worker.start()
    children = observe_children(monkeypatch)
    try:
        with pytest.raises(module.StreamFailure) as caught:
            scanner(root, timeout=5)
        assert caught.value.status == 2 and caught.value.stage == 'source'
    finally:
        # Unblock an unaccepted writer on an unexpected pre-open failure as well.
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
        try:
            worker.join(timeout=5)
        finally:
            os.close(descriptor)
            worker.join(timeout=5)
    assert not worker.is_alive() and not errors
    assert scanner.calls == scanner.discovery_calls == 0
    assert_joined(children)


@pytest.mark.parametrize('fault', ['oversized_path_frame', 'foreign_path', 'foreign_row', 'truncated_row',
                                   'missing_terminal', 'trailing_output', 'signal', 'blocked_request',
                                   'unknown_codec', 'empty_codec', 'boolean_index', 'foreign_tag_path'])
def test_protocol_failures_are_withheld_and_joined(stream, monkeypatch, fault):
    import lib.repo_map._native_stream as module
    scanner, root = stream
    children = []
    code = '''
import json, os, signal, struct, sys, time
fault=sys.argv[1]
def frame(data):
    sys.stdout.buffer.write(struct.pack('<Q',len(data))+data);sys.stdout.buffer.flush()
if fault=='oversized_path_frame':
    sys.stdout.buffer.write(struct.pack('<Q',(8<<20)+2));sys.stdout.buffer.flush();time.sleep(60)
elif fault=='unknown_codec':
    frame(b'\\x03sample.py\\0');time.sleep(60)
elif fault=='empty_codec':
    frame(b'');time.sleep(60)
elif fault=='foreign_path':
    frame(b'\\0../foreign.py\\0');time.sleep(60)
elif fault=='blocked_request':
    frame(b'\\0'+b''.join((str(i)+'x'*128+'.py').encode()+b'\\0' for i in range(20000)));time.sleep(60)
else:
    frame(b'\\0sample.py\\0')
    length=struct.unpack('<Q',sys.stdin.buffer.read(8))[0]
    request=json.loads(sys.stdin.buffer.read(length));assert sys.stdin.buffer.read()==b''
    if fault=='signal': os.kill(os.getpid(),signal.SIGTERM)
    row=json.dumps(dict(path_index=1 if fault=='foreign_row' else False if fault=='boolean_index' else 0,
        text='def sample(): pass\\n',admitted=True,tags=[{'rel_path':'foreign.py'}] if fault=='foreign_tag_path' else [],grammar_error=None)).encode()
    if fault=='truncated_row':
        sys.stdout.buffer.write(struct.pack('<Q',len(row))+row[:5]);sys.stdout.buffer.flush();os._exit(0)
    frame(row)
    if fault=='missing_terminal': os._exit(0)
    frame(b'')
    if fault=='trailing_output': sys.stdout.buffer.write(b'!');sys.stdout.buffer.flush()
'''
    def start(image, *args):
        child = subprocess.Popen([os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), '-c', code, fault],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        children.append(child)
        return child
    monkeypatch.setattr(module._StreamImage, 'start', start)
    expected = module.DiscoveryTimeout if fault == 'blocked_request' else module.StreamFailure if fault == 'signal' else ValueError
    with pytest.raises(expected) as caught:
        scanner(root, timeout=2)
    if fault == 'signal':
        assert caught.value.status == -15 and caught.value.stage == 'source'
    assert scanner.calls == scanner.discovery_calls == 0
    assert_joined(children)


def test_stderr_is_drained_before_and_after_the_terminal_frame(stream, monkeypatch):
    import lib.repo_map._native_stream as module
    scanner, root = stream
    children = []
    code = '''
import json, os, struct, sys
def frame(data):
    sys.stdout.buffer.write(struct.pack('<Q',len(data))+data);sys.stdout.buffer.flush()
frame(b'\\0sample.py\\0')
length=struct.unpack('<Q',sys.stdin.buffer.read(8))[0]
request=json.loads(sys.stdin.buffer.read(length));assert sys.stdin.buffer.read()==b''
assert request['path_indexes']==[0]
os.write(2,b'x'*131072)
frame(json.dumps(dict(path_index=0,text='def stderrSafe(): pass\\n',admitted=True,tags=[],grammar_error=None)).encode())
frame(b'');os.close(1)
os.write(2,b'y'*131072)
'''
    def start(image, *args):
        child = subprocess.Popen([os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), '-c', code],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        children.append(child)
        return child
    monkeypatch.setattr(module._StreamImage, 'start', start)
    assert scanner(root, timeout=10).tags['sample.py'][0].name == 'stderrSafe'
    assert children[0].returncode == 0
    assert_joined(children)


@pytest.mark.parametrize('outcome', ['success', 'timeout', 'cancel'])
def test_closed_output_pipes_wait_for_owned_exit_and_remain_cancellable(stream, monkeypatch, outcome):
    import lib.repo_map._native_stream as module
    scanner, root = stream
    cancel = threading.Event()
    children, waits = [], []
    code = '''
import os, struct, sys, time
sys.stdout.buffer.write(struct.pack('<Q',1)+b'\\0');sys.stdout.buffer.flush()
length=struct.unpack('<Q',sys.stdin.buffer.read(8))[0]
sys.stdin.buffer.read(length);assert sys.stdin.buffer.read()==b''
sys.stdout.buffer.write(struct.pack('<Q',0));sys.stdout.buffer.flush()
os.close(1);os.close(2)
time.sleep(0.1 if sys.argv[1]=='success' else 60)
'''
    def start(image, *args):
        child = subprocess.Popen([os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), '-c', code, outcome],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        children.append(child)
        wait = child.wait
        def observed_wait(timeout=None):
            if timeout is not None:
                waits.append(timeout)
                if outcome == 'cancel':
                    cancel.set()
            return wait(timeout=timeout)
        child.wait = observed_wait
        return child
    monkeypatch.setattr(module._StreamImage, 'start', start)
    select = module.selectors.DefaultSelector.select
    def observed_select(selector, timeout=None):
        assert selector.get_map(), 'output EOF must wait for PID, not an empty selector'
        return select(selector, timeout)
    monkeypatch.setattr(module.selectors.DefaultSelector, 'select', observed_select)
    if outcome == 'success':
        assert scanner(root, timeout=5).files == []
        assert children[0].returncode == 0 and scanner.discovery_calls == 1
    else:
        error = module.DiscoveryTimeout if outcome == 'timeout' else module.DiscoveryCancelled
        with pytest.raises(error):
            scanner(root, timeout=1, cancellation=cancel if outcome == 'cancel' else None)
        assert scanner.discovery_calls == 0
    assert waits
    assert_joined(children)


@pytest.mark.parametrize('transport', ['coalesced', 'fragmented', 'coalesced_trailer'])
def test_frame_boundaries_survive_read_ahead_and_fragmentation(stream, monkeypatch, transport):
    import lib.repo_map._native_stream as module
    scanner, root = stream
    children = []
    code = '''
import json, os, struct, sys
transport=sys.argv[1]
def frame(data): return struct.pack('<Q',len(data))+data
os.write(1,frame(b'\\0a.py\\0b.py\\0'))
length=struct.unpack('<Q',sys.stdin.buffer.read(8))[0]
request=json.loads(sys.stdin.buffer.read(length));assert sys.stdin.buffer.read()==b''
assert request['path_indexes']==[0,1]
data=b''.join(frame(json.dumps(dict(path_index=index,text='def '+['a','b'][index]+'(): pass\\n',admitted=True,tags=[],grammar_error=None)).encode()) for index in request['path_indexes'])+frame(b'')
if transport=='coalesced_trailer': data+=b'forbidden'
if transport=='fragmented':
    for start in range(0,len(data),3): os.write(1,data[start:start+3])
else: os.write(1,data)
'''
    def start(image, *args):
        child = subprocess.Popen([os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), '-c', code, transport],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        children.append(child)
        return child
    monkeypatch.setattr(module._StreamImage, 'start', start)
    if transport == 'coalesced_trailer':
        with pytest.raises(ValueError, match='trailing output'):
            scanner(root)
        assert scanner.discovery_calls == 0
    else:
        read = module.os.read
        def fragmented_read(descriptor, size):
            if transport == 'fragmented' and children and descriptor == children[0].stdout.fileno():
                size = min(size, 3)
            return read(descriptor, size)
        monkeypatch.setattr(module.os, 'read', fragmented_read)
        result = scanner(root)
        assert result.files == ['a.py', 'b.py']
        assert [result.tags[name][0].name for name in result.files] == ['a', 'b']
    assert_joined(children)


@pytest.mark.parametrize('fault', ['duplicate', 'outside', 'negative', 'boolean', 'overflow', 'string', 'foreign_format', 'path_injection'])
def test_native_selection_refuses_invalid_indexes_before_reading_sources(stream, fault):
    import json
    import struct
    from lib.repo_map._native_stream import FORMAT, _retire
    scanner, root = stream
    (root/'sample.py').write_text('def selected_only(): pass\n')
    request = dict(format=FORMAT, path_indexes=[0], max_source_bytes=512*1024,
                   max_reference_chars=65536, include_refs=True)
    indexes = {'duplicate':[0,0], 'outside':[1], 'negative':[-1], 'boolean':[True],
               'overflow':[1<<64], 'string':['0']}
    if fault in indexes: request['path_indexes'] = indexes[fault]
    if fault == 'foreign_format': request['format'] = 'tofu.repository-stream/v1'
    if fault == 'path_injection': request['paths'] = ['../outside.py']
    payload = json.dumps(request).encode()
    with scanner.image_scope() as image:
        child = image.start(root, scanner.grammar_directory, 5_000_000_000, scanner.limits.arguments())
        try:
            output, errors = child.communicate(struct.pack('<Q',len(payload))+payload, timeout=10)
            assert child.returncode == 2 and not errors
            discovered = b'\0sample.py\0'
            assert output == struct.pack('<Q',len(discovered))+discovered
        finally:
            _retire(child, None)
    assert_joined([child])
