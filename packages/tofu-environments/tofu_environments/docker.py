"""Public Docker provider: owned build context, operation labels and readback."""
from __future__ import annotations
from pathlib import Path
import hashlib
import re
import tempfile
import os
import stat
import shutil
import json
import urllib.request
import urllib.error
from .command import CommandRunner
from .contracts import Actor as Actor, EnvironmentError as EnvironmentError, Snapshot as Snapshot, seal as seal

class DockerProvider:
    provider_id = "docker"
    def __init__(self, runner=None, *, context_byte_limit=1073741824, context_file_limit=100000, readiness_probe=None):
        self.runner = runner or CommandRunner()
        self.context_byte_limit, self.context_file_limit = context_byte_limit, context_file_limit
        self.operation_timeout = self.runner.timeout
        self.readiness_probe = readiness_probe or self._probe
    def catalog(self, actor, project, query):
        raw = self.runner.run(["image", "ls", "--no-trunc", "--digests", "--format", "{{json .}}"])
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        images = []
        for row in rows:
            label = str(row.get("Repository", "")) + ":" + str(row.get("Tag", ""))
            if query and query.lower() not in label.lower(): continue
            images.append({"base": row.get("ID"), "revision": row.get("Digest"), "reference": label})
            if len(images) == 20: break
        return {"images": images, "queues": [], "constraints": {"build_context": "owned local project", "ports": "loopback only"}}
    def _context(self, actor, spec):
        if not actor.project_root:
            raise EnvironmentError("project_required", "Docker builds require a declared project root")
        root = Path(actor.project_root).resolve()
        context = (root / spec["image"].get("context", ".")).resolve()
        if not context.is_relative_to(root) or not context.is_dir():
            raise EnvironmentError("context_outside_project", "Build context must be inside the declared project")
        return root, context
    def _cancel(self):
        if getattr(self.runner, "cancelled", lambda: False)():
            raise EnvironmentError("cancelled", "Context operation cancelled before dispatch")
    def _paths(self, context):
        paths = []
        for directory, folders, files in os.walk(context, followlinks=False):
            self._cancel()
            for name in folders + files:
                paths.append(Path(directory) / name)
                if len(paths) > self.context_file_limit:
                    raise EnvironmentError("context_budget", "Build context exceeds configured capacity")
        return sorted(paths)
    def _context_seal(self, context):
        # Seal a conservative superset of Docker's context. Never reimplement
        # .dockerignore matching; ignored files can only cause a safe replan.
        digest = hashlib.sha256(); count = total = 0
        for path in self._paths(context):
            if path.is_symlink():
                raise EnvironmentError("context_symlink", "Build context symlinks require a custom verified context provider")
            metadata = path.stat()
            digest.update(str(path.relative_to(context)).encode()); digest.update(b"\0")
            digest.update(str(stat.S_IMODE(metadata.st_mode)).encode()); digest.update(b"\0")
            if path.is_dir():
                digest.update(b"directory\0"); continue
            if not stat.S_ISREG(metadata.st_mode):
                raise EnvironmentError("context_special_file", "Context requires regular files")
            count += 1; total += path.stat().st_size
            if count > self.context_file_limit or total > self.context_byte_limit:
                raise EnvironmentError("context_budget", "Build context exceeds configured capacity")
            digest.update(str(path.relative_to(context)).encode()); digest.update(b"\0")
            with path.open("rb") as source:
                for block in iter(lambda: source.read(65536), b""):
                    self._cancel(); digest.update(block)
            digest.update(b"\0")
        return digest.hexdigest()
    def validate(self, actor, spec):
        root, context = self._context(actor, spec)
        image = spec["image"]
        if any(line.lstrip().upper().startswith("FROM ") for line in image["instructions"].splitlines()):
            raise EnvironmentError("invalid_recipe", "Instructions extend the declared base and must not replace FROM")
        if any(c in image["base"] for c in "\r\n "):
            raise EnvironmentError("invalid_recipe", "Base image must be one exact reference")
        if not re.fullmatch(r"(?:sha256:[a-f0-9]{64}|[^\s@]+@sha256:[a-f0-9]{64})", image["base"]):
            raise EnvironmentError("mutable_base", "Select an immutable base digest from the catalog")
        if spec.get("provider_options"):
            raise EnvironmentError("unsupported_option", "Docker provider accepts no platform-specific options")
        workspace = spec["workspace"]
        if workspace.get("queue") or workspace.get("ide"):
            raise EnvironmentError("unsupported_option", "Docker IDE startup belongs in the image command; scheduler queues require another provider")
        if workspace.get("gpu_count", 0) and workspace.get("gpu_type") != "nvidia":
            raise EnvironmentError("unsupported_gpu", "Docker GPU provider requires explicit nvidia runtime")
        for mount in workspace.get("mounts", []):
            source = Path(mount["source"]).resolve()
            if not source.is_relative_to(root) or not source.exists():
                raise EnvironmentError("mount_outside_project", "Mount source must be inside declared project")
            if not mount["target"].startswith("/") or "," in str(source) + mount["target"]:
                raise EnvironmentError("invalid_mount", "Mount paths must be safe absolute destinations")
        if spec["profile"] == "tofu_service" and not workspace.get("service_port"):
            raise EnvironmentError("readiness_required", "Service profile requires an application service_port")
        return {"provider_api": 1, "context_sha256": self._context_seal(context),
                "executable": self.runner.executable, "root": str(root), "context": str(context)}
    def create_image(self, actor, spec, operation_id):
        return "tofu-environment/" + spec["image"]["name"] + ":" + operation_id
    def start_build(self, actor, spec, image_id, operation_id, *, admission):
        _, context = self._context(actor, spec)
        dockerfile = ("FROM " + spec["image"]["base"] + "\n" + spec["image"]["instructions"] + "\n").encode()
        # A private verified snapshot closes the approval-to-build file race.
        # O_NOFOLLOW prevents swapping a selected file for an external symlink.
        with tempfile.TemporaryDirectory(prefix="tofu-environment-context-") as staging:
            staged = Path(staging)
            count = total = 0
            # Recheck the actual staging volume at dispatch: it may differ
            # from the host launch volume or have lost headroom since startup.
            copy_limit = min(self.context_byte_limit, shutil.disk_usage(staging).free // 2)
            directories = []
            for path in self._paths(context):
                relative = path.relative_to(context)
                if path.is_symlink():
                    raise EnvironmentError("context_symlink", "Build context symlinks are unsupported")
                destination = staged / relative
                if path.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    directories.append((destination, stat.S_IMODE(path.stat().st_mode)))
                    continue
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as source:
                    metadata = os.fstat(source.fileno())
                    if not stat.S_ISREG(metadata.st_mode):
                        raise EnvironmentError("context_special_file", "Context requires regular files")
                    count += 1
                    if count > self.context_file_limit:
                        raise EnvironmentError("context_budget", "Build context exceeds configured capacity")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("wb") as target:
                        while block := source.read(65536):
                            self._cancel()
                            total += len(block)
                            if total > copy_limit:
                                raise EnvironmentError("context_budget", "Build context exceeds configured capacity")
                            target.write(block)
                    destination.chmod(stat.S_IMODE(metadata.st_mode))
            for directory, mode in reversed(directories):
                directory.chmod(mode)
            if self._context_seal(staged) != admission["context_sha256"]:
                raise EnvironmentError("context_changed", "Build context changed after approval; replan")
            self.runner.run(["build", "--label", "tofu.environment.operation=" + operation_id,
                             "--label", "tofu.environment.owner=" + actor.scope,
                             "--tag", image_id, "--file", "-", str(staged)], input_bytes=dockerfile, mutation=True)
    def _inspect(self, kind, identity):
        return json.loads(self.runner.run([kind, "inspect", identity]))[0]
    def _owned(self, item, actor, operation_id):
        labels = item.get("Config", {}).get("Labels", {}) or {}
        return labels.get("tofu.environment.owner") == actor.scope and labels.get("tofu.environment.operation") == operation_id
    def image_status(self, actor, spec, image_id, operation_id):
        item = self._inspect("image", image_id)
        operation = operation_id
        if not self._owned(item, actor, operation):
            raise EnvironmentError("foreign_resource", "Image does not match approved owner/operation")
        return Snapshot("ready")
    def create_workspace(self, actor, spec, image_id, operation_id):
        image = self._inspect("image", image_id)
        if not self._owned(image, actor, operation_id) or not isinstance(image.get("Id"), str) or not image["Id"]:
            raise EnvironmentError("foreign_resource", "Workspace requires the exact owned built image")
        workspace = spec["workspace"]
        name = "tofu-env-" + operation_id
        args = ["create", "--name", name, "--label", "tofu.environment.operation=" + operation_id,
                "--label", "tofu.environment.owner=" + actor.scope, "--cpus", str(workspace["cpu"]),
                "--memory", str(workspace["memory_gib"]) + "g"]
        ports = set(workspace.get("ports", []))
        if workspace.get("service_port"): ports.add(workspace["service_port"])
        for port in sorted(ports): args.extend(["--publish", "127.0.0.1::" + str(port)])
        if workspace.get("gpu_count", 0): args.extend(["--gpus", str(workspace["gpu_count"])])
        for mount in workspace.get("mounts", []):
            args.extend(["--mount", "type=bind,src=" + str(Path(mount["source"]).resolve()) + ",dst=" + mount["target"] + (",readonly" if mount["read_only"] else "")])
        args.append(image["Id"])
        if workspace.get("startup_command"): args.extend(workspace["startup_command"])
        resource = self.runner.run(args, mutation=True).strip()
        if not resource: raise EnvironmentError("provider_protocol", "Workspace identifier missing", uncertain=True)
        return resource
    def activate_workspace(self, actor, spec, workspace_id, operation_id):
        item = self._inspect("container", workspace_id)
        if not self._owned(item, actor, operation_id):
            raise EnvironmentError("foreign_resource", "Workspace ownership does not match")
        self.runner.run(["start", workspace_id], mutation=True)
        return True
    def workspace_status(self, actor, spec, workspace_id, operation_id):
        item = self._inspect("container", workspace_id)
        if not self._owned(item, actor, operation_id):
            raise EnvironmentError("foreign_resource", "Workspace ownership does not match")
        if not item.get("State", {}).get("Running"):
            return Snapshot("failed" if item.get("State", {}).get("Status") == "exited" else "waiting")
        ports = item.get("NetworkSettings", {}).get("Ports", {}) or {}
        service_url = ""
        if spec["profile"] == "tofu_service":
            mappings = ports.get(str(spec["workspace"]["service_port"]) + "/tcp", []) or []
            owned = [x for x in mappings if x.get("HostIp") == "127.0.0.1" and str(x.get("HostPort", "")).isdigit() and 1 <= int(x["HostPort"]) <= 65535]
            if len(owned) != 1: return Snapshot("waiting")
            service_url = "http://127.0.0.1:" + str(owned[0]["HostPort"])
            if getattr(self.runner, "cancelled", lambda: False)():
                raise EnvironmentError("cancelled", "Readiness probe cancelled")
            if not self.readiness_probe(service_url + spec["workspace"].get("readiness_path", "/api/ready")):
                return Snapshot("waiting")
        safe = {key: [{"host": x.get("HostIp"), "port": x.get("HostPort")} for x in values or []] for key, values in ports.items()}
        return Snapshot("ready", {"kind": "docker", "workspace_id": workspace_id, "ports": safe,
                                  **({"service_url": service_url} if service_url else {})})
    def _probe(self, url):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(url, timeout=min(5, self.runner.timeout)) as response:
                raw = response.read(65537)
            if len(raw) > 65536: return False
            result = json.loads(raw)
            return isinstance(result, dict) and (result.get("ready") is True or isinstance(result.get("data"), dict) and result["data"].get("ready") is True)
        except (OSError, ValueError): return False
    def reconcile(self, actor, record):
        phase = record.get("uncertain_phase", record["phase"])
        operation = record["operation_id"]
        try:
            if phase in {"creating_image", "starting_build"}:
                image_id = self.create_image(actor, record["plan"]["spec"], operation)
                item = self._inspect("image", image_id)
                if self._owned(item, actor, operation):
                    return {"phase": "image_ready" if phase == "starting_build" else "image_created", "image_id": image_id}
            if phase in {"creating_workspace", "activating_workspace"}:
                item = self._inspect("container", "tofu-env-" + operation)
                if self._owned(item, actor, operation):
                    return {"phase": "activating" if item.get("State", {}).get("Running") else "workspace_created", "workspace_id": item["Id"]}
        except EnvironmentError:
            return None
        return None
