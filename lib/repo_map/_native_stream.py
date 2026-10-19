"""Explicit native discovery/read/extraction with directly joined producers.

Borrows installed tofu_repo_kernel sealing, environment and terminal retirement.
Caller supplies byte budgets and trusted artifact digest. No product selector,
pool or persistent child. Python AST/reference steps cooperate between files;
artifact admission and parent CPU work are not preemptible by this boundary.
"""
from ._native_limits import StreamLimits as StreamLimits
import json
import os
from pathlib import Path
import selectors
import struct
import subprocess
import sys
from threading import Lock
import time

from lib.repo_map import outline
from ._native_source_admission import is_code_path
from tofu_repo_kernel._discovery import DiscoveryError, validate_request
from tofu_repo_kernel._process_discovery import (
    DiscoveryCancelled, DiscoveryTimeout, retire_process)
from tofu_repo_kernel._stream import StreamExecutable

from lib.log import get_logger

logger = get_logger(__name__)



FORMAT = 'tofu.repository-stream/v2'



def checkpoint(deadline, cancellation):
    expired = time.monotonic() >= deadline
    if cancellation is not None and cancellation.is_set():
        raise DiscoveryCancelled('native repository stream cancelled', timed_out=expired)
    if expired:
        raise DiscoveryTimeout('native repository stream deadline elapsed')


_StreamImage = StreamExecutable

def _retire(process, primary):
    errors = []
    try:
        retire_process(process)
    except BaseException as error:
        errors.append(error)
    finally:
        # Kill/join precedes input close: even repeated close interruptions must
        # not leave an executing child. Raw FileIO has no buffered writes.
        while not process.stdin.closed:
            try:
                process.stdin.close()
            except KeyboardInterrupt as error:
                errors.append(error)
            except BaseException as error:
                errors.append(error)
                break
    if primary is not None:
        if isinstance(primary, (DiscoveryCancelled, DiscoveryTimeout)):
            primary.returncode = process.returncode
        if errors and hasattr(primary, 'add_note'):
            primary.add_note('native stream cleanup: ' + ', '.join(type(error).__name__ for error in errors))
    elif errors:
        raise errors[0]


class _NativeEOF(ValueError):
    pass


class StreamFailure(DiscoveryError):
    def __init__(self, status, stage):
        super().__init__(status)
        self.stage = stage
        self.args = (f'native repository stream failed during {stage} (status {status})',)


class _Channel:
    def __init__(self, process, deadline, cancellation):
        self.process, self.deadline, self.cancellation = process, deadline, cancellation
        self._output_buffer = b''
        self._output_offset = 0
        self.selector = selectors.DefaultSelector()
        try:
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            self.selector.register(process.stderr, selectors.EVENT_READ)
        except BaseException:
            self.selector.close()
            raise

    def close(self):
        self.selector.close()

    def wait_ready(self, stream, event):
        self.selector.register(stream, event)
        try:
            while True:
                checkpoint(self.deadline, self.cancellation)
                remaining = self.deadline - time.monotonic()
                for key, _ in self.selector.select(min(remaining, .05) if self.cancellation is not None else remaining):
                    if key.fileobj is stream:
                        return
                    # Drain without retaining unbounded native stderr. Parent
                    # cancellation/deadline remains live even under stderr flood.
                    try:
                        data = os.read(key.fd, 65536)
                        if not data:
                            self.selector.unregister(key.fileobj)
                    except BlockingIOError:
                        pass
        finally:
            self.selector.unregister(stream)

    def read_output(self):
        # Consume ready pipe data directly. A bounded read can contain several
        # small frames; readiness registration is needed only after EAGAIN.
        while True:
            checkpoint(self.deadline, self.cancellation)
            try:
                return os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                self.wait_ready(self.process.stdout, selectors.EVENT_READ)

    def read_exact(self, size):
        checkpoint(self.deadline, self.cancellation)
        start = self._output_offset
        if size <= len(self._output_buffer) - start:
            end = start + size
            result = self._output_buffer[start:end]
            if end == len(self._output_buffer):
                self._output_buffer, self._output_offset = b'', 0
            else:
                self._output_offset = end
            return result
        data = bytearray(self._output_buffer[start:])
        self._output_buffer, self._output_offset = b'', 0
        while len(data) < size:
            part = self.read_output()
            if not part:
                raise _NativeEOF('native repository stream ended inside a frame')
            needed = size - len(data)
            data.extend(part[:needed])
            if len(part) > needed:
                self._output_buffer, self._output_offset = part, needed
        return bytes(data)

    def frame(self, limit, *, allow_fallback=False):
        length, = struct.unpack('<Q', self.read_exact(8))
        if allow_fallback and length == (1 << 64) - 1:
            return None
        if length > limit:
            raise ValueError('native repository frame exceeds caller budget')
        return self.read_exact(length)

    def send_request(self, payload, *, close=True):
        for part in (struct.pack('<Q', len(payload)), payload):
            view = memoryview(part)
            while view:
                checkpoint(self.deadline, self.cancellation)
                self.wait_ready(self.process.stdin, selectors.EVENT_WRITE)
                try:
                    written = os.write(self.process.stdin.fileno(), view)
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise ValueError('native repository request made no progress')
                view = view[written:]
        if close:
            self.process.stdin.close()

    def finish(self, *, native_status=True):
        if self._output_offset < len(self._output_buffer) or self.read_output():
            raise ValueError('native repository stream has trailing output')
        while self.process.poll() is None:
            checkpoint(self.deadline, self.cancellation)
            remaining = self.deadline - time.monotonic()
            if not self.selector.get_map():
                # Both output pipes reached EOF. Selecting no descriptors would
                # sleep the full interval even when the child has just exited.
                # Wait on the owned PID; preserve deadline/cancellation checks
                # when a child closes its pipes before finishing its work.
                try:
                    self.process.wait(timeout=min(remaining, .05) if self.cancellation is not None else remaining)
                except subprocess.TimeoutExpired:
                    pass
                continue
            for key, _ in self.selector.select(min(remaining, .05)):
                try:
                    if not os.read(key.fd, 65536):
                        self.selector.unregister(key.fileobj)
                except BlockingIOError:
                    pass
        if native_status and self.process.returncode == 3:
            raise DiscoveryTimeout('native repository discovery deadline elapsed',
                                   cancelled=self.cancellation is not None and self.cancellation.is_set())
        checkpoint(self.deadline, self.cancellation)
        if native_status and self.process.returncode:
            raise StreamFailure(self.process.returncode, 'completion')


