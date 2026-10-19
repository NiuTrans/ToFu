"""Frozen Harbor request admission; reuse the maintained CLI and dataset locks.

No provider imports, authentication or external writes. Paths are worker inputs;
the launcher owns the output root and trial identity. Internal Harbor retries
are prohibited so failed attempts remain evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from evaluations.catalog import BENCHMARKS
from evaluations.swebench.constants import HARBOR_COMMIT


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def parse_arguments(arguments: list[str]):
    from evaluations.swebench.cli import build_parser
    from rootless_vm.harness_profiles import harness_profiles
    if not isinstance(arguments, list) or not arguments or any(
        not isinstance(value, str) or not value or len(value) > 4096 for value in arguments
    ) or len(arguments) > 256:
        raise ValueError("bounded Harbor arguments required")
    prohibited = {"--output-root", "--run-id", "--harbor-bin", "--source-receipt", "--dry-run", "--limit", "--help", "-h"}
    if any(value.split("=", 1)[0] in prohibited for value in arguments):
        raise ValueError("platform launcher owns output, executable and run identity; select exact tasks")
    if any("\x00" in value or "\n" in value or "\r" in value or
           re.search(r"https?://|\bBearer\s|\bsk-[A-Za-z0-9_-]{12,}", value) for value in arguments):
        raise ValueError("credentials and URLs are not platform evaluation arguments")
    try:
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            args = build_parser().parse_args(["run", *arguments])
    except SystemExit as exc:
        raise ValueError("invalid Harbor run arguments") from exc
    agents = {"oracle"} | {name for profile in harness_profiles()
                           for name in (profile.profile_id, profile.harbor_agent)}
    if args.agent not in agents:
        raise ValueError("platform evaluation requires a registered agent harness")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", model) for model in args.models):
        raise ValueError("public model identifiers required")
    if len(args.models) != len(set(args.models)):
        raise ValueError("duplicate model identifiers")
    if args.max_retries not in (None, 0):
        raise ValueError("platform evaluation cannot erase attempts with Harbor internal retries")
    definition = BENCHMARKS[args.benchmark]
    definition.validate_backend(args.backend)
    if args.attempts is not None and not 1 <= args.attempts <= 100:
        raise ValueError("attempt budget must be 1..100")
    if len(args.tasks) != len(set(args.tasks)) or set(args.tasks) - definition.task_digests().keys():
        raise ValueError("task selection must match the frozen task lock")
    return args


def compile_request(arguments: list[str], *, run_id: str, runner_revision: str,
                    runtime_seconds: int, result_bytes: int, console_bytes: int) -> dict:
    args = parse_arguments(arguments)
    definition = BENCHMARKS[args.benchmark]
    credentials = credential_names(args)

    frozen = list(arguments)
    if args.max_retries is None:
        frozen += ["--max-retries", "0"]
    request = {"version": 1, "run_id": run_id, "runner_revision": runner_revision,
               "benchmark": definition.key, "dataset_ref": definition.dataset_source_revision,
               "task_lock_sha256": digest(definition.task_digests()), "harbor_commit": HARBOR_COMMIT,
               "arguments": frozen, "credential_env_names": sorted(credentials),
               "limits": {"runtime_seconds": runtime_seconds, "result_bytes": result_bytes,
                          "console_bytes": console_bytes}}
    validate_request(request)
    return request


def credential_names(args) -> set[str]:
    credentials = set(args.secret_env)
    if args.agent in {"tofu-kimi", "rootless_vm.harbor_tofu_runtime_agent:TofuKimiRuntimeAgent",
                      "codex-kimi", "rootless_vm.harbor_codex_kimi_agent:CodexKimiGuestAgent"}:
        credentials.add(args.kimi_api_key_env)
    if args.agent in {"opencode", "rootless_vm.harbor_opencode_agent:OpenCodeGuestAgent"}:
        credentials.add(args.glm_api_key_env)
    return credentials


def validate_request(request: dict) -> None:
    from jsonschema import Draft202012Validator
    schema = json.loads(Path(__file__).with_name("request.schema.json").read_text())
    if not Draft202012Validator(schema).is_valid(request) or type(request.get("version")) is not int:
        raise ValueError("platform evaluation request does not match its contract")
    if len(canonical(request)) > 256 * 1024:
        raise ValueError("platform evaluation request exceeds byte budget")
    args = parse_arguments(request["arguments"])
    definition = BENCHMARKS[args.benchmark]
    if (request["benchmark"] != definition.key or request["dataset_ref"] != definition.dataset_source_revision
            or request["task_lock_sha256"] != digest(definition.task_digests())
            or request["harbor_commit"] != HARBOR_COMMIT or args.max_retries != 0
            or request["credential_env_names"] != sorted(credential_names(args))):
        raise ValueError("platform benchmark, engine or task identity drifted")
    if request["limits"]["console_bytes"] > request["limits"]["result_bytes"]:
        raise ValueError("console budget exceeds total result budget")
    if request["limits"]["result_bytes"] < 8192:
        raise ValueError("result budget must reserve at least 8192 bytes for receipts")
