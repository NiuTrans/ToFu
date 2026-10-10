#!/usr/bin/env python3
"""Bind tofu-db filesystem-certification evidence to its exact build and filesystem.

The native CLI emits `measurements_are_observations_not_release_certification=true`
for every certification cell: the numbers are bounded observations, not a release
record. `docs/STORAGE.md` states that release certification must bind these
observations to the exact build and filesystem assumptions. No native command
produces that binding. This stdlib-only generator combines:

  * the release binary identity (SHA-256, ELF build ID, strip state),
  * the source-tree identity (git HEAD plus dirty state, because an uncommitted
    tree means the commit alone cannot stand in for the build),
  * the toolchain identity (rustc/cargo versions),
  * the filesystem identity of every real-path certification target
    (statfs type, device ID, mount source/options),
  * the parsed machine-readable cell lines from one or more certification runs.

It emits one JSON record on stdout (or `--output`) with a canonical SHA-256
digest of the record body so the binding itself is content-addressed. The
generator never runs the certification; it only binds evidence the operator
already captured, so it cannot manufacture a pass.

Usage:
  gen_tofudb_cert_binding.py \
      --binary packages/tofu-db/target/release/tofu-db \
      --run role=single-xfs,target=/path/single,log=single.txt \
      --run role=matrix,target=/path/matrix,log=matrix.txt,mount_target=/path/mount \
      [--output binding.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MATRIX_FORMAT = "tofu-db.filesystem-certification-matrix.v1"
SINGLE_FORMAT = "tofu-db.filesystem-certification.v1"
REQUIRED_MATRIX_CELLS = [
    "primary.path",
    "mount.opt-in",
    "vfs-fault.syscall-error.interrupted",
    "vfs-fault.syscall-error.storage-full",
    "vfs-fault.short-write",
    "vfs-fault.lost-sync",
]


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binary_identity(binary: Path) -> dict:
    binary = binary.resolve()
    if not binary.is_file():
        raise SystemExit(f"binary not found: {binary}")
    identity = {
        "path": str(binary),
        "sha256": _sha256_file(binary),
        "size_bytes": binary.stat().st_size,
        "elf_build_id": None,
        "stripped": None,
        "file_description": None,
    }
    file_bin = shutil.which("file")
    if file_bin:
        description = _run([file_bin, "-b", str(binary)]).strip()
        identity["file_description"] = description
        identity["stripped"] = ("not stripped" not in description)
    readelf = shutil.which("readelf")
    if readelf:
        notes = _run([readelf, "-n", str(binary)])
        match = re.search(r"Build ID:\s*([0-9a-f]+)", notes)
        if match:
            identity["elf_build_id"] = match.group(1)
    if identity["stripped"] is not False:
        # The README documents that link-time stripping zeroes initialized data;
        # refuse to bind a record whose strip state is unknown or stripped.
        raise SystemExit(
            "binary strip state is not verified-unstripped; binding refused "
            f"(file(1) said: {identity['file_description']!r})"
        )
    return identity


def _tool_identity(tool_path_hint: str | None) -> dict:
    identity: dict[str, str | None] = {"rustc": None, "cargo": None, "os": None, "kernel": None}
    for tool in ("rustc", "cargo"):
        candidate = None
        if tool_path_hint:
            hinted = Path(tool_path_hint) / tool
            if hinted.exists():
                candidate = str(hinted)
        candidate = candidate or shutil.which(tool)
        if candidate:
            identity[tool] = _run([candidate, "--version"]).strip()
    identity["os"] = _run(["uname", "-srmo"]).strip()
    return identity


def _git_identity(repo: Path) -> dict:
    def git(*args: str) -> str:
        return _run(["git", "-C", str(repo), *args]).strip()

    dirty = git("status", "--porcelain").splitlines()
    return {
        "repo": str(repo),
        "head_commit": git("rev-parse", "HEAD"),
        "dirty": bool(dirty),
        "dirty_entry_count": len(dirty),
        # An uncommitted tree means head_commit alone does not identify the
        # build; binary.sha256 is then the only exact build anchor.
        "commit_identifies_build": not dirty,
    }


def _filesystem_identity(path: Path) -> dict:
    path = path.resolve()
    if not path.exists():
        raise SystemExit(f"certification target missing: {path}")
    stat_out = _run(["stat", "-f", "-c", "%T|%S", str(path)]).strip().split("|")
    device = int(_run(["stat", "-c", "%d", str(path)]).strip())
    mount_source = mount_type = mount_options = None
    findmnt = shutil.which("findmnt")
    if findmnt:
        probe = subprocess.run(
            [findmnt, "-n", "-o", "SOURCE,FSTYPE,OPTIONS", "--target", str(path)],
            capture_output=True, text=True,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            parts = probe.stdout.strip().split(None, 2)
            mount_source = parts[0]
            if len(parts) > 1:
                mount_type = parts[1]
            if len(parts) > 2:
                mount_options = parts[2]
    return {
        "path": str(path),
        "statfs_type": stat_out[0],
        "statfs_block_bytes": int(stat_out[1]),
        "device_id_decimal": device,
        "device_id_hex": format(device, "x"),
        "mount_source": mount_source,
        "mount_fstype": mount_type,
        "mount_options": mount_options,
    }


def _parse_fields(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in line.split():
        key, separator, value = token.partition("=")
        if separator:
            fields[key] = value
    return fields


def _parse_run(spec: dict[str, str]) -> dict:
    log_path = Path(spec["log"]).resolve()
    if not log_path.is_file():
        raise SystemExit(f"certification log not found: {log_path}")
    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    cells: list[dict] = []
    summary: dict[str, str] | None = None
    single: dict[str, str] | None = None
    for line in lines:
        fields = _parse_fields(line)
        fmt = fields.get("format")
        if fmt == MATRIX_FORMAT:
            if "matrix_result" in fields:
                summary = fields
            elif "cell" in fields:
                cells.append(fields)
        elif fmt == SINGLE_FORMAT:
            single = fields
    run: dict[str, object] = {
        "role": spec["role"],
        "log": str(log_path),
        "log_sha256": _sha256_file(log_path),
        "target": _filesystem_identity(Path(spec["target"])),
    }
    if spec.get("mount_target"):
        run["mount_target"] = _filesystem_identity(Path(spec["mount_target"]))
    if cells or summary is not None:
        cell_names = [cell["cell"] for cell in cells]
        missing = [cell for cell in REQUIRED_MATRIX_CELLS if cell not in cell_names]
        run["matrix"] = {
            "cells": cells,
            "summary": summary,
            "required_cells_present": not missing,
            "missing_required_cells": missing,
        }
    if single is not None:
        run["single_certification"] = single
    if not cells and single is None:
        raise SystemExit(f"log contains no certification evidence: {log_path}")
    return run


def _parse_run_spec(raw: str) -> dict[str, str]:
    spec: dict[str, str] = {}
    for pair in raw.split(","):
        key, separator, value = pair.partition("=")
        if not separator or key not in {"role", "target", "log", "mount_target"}:
            raise SystemExit(f"bad --run component: {pair!r}")
        spec[key] = value
    if not {"role", "target", "log"} <= spec.keys():
        raise SystemExit(f"--run requires role=,target=,log= : {raw!r}")
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--run", action="append", default=[], required=True,
                        help="role=NAME,target=DIR,log=FILE[,mount_target=DIR]")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--tool-bin", default=os.environ.get("CARGO_HOME") and str(Path(os.environ["CARGO_HOME"]) / "bin"))
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    runs = [_parse_run(_parse_run_spec(raw)) for raw in arguments.run]
    record: dict[str, object] = {
        "format": "tofu-db.filesystem-certification-binding.v1",
        "created_unix_seconds": int(__import__("time").time()),
        "binary": _binary_identity(arguments.binary),
        "toolchain": _tool_identity(arguments.tool_bin),
        "source": _git_identity(arguments.repo.resolve()),
        "contract": {
            "path": "contracts/tofudb_ir_v1.json",
            "matrix_format": MATRIX_FORMAT,
            "required_matrix_cells": REQUIRED_MATRIX_CELLS,
            "single_format": SINGLE_FORMAT,
        },
        "runs": runs,
    }
    body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    record["binding_sha256"] = hashlib.sha256(body).hexdigest()
    output = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.write_text(output)
        print(f"wrote {arguments.output} binding_sha256={record['binding_sha256']}", file=sys.stderr)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