class NativeStreamScanner:
    def __init__(self, binary, grammar_directory, *, expected_sha256, limits, strict=True, image_factory=None):
        if binary is None and image_factory is None:
            raise ValueError('native stream requires an artifact or an explicit image owner')
        self.binary = Path(binary).resolve(strict=True) if binary is not None else None
        self._image_factory = image_factory
        self.grammar_directory = Path(grammar_directory).resolve(strict=True) if grammar_directory is not None else None
        self.expected_sha256, self.limits, self.strict = expected_sha256, limits, strict
        self.calls = self.discovery_calls = 0
        self._counter_lock = Lock()

    def image_scope(self):
        """Default calls own one image; an execution owner may lend a sealed one."""
        return (self._image_factory() if self._image_factory is not None
                else _StreamImage(self.binary, self.expected_sha256))

    def __call__(self, root, *, path='', focus='', include_refs=True, include_tests=False,
                 timeout=60.0, cancellation=None):
        root, timeout_ns = validate_request(root, timeout)
        root = os.fsdecode(os.path.abspath(root))
        deadline = time.monotonic() + timeout
        checkpoint(deadline, cancellation)
        with self.image_scope() as image:
            checkpoint(deadline, cancellation)
            process = image.start(root, self.grammar_directory, timeout_ns, self.limits.arguments())
            channel = None
            stage = 'discovery'
            try:
                channel = _Channel(process, deadline, cancellation)
                raw = channel.frame(self.limits.path_bytes + 1, allow_fallback=True)
                if raw is None:
                    from ._native_discovery_fallback import negotiate
                    payload = negotiate(image, root, deadline, cancellation, self.limits,
                                        channel_type=_Channel, retire=_retire, check=checkpoint)
                    channel.send_request(payload, close=False)
                    raw = channel.frame(self.limits.path_bytes + 1)
                if not raw or raw[0] not in (0, 1, 2):
                    raise ValueError('native discovery returned an invalid path codec')
                codec, raw = raw[0], raw[1:]
                if raw and (not raw.endswith(b'\0') or raw.startswith(b'\0') or b'\0\0' in raw):
                    raise ValueError('native discovery returned an invalid path frame')
                discovered = [(name.decode('utf8', 'surrogateescape' if codec == 1 else 'replace'), index)
                              for index, name in enumerate(raw.split(b'\0')[:-1])] if raw else []
                # Git deduplicates decoded names; rg keeps replacement collisions.
                if codec == 0:
                    discovered = list(dict(discovered).items())
                discovered.sort(key=lambda entry: entry[0])
                files = [name for name, _index in discovered]
                del raw
                if any(os.path.isabs(name) or '..' in name.split('/') for name in files):
                    raise ValueError('native discovery returned a path outside its root')
                stage = 'selection'
                if path:
                    prefix = path.strip('/').rstrip('/') + '/'
                    discovered = [(name, index) for name, index in discovered
                                  if name.startswith(prefix) or name == prefix.rstrip('/')]
                if focus:
                    discovered = [(name, index) for name, index in discovered if focus.lower() in name.lower()]
                files = [name for name, _index in discovered]
                selected = [(name, index) for name, index in discovered
                            if is_code_path(name)
                            and not outline._is_generated(name)
                            and (include_tests or not outline._is_test_path(name))]
                payload = json.dumps(dict(format=FORMAT, path_indexes=[index for _name, index in selected], max_source_bytes=outline._MAX_SOURCE_BYTES,
                                          max_reference_chars=outline._MAX_REF_BYTES, include_refs=include_refs),
                                     ensure_ascii=False).encode('utf8')
                if len(payload) > self.limits.request_bytes:
                    raise ValueError('native repository selection exceeds caller budget')
                channel.send_request(payload)
                del payload
                stage = 'source'
                result = outline.RepoMap(root=root, files=files)
                extracted = False
                for name, index in selected:
                    checkpoint(deadline, cancellation)
                    row = json.loads(channel.frame(self.limits.file_frame_bytes))
                    if (set(row) != {'path_index', 'text', 'admitted', 'tags', 'grammar_error'}
                            or type(row['path_index']) is not int or row['path_index'] != index):
                        raise ValueError('native repository returned another file or malformed row')
                    if type(row['admitted']) is not bool or not isinstance(row['tags'], list):
                        raise ValueError('native repository returned invalid source metadata')
                    text = row['text']
                    if text is None:
                        if row['tags'] or row['admitted'] or row['grammar_error'] is not None:
                            raise ValueError('unreadable native source returned partial contents')
                        continue
                    if not isinstance(text, str):
                        raise ValueError('native repository returned invalid source text')
                    if row['grammar_error'] is not None:
                        if (not row['admitted'] or not isinstance(row['grammar_error'], str)
                                or row['grammar_error'] not in {'invalid_language', 'grammar_unavailable', 'grammar_abi', 'parse_failed', 'grammar_admission'}):
                            raise ValueError('native repository returned invalid grammar failure')
                        if row['tags']:
                            raise ValueError('native grammar error returned partial tags')
                        if self.strict or row['grammar_error'] == 'grammar_admission':
                            raise RuntimeError('native extraction failed for an admitted file: ' + row['grammar_error'])
                    if row['admitted'] and name.endswith('.py'):
                        if row['tags'] or row['grammar_error'] is not None:
                            raise ValueError('native worker replaced canonical Python AST')
                        tags = outline._extract_python_tags(name, text)
                    else:
                        if any(not isinstance(tag, dict) or set(tag) != {'name', 'line', 'kind', 'signature', 'indent'}
                               for tag in row['tags']):
                            raise ValueError('native worker returned malformed tag fields')
                        tags = [outline.Tag(rel_path=name, **tag) for tag in row['tags']]
                        if not row['admitted'] and tags:
                            raise ValueError('native worker returned inadmissible tags')
                        if any(type(tag.line) is not int or tag.line < 1 or type(tag.indent) is not int
                               or tag.indent < 0 or tag.kind not in ('def', 'class')
                               or not isinstance(tag.name, str) or not isinstance(tag.signature, str) for tag in tags):
                            raise ValueError('native worker returned invalid tag fields')
                        extracted |= row['admitted']
                    for tag in tags:
                        result.add_tag(tag)
                    if include_refs:
                        result.add_references(name, outline.extract_identifiers(text[:outline._MAX_REF_BYTES]))
                stage = 'completion'
                if channel.frame(self.limits.file_frame_bytes):
                    raise ValueError('native repository stream has extra file rows')
                channel.finish()
                with self._counter_lock:
                    self.discovery_calls += 1
                    self.calls += int(extracted)
                return result
            finally:
                primary = sys.exc_info()[1]
                channel_error = None
                try:
                    if channel is not None:
                        channel.close()
                except BaseException as error:
                    logger.debug('__call__ failure handled: %s', type(error).__name__)
                    channel_error = error
                finally:
                    _retire(process, primary or channel_error)
                if channel_error is not None:
                    if primary is None:
                        raise channel_error
                    if hasattr(primary, 'add_note'):
                        primary.add_note('native stream channel close: ' + type(channel_error).__name__)
                if isinstance(primary, _NativeEOF) and process.returncode is not None and process.returncode != 0:
                    expired = process.returncode == 3 or time.monotonic() >= deadline
                    if cancellation is not None and cancellation.is_set():
                        error = DiscoveryCancelled('native stream ended after cancellation', timed_out=expired)
                        error.returncode = process.returncode
                        raise error from primary
                    if expired:
                        error = DiscoveryTimeout('native stream ended after deadline')
                        error.returncode = process.returncode
                        raise error from primary
                    raise StreamFailure(process.returncode, stage) from primary
