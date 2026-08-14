"""Command-line diagnostics for the rootless VM core."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

from .harbor_runner import HarborRunSpec, harbor_argv, run_harbor
from .image_cache import PreparedImageCache, PreparedImageSpec
from .qemu import QemuRuntime, QemuUnavailableError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rootless_vm")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="launch a no-disk TCG VM and verify QMP")
    doctor.add_argument("--qemu")
    doctor.add_argument("--qemu-img")
    doctor.add_argument("--json", action="store_true")
    prepare = subparsers.add_parser(
        "prepare", help="build or validate a digest-addressed offline task cache"
    )
    prepare.add_argument("--qemu")
    prepare.add_argument("--qemu-img")
    prepare.add_argument("--cache-root", type=Path, required=True)
    prepare.add_argument("--base-disk", type=Path, required=True)
    prepare.add_argument("--base-disk-sha256")
    prepare.add_argument("--payload-iso", type=Path, required=True)
    prepare.add_argument("--payload-iso-sha256")
    prepare.add_argument("--task-image", required=True)
    prepare.add_argument("--python-runtime-image")
    prepare.add_argument("--memory-mib", type=int, default=2048)
    prepare.add_argument("--cpus", type=int, default=2)
    prepare.add_argument("--boot-timeout-sec", type=float, default=360.0)
    prepare.add_argument("--json", action="store_true")
    run = subparsers.add_parser(
        "run", help="run one Harbor task through the local rootless QEMU harness"
    )
    run.add_argument("--harbor", default="harbor")
    run.add_argument("--task-path", type=Path, required=True)
    run.add_argument("--base-disk", type=Path, required=True)
    run.add_argument("--base-disk-sha256", required=True)
    run.add_argument("--payload-iso", type=Path, required=True)
    run.add_argument("--payload-iso-sha256", required=True)
    run.add_argument("--task-image", required=True)
    run.add_argument("--python-runtime-image")
    run.add_argument("--state-root", type=Path, required=True)
    run.add_argument("--cache-root", type=Path, required=True)
    run.add_argument("--jobs-dir", type=Path, required=True)
    run.add_argument("--job-name")
    run.add_argument("--model", default="deepseek-v4-flash-meituan")
    run.add_argument("--oracle", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        try:
            report = QemuRuntime.discover(args.qemu, args.qemu_img).preflight()
        except (OSError, ValueError, QemuUnavailableError) as exc:
            payload = {"ok": False, "error": str(exc)}
            print(json.dumps(payload, ensure_ascii=False) if args.json else f"FAIL: {exc}")
            return 2
        payload = report.to_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else f"PASS: {payload}")
        return 0
    if args.command == "prepare":
        try:
            runtime = QemuRuntime.discover(args.qemu, args.qemu_img)
            result = PreparedImageCache(
                PreparedImageSpec(
                    runtime=runtime,
                    cache_root=args.cache_root,
                    base_disk=args.base_disk,
                    payload_iso=args.payload_iso,
                    task_image=args.task_image,
                    python_runtime_image=args.python_runtime_image,
                    expected_base_disk_sha256=args.base_disk_sha256,
                    expected_payload_iso_sha256=args.payload_iso_sha256,
                    memory_mib=args.memory_mib,
                    cpus=args.cpus,
                    boot_timeout_sec=args.boot_timeout_sec,
                )
            ).prepare()
        except (OSError, ValueError, RuntimeError, QemuUnavailableError) as exc:
            payload = {"ok": False, "error": str(exc)}
            print(json.dumps(payload, ensure_ascii=False) if args.json else f"FAIL: {exc}")
            return 2
        payload = {
            "ok": True,
            "disk": str(result.disk),
            "image_reference": result.image_reference,
            "recipe_digest": result.recipe_digest,
            "cache_hit": result.cache_hit,
            "elapsed_sec": round(result.elapsed_sec, 3),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else f"PASS: {payload}")
        return 0
    if args.command == "run":
        spec = HarborRunSpec(
            harbor=args.harbor,
            task_path=args.task_path,
            base_disk=args.base_disk,
            base_disk_sha256=args.base_disk_sha256,
            image_iso=args.payload_iso,
            image_iso_sha256=args.payload_iso_sha256,
            image_reference=args.task_image,
            python_runtime_image=args.python_runtime_image,
            state_root=args.state_root,
            prepared_cache_root=args.cache_root,
            jobs_dir=args.jobs_dir,
            job_name=args.job_name,
            model=args.model,
            oracle=args.oracle,
        )
        try:
            argv = harbor_argv(spec)
            if args.dry_run:
                print(shlex.join(argv))
                return 0
            return run_harbor(spec)
        except (OSError, ValueError, PermissionError) as exc:
            print(f"FAIL: {exc}")
            return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
