"""Git-object provenance for evaluation source exports without a .git directory.

Git remains the revision authority. A receipt carries the exact commit and
ancestor tree objects; every exported file must match its Git blob. Generated
transport files are enumerated, never accepted as substitutes for source code.
No storage, provider, network or source mutation occurs here.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath

EVALUATION_ARTIFACT_DIRECTORY = ".eval-runs"

REQUIRED_SOURCES = ("evaluations/__main__.py", "evaluations/swebench/harbor_runner.py",
                    "tofu_agent/runtime.py", "lib/version.py")
GENERATED_FILES = frozenset({"platform-evaluation.json", "payload-source.json", "tofu-job.hope",
    "tofu-job-runner.py", "tofu-execution.json", "tofu-hope-publish.py", "tofu-publication.json", "tofu-publication-intent.json",
    "tofu-parallel-upload.jar", "platform-result.json", "platform-evidence.zip"})


def git_object_id(kind: str, body: bytes) -> str:
    return hashlib.sha1(kind.encode() + b" " + str(len(body)).encode() + b"\0" + body).hexdigest()


def safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or any(character in value for character in ("\\", "\0", "\n", "\r")):
        raise ValueError("invalid source-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or ".." in path.parts or not path.parts:
        raise ValueError("invalid source-relative path")
    return path


def tree_entries(body: bytes) -> dict[str, tuple[str, str]]:
    entries, offset = {}, 0
    while offset < len(body):
        space = body.index(b" ", offset)
        zero = body.index(b"\0", space)
        mode = body[offset:space].decode("ascii")
        name = body[space + 1:zero].decode("utf-8")
        safe_relative(name)
        if "/" in name or name in entries or zero + 21 > len(body):
            raise ValueError("invalid Git tree entry")
        entries[name] = (mode, body[zero + 1:zero + 21].hex())
        offset = zero + 21
    return entries


def verify_source_receipt(root: Path, receipt_path: Path, *, expected_revision: str | None = None) -> dict:
    root = root.resolve(strict=True)
    if receipt_path.is_symlink() or not receipt_path.resolve(strict=True).is_relative_to(root):
        raise ValueError("source receipt must belong to the worker source root")
    with receipt_path.open("rb") as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("source receipt exceeds byte budget")
    receipt = json.loads(raw)
    if not isinstance(receipt, dict) or set(receipt) != {"version", "revision", "commit", "trees", "files"} \
            or type(receipt["version"]) is not int or receipt["version"] != 1:
        raise ValueError("invalid evaluation source receipt")
    commit = base64.b64decode(receipt["commit"], validate=True)
    revision = git_object_id("commit", commit)
    if revision != receipt["revision"] or (expected_revision is not None and revision != expected_revision):
        raise ValueError("source commit does not match the pinned revision")
    tree_header = commit.split(b"\n", 1)[0].decode("ascii")
    if not tree_header.startswith("tree "):
        raise ValueError("source commit has no root tree")
    root_tree = tree_header[5:]
    trees = {}
    if not isinstance(receipt["trees"], dict) or len(receipt["trees"]) > 65536:
        raise ValueError("invalid source tree proof")
    for object_id, encoded in receipt["trees"].items():
        body = base64.b64decode(encoded, validate=True)
        if git_object_id("tree", body) != object_id:
            raise ValueError("source tree hash mismatch")
        trees[object_id] = tree_entries(body)
    files = receipt["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 65536:
        raise ValueError("source file count exceeds admission")
    seen = set()
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "repository_path", "blob", "mode"}:
            raise ValueError("invalid source file proof")
        path = safe_relative(row["path"])
        repository_path = safe_relative(row["repository_path"])
        sdk_prefix = "packages/tofu-environments/tofu_environments/"
        expected_path = "tofu_environments/" + str(repository_path)[len(sdk_prefix):] \
            if str(repository_path).startswith(sdk_prefix) else str(repository_path)
        if str(path) != expected_path:
            raise ValueError("source export cannot relocate retained modules")
        if str(path) in seen or str(path) in GENERATED_FILES:
            raise ValueError("duplicate or reserved source file")
        seen.add(str(path))
        tree = root_tree
        for part in repository_path.parts[:-1]:
            mode, tree = trees[tree][part]
            if mode != "40000":
                raise ValueError("source ancestor is not a Git tree")
        mode, blob = trees[tree][repository_path.name]
        if mode not in {"100644", "100755"} or blob != row["blob"] or mode != row["mode"]:
            raise ValueError("source file differs from its committed tree")
        local = root
        for part in path.parts:
            local /= part
            if local.is_symlink():
                raise ValueError("symlink in source export")
        if not local.is_file():
            raise ValueError("exported source is not a regular file")
        digest = hashlib.sha1(b"blob " + str(local.stat().st_size).encode() + b"\0")
        with local.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != blob:
            raise ValueError("exported source bytes differ from the committed blob")
    if not set(REQUIRED_SOURCES) <= seen:
        raise ValueError("source export is missing retained runtime entry points")
    # Check only the admitted source tree; benchmark caches/artifacts are a
    # declared reconstructible output lifecycle, not source imports.
    import os
    observed = set()
    for current, directories, names in os.walk(root, followlinks=False):
        relative = Path(current).relative_to(root)
        directories[:] = [name for name in directories if name not in
            {"__pycache__", ".runtime", "benchmark-runs"}]
        if relative == Path("."):
            directories[:] = [name for name in directories if name != EVALUATION_ARTIFACT_DIRECTORY]
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise ValueError("symlink directory in source export")
        for name in names:
            path = relative / name
            if (Path(current) / name).is_symlink():
                raise ValueError("symlink file in source export")
            if str(path) not in GENERATED_FILES:
                observed.add(path.as_posix())
            if len(observed) > 65536:
                raise ValueError("unregistered files exceed source export budget")
    if observed != seen:
        raise ValueError("unregistered or missing files in source export")
    return {"kind": "git-object-proof", "revision": revision,
            "receipt_sha256": hashlib.sha256(raw).hexdigest(), "file_count": len(seen)}
