#!/usr/bin/env python3
"""Static checks on the proot native libraries that build-proot.sh packages.

usage: python3 android/native/check_proot.py [BUILD_DIR]   (default: android/native/build)

Reads BUILD_DIR/jniLibs/arm64-v8a/{libproot.so,libproot-loader.so} with the
llvm-readelf of the NDK pinned in pins.env ($ANDROID_HOME/ndk/<NDK_VERSION>),
prints the evidence (size, SHA-256, LOAD rows, NEEDED) and exits 1 unless:
- both files are ELF64 little-endian AArch64, every PT_LOAD is aligned to at
  least 16 KiB and its file offset and address agree modulo 16 KiB, and no
  glibc symbol version is referenced;
- libproot.so is a PIE for /system/bin/linker64 built for the app's minSdk,
  needs only Android system libraries, has no RPATH/RUNPATH or text
  relocations, and finds its loader through PROOT_LOADER (unbundled loader);
- libproot-loader.so is a static executable;
- licenses/GPL-3.0.txt matches its pin.
Only a device can run these binaries; see README.md for what that verifies.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Android's largest page size: 16 KB-page devices map every LOAD on this boundary.
PAGE_SIZE_16K = 0x4000
ANDROID_SYSTEM_LIBRARIES = frozenset({"libc.so", "libdl.so", "libm.so", "liblog.so"})
INTERPRETER = "/system/bin/linker64"
READELF_VIEWS = {
    "header": "-hW",
    "segments": "-lW",
    "dynamic": "-dW",
    "notes": "-nW",
    "versions": "-VW",
}


def read_pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            pins[key] = value
    return pins


def app_min_sdk(gradle_file: Path) -> int:
    match = re.search(
        r"^\s*minSdk\s*=\s*(\d+)", gradle_file.read_text(encoding="utf-8"), re.M
    )
    if match is None:
        raise SystemExit(f"no minSdk in {gradle_file}")
    return int(match.group(1))


def load_segments(segments_view: str) -> list[tuple[int, int, int]]:
    """(offset, address, alignment) of each LOAD row of `llvm-readelf -lW`."""
    rows = []
    for line in segments_view.splitlines():
        fields = line.split()
        if fields and fields[0] == "LOAD":
            rows.append((int(fields[1], 16), int(fields[2], 16), int(fields[-1], 16)))
    return rows


def needed_libraries(dynamic_view: str) -> list[str]:
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", dynamic_view)


def android_api_level(notes_view: str) -> int | None:
    """API level of NT_ANDROID_TYPE_IDENT: the first 4 description bytes, little-endian."""
    match = re.search(
        r"NT_ANDROID_TYPE_IDENT\s+description data:((?: [0-9a-f]{2}){4})", notes_view
    )
    return (
        None
        if match is None
        else int.from_bytes(bytes.fromhex(match.group(1)), "little")
    )


def has_segment(segments_view: str, kind: str) -> bool:
    return re.search(rf"^\s*{kind}\s", segments_view, re.M) is not None


def elf_problems(views: dict[str, str]) -> list[str]:
    problems = []
    header = views["header"]
    for field, value in (
        ("Class", "ELF64"),
        ("Data", "2's complement, little endian"),
        ("Machine", "AArch64"),
    ):
        if not re.search(rf"^\s*{field}:\s+{re.escape(value)}\s*$", header, re.M):
            problems.append(f"ELF {field} is not {value}")
    segments = load_segments(views["segments"])
    if not segments:
        problems.append("no LOAD segment")
    for offset, address, alignment in segments:
        if alignment < PAGE_SIZE_16K:
            problems.append(
                f"LOAD at {address:#x} is aligned to {alignment:#x}, below {PAGE_SIZE_16K:#x}"
            )
        elif offset % PAGE_SIZE_16K != address % PAGE_SIZE_16K:
            problems.append(
                f"LOAD at {address:#x} has offset {offset:#x}, not congruent modulo {PAGE_SIZE_16K:#x}"
            )
    if "GLIBC_" in views["versions"]:
        problems.append("references glibc symbol versions")
    return problems


def proot_problems(
    views: dict[str, str], contents: bytes, min_sdk: int, unbundle_loader_dir: str
) -> list[str]:
    problems = elf_problems(views)
    dynamic = views["dynamic"]
    if not re.search(r"^\s*Type:\s+DYN\b", views["header"], re.M) or not re.search(
        r"\(FLAGS_1\).*\bPIE\b", dynamic
    ):
        problems.append("not a PIE")
    if f"[Requesting program interpreter: {INTERPRETER}]" not in views["segments"]:
        problems.append(f"interpreter is not {INTERPRETER}")
    foreign = sorted(set(needed_libraries(dynamic)) - ANDROID_SYSTEM_LIBRARIES)
    if foreign:
        problems.append(
            f"NEEDED outside Android system libraries: {', '.join(foreign)}"
        )
    for tag in ("RPATH", "RUNPATH", "TEXTREL"):
        if f"({tag})" in dynamic:
            problems.append(f"has DT_{tag}")
    if re.search(r"\(FLAGS\).*\bTEXTREL\b", dynamic):
        problems.append("has text relocations")
    api_level = android_api_level(views["notes"])
    if api_level != min_sdk:
        problems.append(
            f"built for API level {api_level}, but the app's minSdk is {min_sdk}"
        )
    for needle in (b"PROOT_LOADER\0", f"{unbundle_loader_dir}/loader\0".encode()):
        if needle not in contents:
            problems.append(
                f"no {needle!r}: not built with PROOT_UNBUNDLE_LOADER={unbundle_loader_dir}"
            )
    return problems


def loader_problems(views: dict[str, str]) -> list[str]:
    problems = elf_problems(views)
    if not re.search(r"^\s*Type:\s+EXEC\b", views["header"], re.M):
        problems.append("not a static executable (ELF type is not EXEC)")
    for kind in ("INTERP", "DYNAMIC"):
        if has_segment(views["segments"], kind):
            problems.append(f"has a PT_{kind} segment")
    return problems


def readelf_views(readelf: Path, path: Path) -> dict[str, str]:
    return {
        name: subprocess.run(
            [str(readelf), flag, str(path)], check=True, capture_output=True, text=True
        ).stdout
        for name, flag in READELF_VIEWS.items()
    }


def main(argv: list[str]) -> int:
    build_dir = Path(argv[1]) if len(argv) > 1 else HERE / "build"
    pins = read_pins(HERE / "pins.env")
    android_home = os.environ.get("ANDROID_HOME")
    if not android_home:
        raise SystemExit(
            f"set ANDROID_HOME to the Android SDK that holds ndk/{pins['NDK_VERSION']}"
        )
    readelf = Path(
        android_home,
        "ndk",
        pins["NDK_VERSION"],
        "toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf",
    )
    min_sdk = app_min_sdk(HERE.parent / "app" / "build.gradle.kts")

    failed = False
    libs = build_dir / "jniLibs" / "arm64-v8a"
    for name in ("libproot.so", "libproot-loader.so"):
        path = libs / name
        if not path.is_file():
            print(f"FAIL {name}: missing {path}")
            failed = True
            continue
        contents = path.read_bytes()
        views = readelf_views(readelf, path)
        print(
            f"== {name}  size={len(contents)}  sha256={hashlib.sha256(contents).hexdigest()}"
        )
        for line in views["segments"].splitlines():
            if has_segment(line, "LOAD") or "program interpreter" in line:
                print(f"  {line.strip()}")
        for library in needed_libraries(views["dynamic"]) or ["(none)"]:
            print(f"  NEEDED {library}")
        if name == "libproot.so":
            problems = proot_problems(
                views, contents, min_sdk, pins["PROOT_UNBUNDLE_LOADER"]
            )
        else:
            problems = loader_problems(views)
        for problem in problems:
            print(f"FAIL {name}: {problem}")
        print(
            f"PASS {name}"
            if not problems
            else f"FAIL {name}: {len(problems)} problem(s)"
        )
        failed = failed or bool(problems)

    gpl3 = HERE / "licenses" / "GPL-3.0.txt"
    if hashlib.sha256(gpl3.read_bytes()).hexdigest() != pins["GPL3_SHA256"]:
        print(f"FAIL licenses: {gpl3.name} does not match GPL3_SHA256")
        failed = True
    for licence in ("proot-COPYING", "talloc-LICENSE"):
        if not (HERE / "licenses" / licence).is_file():
            print(f"FAIL licenses: missing {licence}")
            failed = True
    print("FAIL" if failed else "PASS all checks")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
