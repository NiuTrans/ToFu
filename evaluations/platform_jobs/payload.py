"""Actor-scoped batch payload preparation and admission to the existing JobService.

Export committed retained sources through Git, with a verifiable object proof.
The optional platform SDK owns external lifecycle and approval. Preparing an
archive never authenticates, allocates, submits or installs anything.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import subprocess
import zipfile

from evaluations.source_identity import REQUIRED_SOURCES, safe_relative
from evaluations.swebench.artifacts import atomic_write_json, create_run_dir
from .contract import canonical, compile_request, digest

_PUBLIC_PREFIXES = ("evaluations/", "lib/", "rootless_vm/", "tofu_agent/", "tofu_workers/",
                    "contracts/", "bootstrap_pkg/", "static/provider_templates/")
_SDK_PREFIX = "packages/tofu-environments/tofu_environments/"


def _git(repo: Path, args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], input=input_bytes,
                            capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError("cannot read pinned Git source objects")
    return result.stdout


def export_committed_sources(repo: Path, revision: str, *, byte_limit: int,
                             file_limit: int) -> tuple[dict[str, bytes], dict]:
    import re
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or byte_limit < 1 or not 1 <= file_limit <= 65536:
        raise ValueError("exact Git revision and source bounds required")
    commit = _git(repo, ["cat-file", "commit", revision])
    root_tree = commit.split(b"\n", 1)[0].decode("ascii").removeprefix("tree ")
    listing = _git(repo, ["ls-tree", "-r", "-l", "-z", revision])
    if len(listing) > 16 * 1024 * 1024:
        raise ValueError("repository tree listing exceeds byte budget")
    rows, total = [], 0
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        metadata, raw_name = raw.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        selected = name.startswith(_PUBLIC_PREFIXES) or name.startswith(_SDK_PREFIX) \
            or ("/" not in name and name.endswith((".py", ".toml")))
        if not selected:
            continue
        mode, kind, blob, size = metadata.decode("ascii").split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("source export accepts committed regular files only")
        local = "tofu_environments/" + name[len(_SDK_PREFIX):] if name.startswith(_SDK_PREFIX) else name
        safe_relative(local)
        total += int(size)
        if total > byte_limit or len(rows) >= file_limit:
            raise ValueError("source export exceeds admitted byte/file budget")
        rows.append({"path": local, "repository_path": name, "blob": blob, "mode": mode})
    if not set(REQUIRED_SOURCES) <= {row["path"] for row in rows}:
        raise ValueError("commit does not contain the retained evaluation/runtime sources")
    # Sizes are admitted from the exact immutable tree before bounded batch I/O.
    blobs = sorted({row["blob"] for row in rows})
    output = _git(repo, ["cat-file", "--batch"], input_bytes=("\n".join(blobs) + "\n").encode())
    if len(output) > byte_limit + file_limit * 128:
        raise ValueError("Git blob export exceeds admitted bytes")
    bodies, offset = {}, 0
    for expected in blobs:
        end = output.index(b"\n", offset)
        object_id, kind, length = output[offset:end].decode("ascii").split()
        if object_id != expected or kind != "blob":
            raise ValueError("Git blob batch identity mismatch")
        start, length = end + 1, int(length)
        bodies[object_id] = output[start:start + length]
        if len(bodies[object_id]) != length or output[start + length:start + length + 1] != b"\n":
            raise ValueError("incomplete Git blob export")
        offset = start + length + 1
    tree_names = {""}
    for row in rows:
        parents = Path(row["repository_path"]).parents
        tree_names.update(parent.as_posix() for parent in parents if str(parent) != ".")
    references = [revision + ":" + name if name else root_tree for name in sorted(tree_names)]
    output = _git(repo, ["cat-file", "--batch"], input_bytes=("\n".join(references) + "\n").encode())
    if len(output) > 16 * 1024 * 1024:
        raise ValueError("source ancestor proof exceeds byte budget")
    trees, offset = {}, 0
    for _ in references:
        end = output.index(b"\n", offset)
        object_id, kind, length = output[offset:end].decode("ascii").split()
        if kind != "tree":
            raise ValueError("source ancestor is not a Git tree")
        start, length = end + 1, int(length)
        trees[object_id] = base64.b64encode(output[start:start + length]).decode()
        offset = start + length + 1
    files = {row["path"]: bodies[row["blob"]] for row in rows}
    receipt = {"version": 1, "revision": revision, "commit": base64.b64encode(commit).decode(),
               "trees": trees, "files": rows}
    return files, receipt


def prepare_platform_job(actor, *, repository: Path, revision: str, arguments: list[str],
                         run_id: str, output_root: Path, project: str, resources: dict,
                         provider_options: dict, runtime_seconds: int, result_bytes: int,
                         source_byte_limit: int, source_file_limit: int) -> dict:
    from tofu_environments.jobs import validate_job_spec
    root = Path(actor.project_root).resolve(strict=True)
    output = output_root.expanduser().absolute()
    if not output.resolve().is_relative_to(root):
        raise ValueError("platform payload must belong to the declared actor project")
    request = compile_request(arguments, run_id=run_id, runner_revision=revision,
                              runtime_seconds=runtime_seconds, result_bytes=result_bytes,
                              console_bytes=min(result_bytes, max(65536, result_bytes // 32)))
    files, proof = export_committed_sources(repository, revision, byte_limit=source_byte_limit,
                                            file_limit=source_file_limit)
    # All retained worker/SDK code comes from the same committed tree; do not
    # copy a mutable working tree or silently substitute the host's SDK version.
    if "evaluations/platform_jobs/worker.py" not in files or "tofu_environments/job_runner.py" not in files:
        raise ValueError("commit must include the platform worker and public batch SDK")
    files["platform-evaluation.json"] = canonical(request)
    files["payload-source.json"] = canonical(proof)
    run_dir = create_run_dir(output, run_id)
    archive = run_dir / "worker-source.zip"
    try:
        modes = {row["path"]: row["mode"] for row in proof["files"]}
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, raw in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = int(modes.get(name, "100644"), 8) << 16
                bundle.writestr(info, raw)
        archive.chmod(0o600)
        checksum = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        source_hash = checksum.hexdigest()
        spec = validate_job_spec({"version": 1, "project": project,
            "source": {"uri": archive.as_uri(), "sha256": source_hash},
            "command": ["python3", "-m", "evaluations.platform_jobs.worker", "platform-evaluation.json"],
            "resources": resources, "provider_options": provider_options,
            "limits": {"runtime_seconds": runtime_seconds,
                       "gpu_seconds": resources["gpu_count"] * runtime_seconds,
                       "result_bytes": result_bytes},
            "outputs": ["platform-result.json", "platform-evidence.zip"]})
        registration = {"version": 1, "scope": actor.scope, "project_root": actor.project_root,
                        "run_id": run_id, "request_sha256": digest(request),
                        "request": request, "spec": spec, "spec_sha256": digest(spec),
                        "submission": "not_submitted"}
        atomic_write_json(run_dir / "platform-registration.json", registration)
        return registration
    except BaseException:
        # Preserve failed preparation for diagnosis; never adopt/replace this ID.
        atomic_write_json(run_dir / "platform-preparation-failed.json", {"run_id": run_id, "failed": True})
        raise


def plan_on_platform(service, actor, registration: dict, *, provider: str) -> dict:
    from .contract import validate_request
    validate_request(registration["request"])
    if registration.get("scope") != actor.scope or registration.get("project_root") != actor.project_root:
        raise ValueError("platform evaluation belongs to another actor/project")
    if digest(registration["request"]) != registration["request_sha256"]:
        raise ValueError("platform evaluation request changed before planning")
    if digest(registration["spec"]) != registration["spec_sha256"] \
            or registration["run_id"] != registration["request"]["run_id"]:
        raise ValueError("platform job specification changed before planning")
    # Reuse durable plan/CAS/approval/reconciliation authority. This call does
    # not submit, retry, authorize or switch the configured storage backend.
    return service.plan(actor, provider, registration["spec"], registration["run_id"])
