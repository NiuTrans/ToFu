"""The compiled JavaScript reactor cache is exact, private and derived data."""
from __future__ import annotations

import hashlib
import os
import stat

import pytest

wasmtime = pytest.importorskip('wasmtime')

from lib.tools.javascript import compiled_module  # noqa: E402

pytestmark = pytest.mark.unit

_WASM = wasmtime.wat2wasm('(module (func (export "answer") (result i32) i32.const 42))')
_SHA = hashlib.sha256(_WASM).hexdigest()


class _CountingWasmtime:
    """Real Wasmtime whose compilations are counted; deserialization stays real."""

    def __init__(self):
        self.compiled = 0
        outer = self

        class Module:
            deserialize = staticmethod(wasmtime.Module.deserialize)

            def __new__(cls, engine, wasm):
                outer.compiled += 1
                return wasmtime.Module(engine, wasm)

        self.Module = Module


def _engine(*, fuel=True):
    config = wasmtime.Config()
    config.consume_fuel = fuel
    config.epoch_interruption = True
    return wasmtime.Engine(config)


def _load(*, fuel=True):
    runtime = _CountingWasmtime()
    engine = _engine(fuel=fuel)
    module = compiled_module.load_compiled_module(runtime, engine, _WASM, _SHA)
    store = wasmtime.Store(engine)
    if fuel:
        store.set_fuel(10_000)
    store.set_epoch_deadline(1)
    assert wasmtime.Instance(store, module, []).exports(store)['answer'](store) == 42
    return runtime.compiled


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    path = tmp_path / 'javascript-engine' / f'quickjs-{_SHA[:16]}.cwasm'
    monkeypatch.setattr(compiled_module, 'compiled_module_path', lambda sha: str(path))
    return path


def test_later_engines_reuse_the_private_artifact_without_compiling(artifact):
    assert _load() == 1
    assert artifact.is_file()
    if hasattr(os, 'getuid'):
        assert stat.S_IMODE(artifact.stat().st_mode) & 0o077 == 0
    assert _load() == 0
    assert not [name for name in os.listdir(artifact.parent) if name.endswith('.tmp')]


@pytest.mark.parametrize('damage', ['payload_byte', 'truncated', 'foreign_engine', 'group_writable'])
def test_unusable_artifacts_are_misses_and_are_replaced(artifact, damage):
    if damage == 'foreign_engine':
        assert _load(fuel=False) == 1
    else:
        assert _load() == 1
        blob = bytearray(artifact.read_bytes())
        if damage == 'payload_byte':
            blob[-1] ^= 0xFF
            artifact.write_bytes(bytes(blob))
        elif damage == 'truncated':
            artifact.write_bytes(bytes(blob[:len(blob) // 2]))
        elif hasattr(os, 'getuid'):
            artifact.chmod(0o664)
        else:
            pytest.skip('POSIX ownership checks only')
    assert _load() == 1
    assert _load() == 0


def test_writing_an_artifact_removes_other_reactor_builds(artifact):
    artifact.parent.mkdir(parents=True)
    stale = artifact.parent / 'quickjs-0000000000000000.cwasm'
    stale.write_bytes(b'stale')
    unrelated = artifact.parent / 'notes.txt'
    unrelated.write_text('kept')
    assert _load() == 1
    assert not stale.exists()
    assert unrelated.exists() and artifact.is_file()


def test_unwritable_cache_still_returns_the_compiled_module(tmp_path, monkeypatch):
    blocker = tmp_path / 'not-a-directory'
    blocker.write_text('')
    monkeypatch.setattr(compiled_module, 'compiled_module_path',
                        lambda sha: str(blocker / 'javascript-engine' / 'quickjs.cwasm'))
    assert _load() == 1
    assert _load() == 1
