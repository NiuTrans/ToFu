"""android/native/check_proot.py must reject proot builds the app cannot ship."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_NATIVE = Path(__file__).resolve().parents[2] / "android" / "native"
_spec = importlib.util.spec_from_file_location(
    "check_proot", _NATIVE / "check_proot.py"
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_PINS = check.read_pins(_NATIVE / "pins.env")
_MIN_SDK = 26
_FALLBACK = _PINS["PROOT_UNBUNDLE_LOADER"]

# Trimmed llvm-readelf (NDK r27d) output of a passing build.
_PROOT = {
    "header": (
        "  Class:                             ELF64\n"
        "  Data:                              2's complement, little endian\n"
        "  Type:                              DYN (Shared object file)\n"
        "  Machine:                           AArch64\n"
    ),
    "segments": (
        "  Type           Offset   VirtAddr           PhysAddr           FileSiz  MemSiz   Flg Align\n"
        "  INTERP         0x0002a8 0x00000000000002a8 0x00000000000002a8 0x000015 0x000015 R   0x1\n"
        "      [Requesting program interpreter: /system/bin/linker64]\n"
        "  LOAD           0x000000 0x0000000000000000 0x0000000000000000 0x016e14 0x016e14 R   0x4000\n"
        "  LOAD           0x016e14 0x000000000001ae14 0x000000000001ae14 0x029eac 0x029eac R E 0x4000\n"
        "  DYNAMIC        0x041c30 0x0000000000049c30 0x0000000000049c30 0x000180 0x000180 RW  0x8\n"
    ),
    "dynamic": (
        "  0x0000000000000001 (NEEDED)       Shared library: [libdl.so]\n"
        "  0x0000000000000001 (NEEDED)       Shared library: [libc.so]\n"
        "  0x000000000000001e (FLAGS)        BIND_NOW\n"
        "  0x000000006ffffffb (FLAGS_1)      NOW PIE\n"
    ),
    "notes": (
        "  Android              0x00000084\tNT_ANDROID_TYPE_IDENT\n"
        "   description data: 1a 00 00 00 72 32 37 64 00 00\n"
    ),
    "versions": "",
}
_LOADER = {
    "header": _PROOT["header"].replace(
        "DYN (Shared object file)", "EXEC (Executable file)"
    ),
    "segments": (
        "  LOAD           0x000000 0x0000000000200000 0x0000000000200000 0x000120 0x000120 R   0x4000\n"
        "  LOAD           0x004000 0x0000005400000000 0x0000005400000000 0x000400 0x000400 R E 0x4000\n"
    ),
    "dynamic": "",
    "notes": "",
    "versions": "",
}
_CONTENTS = b"\x7fELF...PROOT_LOADER\0..." + _FALLBACK.encode() + b"/loader\0..."


def _proot_problems(views=None, contents=_CONTENTS):
    return check.proot_problems(views or _PROOT, contents, _MIN_SDK, _FALLBACK)


def _with(views, view, old, new):
    assert old in views[view]
    return {**views, view: views[view].replace(old, new)}


def test_a_shippable_pair_passes():
    assert _proot_problems() == []
    assert check.loader_problems(_LOADER) == []


@pytest.mark.parametrize(
    ("view", "old", "new", "reason"),
    [
        ("segments", "R E 0x4000", "R E 0x1000", "aligned to 0x1000"),
        (
            "segments",
            "0x016e14 0x000000000001ae14",
            "0x016e10 0x000000000001ae14",
            "not congruent",
        ),
        ("dynamic", "[libc.so]", "[libtalloc.so]", "libtalloc.so"),
        (
            "dynamic",
            "(FLAGS)        BIND_NOW",
            "(RUNPATH)      Library runpath: [$ORIGIN]",
            "DT_RUNPATH",
        ),
        ("dynamic", "BIND_NOW\n", "BIND_NOW TEXTREL\n", "text relocations"),
        ("dynamic", "NOW PIE", "NOW", "not a PIE"),
        ("notes", "1a 00 00 00", "18 00 00 00", "API level 24"),
        ("versions", "", "  0x0001: Name: GLIBC_2.17", "glibc"),
        ("header", "AArch64", "Advanced Micro Devices X86-64", "Machine"),
        (
            "segments",
            "/system/bin/linker64",
            "/lib/ld-linux-aarch64.so.1",
            "interpreter",
        ),
    ],
)
def test_proot_defects_fail(view, old, new, reason):
    views = {**_PROOT, view: new} if old == "" else _with(_PROOT, view, old, new)
    problems = _proot_problems(views)
    assert any(reason in problem for problem in problems), problems


def test_bundled_loader_build_fails():
    problems = _proot_problems(contents=b"PROOT_LOADER\0")
    assert any("PROOT_UNBUNDLE_LOADER" in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("view", "old", "new", "reason"),
    [
        (
            "header",
            "EXEC (Executable file)",
            "DYN (Shared object file)",
            "not a static executable",
        ),
        (
            "segments",
            "  LOAD           0x000000",
            "  INTERP         0x0002a8 0x2a8 0x2a8 0x15 0x15 R 0x1\n  LOAD           0x000000",
            "PT_INTERP",
        ),
        ("segments", "R   0x4000", "R   0x1000", "aligned to 0x1000"),
    ],
)
def test_loader_defects_fail(view, old, new, reason):
    problems = check.loader_problems(_with(_LOADER, view, old, new))
    assert any(reason in problem for problem in problems), problems


def test_committed_pins_licences_and_readme_agree():
    licences = _NATIVE / "licenses"
    assert (
        hashlib.sha256((licences / "GPL-3.0.txt").read_bytes()).hexdigest()
        == _PINS["GPL3_SHA256"]
    )
    proot_copying = (licences / "proot-COPYING").read_text(encoding="utf-8")
    assert (
        "GNU GENERAL PUBLIC LICENSE\n                       Version 2, June 1991"
        in proot_copying
    )
    talloc_license = (licences / "talloc-LICENSE").read_text(encoding="utf-8")
    assert (
        "GNU LESSER GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007"
        in talloc_license
    )
    assert "UNKNOWN" not in (_NATIVE / "talloc-cross-answers.txt").read_text()
    readme = (_NATIVE / "README.md").read_text(encoding="utf-8")
    for key in (
        "NDK_VERSION",
        "PROOT_VERSION",
        "PROOT_COMMIT",
        "PROOT_SHA256",
        "TALLOC_VERSION",
        "TALLOC_SHA256",
        "GPL3_SHA256",
        "PROOT_UNBUNDLE_LOADER",
    ):
        assert _PINS[key] in readme, key
