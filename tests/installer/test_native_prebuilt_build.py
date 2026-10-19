"""Native musl compilers may be reused only for the matching host architecture."""
import pytest

from scripts import build_tofudb_prebuilt as builder

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('host,target', [('x86_64', 'x86_64'), ('aarch64', 'aarch64'),
                                        ('arm64', 'aarch64')])
def test_native_musl_toolchain_is_accepted(host, target, monkeypatch):
    monkeypatch.setattr(builder.platform, 'machine', lambda: host)
    monkeypatch.setattr(builder.shutil, 'which',
                        lambda name: '/usr/bin/musl-gcc' if name == 'musl-gcc' else None)
    triple = target + '-unknown-linux-musl'
    environment = builder._musl_cross_env(triple)
    assert environment['CC_' + triple.replace('-', '_')] == '/usr/bin/musl-gcc'
    assert environment['CARGO_TARGET_' + triple.upper().replace('-', '_') + '_LINKER'] == '/usr/bin/musl-gcc'


def test_cross_build_never_uses_wrong_arch_native_compiler(monkeypatch):
    monkeypatch.setattr(builder.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(builder.shutil, 'which',
                        lambda name: '/usr/bin/musl-gcc' if name == 'musl-gcc' else None)
    with pytest.raises(builder.BuildError, match='musl C toolchain'):
        builder._musl_cross_env('aarch64-unknown-linux-musl')
