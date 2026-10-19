"""Prepare a reviewable batch job or inspect outputs; never allocate implicitly."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def read_json(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ValueError("bounded regular JSON input required")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluations platform")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Export committed sources and freeze an owned platform job")
    for name in ("repository", "project-root", "output-root", "arguments", "provider-options"):
        prepare.add_argument("--" + name, type=Path, required=True)
    for name in ("revision", "run-id", "project"):
        prepare.add_argument("--" + name, required=True)
    prepare.add_argument("--owner-id", type=int, required=True)
    prepare.add_argument("--tenant-id", default="")
    prepare.add_argument("--cpu", type=int, required=True)
    prepare.add_argument("--memory-gib", type=float, required=True)
    prepare.add_argument("--gpu-count", type=int, default=0)
    prepare.add_argument("--runtime-seconds", type=int, required=True)
    prepare.add_argument("--result-bytes", type=int, required=True)
    prepare.add_argument("--source-byte-limit", type=int)
    prepare.add_argument("--source-file-limit", type=int)
    inspect = commands.add_parser("inspect", help="Interpret SDK-collected outputs without scoring")
    inspect.add_argument("directory", type=Path)
    inspect.add_argument("--registration", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            from .collection import inspect_collected_outputs
            result = inspect_collected_outputs(args.directory, read_json(args.registration))
        else:
            from tofu_environments import Actor
            from runtime_guards import probe_system_resources
            from .payload import prepare_platform_job
            observation = probe_system_resources()
            capacity = max(1, observation.effective_memory_available_mb or 32) * 1048576
            free = max(1, observation.disk_free_mb or 4) * 1048576
            result = prepare_platform_job(
                Actor(owner_id=args.owner_id, tenant_id=args.tenant_id, project_root=str(args.project_root.resolve())),
                repository=args.repository, revision=args.revision, arguments=read_json(args.arguments),
                run_id=args.run_id, output_root=args.output_root, project=args.project,
                resources={"cpu": args.cpu, "memory_gib": args.memory_gib, "gpu_count": args.gpu_count},
                provider_options=read_json(args.provider_options), runtime_seconds=args.runtime_seconds,
                result_bytes=args.result_bytes,
                source_byte_limit=args.source_byte_limit or max(1, min(capacity // 8, free // 8)),
                source_file_limit=args.source_file_limit or max(1, min(65536, capacity // 4096, free // 8192)))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, ImportError, KeyError) as exc:
        print(f"platform evaluation: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
