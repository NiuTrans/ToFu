"""The musl asset must be a self-contained static binary, not a hybrid PIE.

Regression for the 0.18.3 x86_64-musl release: Rust links the musl triple as a
static PIE, but Ubuntu 22.04's musl 1.2.2-4 `musl-gcc` specs predate the
static-PIE rule, so the link produced an ELF that declared
`PT_INTERP=/lib/ld-musl-x86_64.so.1` *and* used musl's rcrt1.o self-relocator.
The kernel mapped the interpreter on top of that self-relocating crt, `_start_c`
faulted before `main`, and the no-arg smoke run exited -11 (SIGSEGV) on
x86_64-musl only — aarch64 musl (newer Debian specs) and macOS never link that
way. These tests pin the two halves of the fix: the static link is requested
for every musl build, and a loader-dependent artifact is refused instead of
being packaged.
"""
import struct

import pytest

from scripts import build_tofudb_prebuilt as builder

pytestmark = pytest.mark.unit

ELF_HEADER = b'\x7fELF\x02\x01\x01' + b'\x00' * 9


def _elf64(program_header_types, *, endian='<'):
    """Build the minimum ELF64 image the header reader inspects."""
    header = bytearray(64)
    header[:7] = ELF_HEADER[:7]
    header[4] = 2  # ELFCLASS64
    header[5] = 1 if endian == '<' else 2
    phoff = 64
    phentsize = 56
    struct.pack_into(endian + 'Q', header, 32, phoff)
    struct.pack_into(endian + 'HH', header, 54, phentsize, len(program_header_types))
    table = bytearray(phentsize * len(program_header_types))
    for index, p_type in enumerate(program_header_types):
        struct.pack_into(endian + 'I', table, index * phentsize, p_type)
    return bytes(header + table)


@pytest.mark.parametrize('triple', ['x86_64-unknown-linux-musl',
                                    'aarch64-unknown-linux-musl'])
def test_musl_build_requests_the_non_pie_static_link(triple, monkeypatch):
    monkeypatch.setattr(builder.platform, 'machine', lambda: triple.split('-')[0])
    monkeypatch.setattr(builder.shutil, 'which',
                        lambda name: '/usr/bin/musl-gcc' if name == 'musl-gcc' else None)
    rustflags = builder._musl_cross_env(triple)['RUSTFLAGS'].split()
    assert 'target-feature=+crt-static' in rustflags
    assert 'relocation-model=static' in rustflags


def test_inherited_rustflags_are_extended_never_replaced(monkeypatch):
    monkeypatch.setattr(builder.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(builder.shutil, 'which',
                        lambda name: '/usr/bin/musl-gcc' if name == 'musl-gcc' else None)
    monkeypatch.setenv('RUSTFLAGS', '-C target-cpu=native')
    monkeypatch.delenv('CARGO_ENCODED_RUSTFLAGS', raising=False)
    rustflags = builder._musl_cross_env('x86_64-unknown-linux-musl')['RUSTFLAGS'].split()
    assert rustflags[:2] == ['-C', 'target-cpu=native']
    assert 'relocation-model=static' in rustflags


def test_encoded_rustflags_are_extended_in_cargo_wire_format(monkeypatch):
    monkeypatch.setattr(builder.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(builder.shutil, 'which',
                        lambda name: '/usr/bin/musl-gcc' if name == 'musl-gcc' else None)
    monkeypatch.setenv('CARGO_ENCODED_RUSTFLAGS', '-C\x1ftarget-cpu=native')
    encoded = builder._musl_cross_env('x86_64-unknown-linux-musl')[
        'CARGO_ENCODED_RUSTFLAGS'].split('\x1f')
    assert encoded[:3] == ['-C', 'target-cpu=native', '-C']
    assert 'relocation-model=static' in encoded


def test_interpreter_program_header_is_detected(tmp_path):
    dependent = tmp_path / 'dependent'
    dependent.write_bytes(_elf64([1, builder.PT_INTERP]))
    assert builder._elf_declares_interpreter(dependent) is True

    static = tmp_path / 'static'
    static.write_bytes(_elf64([1, 2]))
    assert builder._elf_declares_interpreter(static) is False


def test_musl_artifact_with_a_program_interpreter_is_refused(tmp_path):
    dependent = tmp_path / 'tofu-db'
    dependent.write_bytes(_elf64([builder.PT_INTERP]))
    with pytest.raises(builder.BuildError, match='not a static executable'):
        builder._require_static_musl_artifact(dependent, 'x86_64-unknown-linux-musl')


def test_static_musl_artifact_is_accepted(tmp_path):
    static = tmp_path / 'tofu-db'
    static.write_bytes(_elf64([1, 2]))
    builder._require_static_musl_artifact(static, 'x86_64-unknown-linux-musl')


@pytest.mark.parametrize('triple', ['x86_64-apple-darwin', 'x86_64-unknown-linux-gnu',
                                    'x86_64-pc-windows-msvc'])
def test_non_musl_targets_keep_their_platform_link_mode(triple, tmp_path):
    """macOS/Windows/glibc legitimately have an interpreter; never refuse them."""
    with_interpreter = tmp_path / 'binary'
    with_interpreter.write_bytes(_elf64([builder.PT_INTERP]))
    builder._require_static_musl_artifact(with_interpreter, triple)


def test_non_elf_artifact_is_reported_not_silently_accepted(tmp_path):
    not_elf = tmp_path / 'tofu-db.exe'
    not_elf.write_bytes(b'MZ' + b'\x00' * 62)
    with pytest.raises(builder.BuildError, match='not an ELF binary'):
        builder._elf_declares_interpreter(not_elf)


def test_artifact_without_an_elf_header_interpreter_is_still_smoked(tmp_path, monkeypatch):
    """The static gate must not replace the no-arg identification smoke test."""
    static = tmp_path / 'tofu-db'
    static.write_bytes(_elf64([1, 2]))
    builder._require_static_musl_artifact(static, 'x86_64-unknown-linux-musl')
    observed = {}

    def fake_run(command, **_kwargs):
        observed['command'] = command
        return builder.subprocess.CompletedProcess(command, 2, '', 'usage: tofu-db <init>')

    monkeypatch.setattr(builder, '_run', fake_run)
    builder._smoke(static)
    assert observed['command'] == [str(static)]
