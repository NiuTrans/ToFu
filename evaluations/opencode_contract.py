"""Pinned OpenCode guest launch contract; no host provider credentials enter it."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from evaluations.codex_kimi_proxy.codex_contract import (
    TRIAL_HEADER,
    CodexContractError,
    validate_trial_token,
)

OPENCODE_VERSION = "1.18.29"
# Public correlation digest, not a credential. Keep it outside token redaction.
OPENCODE_TRIAL_IDENTITY_FIELD = 'trial_id_sha256'
OPENCODE_PROVIDER_NPM = "@ai-sdk/openai-compatible"
OPENCODE_PROVIDER_NPM_VERSION = "3.0.44"
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def prepare_cache_seed(path: str | Path) -> dict[str, str]:
    """Stage the offline provider-package cache OpenCode needs in the guest.

    OpenCode resolves custom providers through Bun's module cache rooted at
    ``$XDG_CACHE_HOME/opencode``; without network access the install must be
    pre-seeded on the host.  Only the pinned exact version is accepted.
    """
    root = Path(path).expanduser().resolve()
    marker = root / "node_modules" / "@ai-sdk" / "openai-compatible" / "package.json"
    if not marker.is_file():
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        manifest = root / "package.json"
        if not manifest.is_file():
            manifest.write_text(json.dumps({
                "name": "opencode-cache",
                "private": True,
                "dependencies": {
                    OPENCODE_PROVIDER_NPM: OPENCODE_PROVIDER_NPM_VERSION,
                },
            }, indent=2) + "\n", encoding="utf-8")
            manifest.chmod(0o600)
        subprocess.run(
            ["bun", "add", "--exact",
             f"{OPENCODE_PROVIDER_NPM}@{OPENCODE_PROVIDER_NPM_VERSION}"],
            cwd=root, check=True, timeout=300,
            env={"PATH": os.environ.get("PATH", os.defpath),
                 "HOME": os.environ.get("HOME", ""),
                 "LANG": "C", "LC_ALL": "C"},
        )
    return verify_cache_seed(root)


def verify_cache_seed(path: str | Path) -> dict[str, str]:
    root = Path(path).expanduser().resolve()
    marker = root / "node_modules" / "@ai-sdk" / "openai-compatible" / "package.json"
    if root.is_symlink() or not root.is_dir() or not marker.is_file():
        raise CodexContractError(
            "OpenCode provider cache seed is missing the pinned package")
    try:
        installed = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexContractError(
            "OpenCode provider cache seed package metadata is unreadable"
        ) from exc
    if not isinstance(installed, dict) \
            or str(installed.get("name") or "") != OPENCODE_PROVIDER_NPM \
            or str(installed.get("version") or "") != OPENCODE_PROVIDER_NPM_VERSION:
        raise CodexContractError(
            f"OpenCode provider cache seed must pin "
            f"{OPENCODE_PROVIDER_NPM}@{OPENCODE_PROVIDER_NPM_VERSION}")
    return {
        "path": str(root),
        "provider": OPENCODE_PROVIDER_NPM,
        "version": OPENCODE_PROVIDER_NPM_VERSION,
    }


def verify_opencode_binary(path: str, *, expected_sha256: str) -> dict[str, str]:
    binary = Path(path).resolve()
    if not binary.is_file():
        raise CodexContractError("OpenCode binary does not exist")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if digest != str(expected_sha256).lower():
        raise CodexContractError("OpenCode binary SHA-256 does not match manifest")
    try:
        version = subprocess.check_output(
            [str(binary), "--version"], text=True,
            stderr=subprocess.STDOUT, timeout=30,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexContractError("Unable to read OpenCode version") from exc
    if OPENCODE_VERSION not in version:
        raise CodexContractError(
            f"OpenCode {OPENCODE_VERSION} is required; observed {version!r}")
    return {"path": str(binary), "version": OPENCODE_VERSION, "sha256": digest}


def guest_config(*, proxy_url: str, model: str, context_tokens: int,
                 output_tokens: int, trial_token: str = "") -> dict:
    """Accept only the sandbox's exact predeclared control-plane endpoint.

    The host proxy must enforce the model and attach the actual upstream key.
    This config intentionally contains no credential, not even a key placeholder.
    """
    parsed = urlsplit(proxy_url)
    try:
        valid_host = ipaddress.ip_address(parsed.hostname or "") == ipaddress.ip_address("10.0.2.101")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid guest proxy endpoint") from exc
    if (parsed.scheme != "http" or not valid_host or port is None
            or not 1 <= port <= 65535 or parsed.username is not None
            or parsed.password is not None or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("guest proxy must be the declared control-plane endpoint")
    if not isinstance(model, str) or not _MODEL.fullmatch(model):
        raise ValueError("model must be an explicit wire model identifier")
    if (type(context_tokens) is not int or type(output_tokens) is not int
            or not 1 <= output_tokens < context_tokens <= 2_000_000):
        raise ValueError("invalid explicit model token limits")
    options: dict[str, object] = {"baseURL": proxy_url.rstrip("/") + "/v1"}
    if trial_token:
        options["headers"] = {TRIAL_HEADER: validate_trial_token(trial_token)}
    return {
        "model": f"benchmark/{model}",
        "small_model": f"benchmark/{model}",
        "enabled_providers": ["benchmark"],
        "share": "disabled",
        "autoupdate": False,
        # Title generation burns benchmark tokens off-trajectory; disable so
        # proxy rows reconcile 1:1 with step_finish events.
        "agent": {"title": {"disable": True}},
        "provider": {
            "benchmark": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Host-owned benchmark proxy",
                "options": options,
                "models": {model: {
                    "name": model,
                    "limit": {"context": context_tokens, "output": output_tokens},
                }},
            },
        },
    }


def guest_command(*, binary: str, model: str, instruction: str) -> list[str]:
    """Return argv only; callers execute exclusively through the guest adapter."""
    if not _MODEL.fullmatch(model):
        raise ValueError("invalid model")
    if binary != "/var/tmp/tofu-opencode/opencode":
        raise ValueError("binary must be the verified guest installation")
    return [binary, "run", "--pure", "--auto", "--format=json",
            "--model", f"benchmark/{model}", "--", instruction]


_CONTINUATION_NUDGE = (
    "Your previous response was cut off at the model output-token limit "
    "before any tool call or final answer. Continue working on the original "
    "task now: prefer concrete tool actions (write the required artifact "
    "files first, then iterate) over extended analysis."
)


def guest_shell_command(
    *,
    binary: str,
    model: str,
    instruction_path: str,
    raw_output_path: str,
    stderr_path: str,
    max_continuations: int = 16,
) -> str:
    """Return the guest shell program running OpenCode to genuine completion.

    OpenCode's provider plugin ends a run as soon as one model response hits
    the output-token cap (``step-finish`` with ``reason=\"length\"``), even
    when that response produced zero visible output and zero tool calls.  The
    benchmark evidence shows whole trials finishing with 13-32% of the agent
    budget unused because of this.  Rather than accept the truncation as
    episode end, re-invoke ``opencode run --continue`` on the same persisted
    session: the CLI resumes exactly where it stopped with a short nudge.

    Termination is fail-closed and auditable:
      * a run whose last event is not a ``length`` step-finish ends the loop
        with the CLI's own exit code (normal completion or tool-driven stop);
      * exhausting the continuation budget exits 42 so the trial is marked
        instead of being projected as a clean finish;
      * a nonzero CLI exit code propagates unchanged.

    All attempts append to one evidence stream, which therefore stays valid
    JSONL (no separator lines).  ``--continue`` requires no new guest
    surface: same binary, same config, same proxy route, same credential
    boundary.
    """
    # The instruction reaches argv through the shell so it never appears in
    # this script's own command line; only the prompt file path does.  Build
    # the canonical argv, substitute a $(cat ...) shell fragment as the final
    # positional, then let shlex.join quote everything EXCEPT that fragment.
    argv = guest_command(binary=binary, model=model, instruction="")
    argv[-1] = "$(cat " + shlex.quote(instruction_path) + ")"
    first = shlex.join(argv[:-1]) + ' "' + argv[-1] + '"'
    nxt = shlex.join([
        binary, "run", "--pure", "--auto", "--format=json",
        "--model", f"benchmark/{model}", "--continue", "--",
        _CONTINUATION_NUDGE,
    ])
    raw = shlex.quote(raw_output_path)
    err = shlex.quote(stderr_path)
    # Detection: the last JSONL event being a step_finish whose part.reason is
    # "length".  OpenCode emits compact JSON (no spaces), but allow optional
    # whitespace for robustness.  POSIX grep (BRE) supports \s via [[:space:]].
    detect = (
        'tail -n 1 ' + raw + ' 2>/dev/null | '
        'grep -q \'"step_finish".*"reason"[[:space:]]*:[[:space:]]*"length"\''
    )
    return (
        "attempt=0\n"
        f"while [ $attempt -le {int(max_continuations)} ]; do\n"
        "  if [ $attempt -eq 0 ]; then\n"
        f"    {first} < /dev/null >> {raw} 2>> {err}\n"
        "  else\n"
        f"    {nxt} < /dev/null >> {raw} 2>> {err}\n"
        "  fi\n"
        "  rc=$?\n"
        "  [ $rc -ne 0 ] && exit $rc\n"
        f"  if {detect}; then\n"
        "    attempt=$((attempt+1))\n"
        "    continue\n"
        "  fi\n"
        "  exit 0\n"
        "done\n"
        "exit 42\n"
    )
