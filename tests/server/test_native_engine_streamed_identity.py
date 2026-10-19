"""Source identity keeps its byte protocol with bounded reads and race refusal."""
import hashlib
import os
from pathlib import Path

import pytest

from serverctl_pkg import native_engine as engines

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path):
    crate = tmp_path/'packages/tofu-db'
    (crate/'src').mkdir(parents=True)
    manifest = crate/'Cargo.toml'
    manifest.write_text('[package]\nname="tofu-db"\nversion="0.0.0"\n'
                        '[package.metadata.tofu]\nexternal-inputs=["contracts/input.json"]\n')
    source = crate/'src/lib.rs'
    source.write_bytes(b'pub fn fixture() {}\n')
    external = tmp_path/'contracts/input.json'
    external.parent.mkdir()
    external.write_bytes(bytes(range(256)) * 8200)
    # Independent spelling of the existing v2 protocol and this exact fixture.
    expected = hashlib.sha256(b'tofu-db-source-v2\0')
    for key, path in sorted([('tofu-db/Cargo.toml', manifest),
                             ('tofu-db/src/lib.rs', source),
                             ('@external/contracts/input.json', external)]):
        content = path.read_bytes()
        expected.update(key.encode()+b'\0')
        expected.update(len(content).to_bytes(8, 'little'))
        expected.update(content)
    return tmp_path, external, expected.hexdigest()


def wrapped_input(monkeypatch, target, transform):
    original = Path.open
    def open_path(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get('mode', 'r')
        return transform(stream) if path == target and mode == 'rb' else stream
    monkeypatch.setattr(Path, 'open', open_path)


def test_large_source_fingerprint_keeps_exact_protocol_without_whole_file_reads(project, monkeypatch):
    root, external, expected = project
    reads = []
    class BoundedReader:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def fileno(self): return self.stream.fileno()
        def read(self, size=-1):
            assert 0 < size <= 1 << 20, 'source fingerprint must bound every read'
            reads.append(size)
            return self.stream.read(size)
    wrapped_input(monkeypatch, external, BoundedReader)
    assert engines.source_fingerprint(root) == expected
    assert len(reads) >= 3


@pytest.mark.parametrize('change', ['grow', 'shrink', 'rewrite'])
def test_source_changed_during_streaming_refuses_identity(project, monkeypatch, change):
    root, external, _ = project
    class MutatingReader:
        changed = False
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def fileno(self): return self.stream.fileno()
        def read(self, size=-1):
            result = self.stream.read(size)
            if not self.changed:
                self.changed = True
                before = external.stat()
                with external.open('r+b') as writer:
                    if change == 'grow': writer.seek(0, 2); writer.write(b'changed')
                    elif change == 'shrink': writer.truncate(0)
                    else: writer.write(b'changed')
                os.utime(external, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            return result
    wrapped_input(monkeypatch, external, MutatingReader)
    with pytest.raises(engines.EnginePreparationError, match='changed'):
        engines.source_fingerprint(root)


def test_empty_source_stays_in_identity_and_later_changes_are_observed(project):
    root, external, original = project
    external.write_bytes(b'')
    empty = engines.source_fingerprint(root)
    assert empty != original and engines.source_fingerprint(root) == empty
    external.write_bytes(b'x')
    assert engines.source_fingerprint(root) not in (original, empty)
