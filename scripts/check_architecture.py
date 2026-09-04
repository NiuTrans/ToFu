#!/usr/bin/env python3
"""Fail when retired architecture or incident-only notation re-enters source.

Inputs: explicit retired paths plus authored Python/JavaScript/TypeScript under
lib/, routes/, and frontend/src/.
Output: a bounded list of violations and a non-zero exit status.
Dependencies: standard library only; generated delivery artifacts are excluded.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "lib", ROOT / "routes", ROOT / "frontend/src")
SOURCE_SUFFIXES = frozenset({".py", ".js", ".ts"})
EXCLUDED_DIRECTORIES = frozenset({"__pycache__", "node_modules"})
GENERATED = frozenset(
    {
        ROOT / "frontend/src/runtime/app-runtime.js",
        ROOT / "frontend/src/api/conversation-sync.generated.ts",
    }
)
FRONTEND_ARCHITECTURE_CONTRACT = (
    ROOT / "contracts/frontend_conversation_architecture_v1.json"
)
RETIRED_PATHS = (
    ROOT / "lib/push.py",
    ROOT / "lib/task_runtime.py",
    ROOT / "routes/turns_v2.py",
    ROOT / "routes/legacy_redirects.py",
    ROOT / "frontend/src/core/attempt-stream.ts",
    # Completed repository codemods do not remain as executable pseudo-tests.
    # Their dry-runs report zero rewrites; Git history retains the implementation.
    ROOT / "tests/_migrate_api_response.py",
    ROOT / "tests/_migrate_http_client.py",
    ROOT / "tests/_migrate_request_parser.py",
    # The one-shot charter migration was completed by dd629593; current
    # charter semantics live in the project-domain contract and tests.
    ROOT / "tests/_migrate_charter_kinds.py",
    # Canonical user-avatar art is static/icons/onigiri.svg. Version words in
    # shipped filenames created three unreferenced copies with no clear owner.
    ROOT / "static/icons/onigiri-new.png",
    ROOT / "static/icons/onigiri-new.svg",
    ROOT / "static/icons/onigiri-old.svg",
    # The README no longer references this ambiguous root-level screenshot
    # bucket; future documentation assets must live with a declared owner.
    ROOT / "propaganda",
)
FORBIDDEN_TEXT = (
    (re.compile(r"\blib\.push\b"), "use the sole PushHub owner lib.agent_core.push"),
    (re.compile(r"from\s+lib\s+import\s+push\b"), "use lib.agent_core.push"),
    (re.compile(r"\blib\.task_runtime\b"), "use lib.agent_core.task_runtime"),
    (re.compile(r"pt_[0-9a-f]{6,}"), "incident/board identifiers belong in Git history"),
    (re.compile("★"), "decorative patch markers obscure ownership"),
    (re.compile(r"/api/v2/conversations"), "conversation v2 HTTP surface is retired"),
    (re.compile(r"/api/chat/stream"), "use task replay SSE or Conversation Sync v3"),
    (re.compile(r"/api/v1/chat/send"), "use Conversation Sync v3 turn commands"),
    (re.compile(r"/api/v1/chat/continue"), "use Conversation Sync v3 attempts"),
    (re.compile(r"\bisTurnAuthorityActive\b"), "conversation authority is unconditional"),
    (re.compile(r"\blegacy-attempt-sse\b"), "use the semantic task-sse transport"),
    (re.compile(r"\bTurnStoreV2\b"), "use ConversationTurnStore"),
    (
        re.compile(
            r"\b(?:_sync_result_to_conversation|_sync_partial_to_conversation|"
            r"_committedMsg|committedMessage|_append_vu_message_to_conv|"
            r"_presync_parent_reply)\b"
        ),
        "messages-array mirror and committed-message transport are retired",
    ),
)


def _relative_contract_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        raise ValueError(f"{field} must be a non-empty repository-relative path")
    parts = Path(value).parts
    if ".." in parts:
        raise ValueError(f"{field} must not escape the repository")
    return ROOT / value


def load_frontend_architecture_contract() -> dict[str, Any]:
    """Load and validate the conversation ownership/debt contract."""
    try:
        document = json.loads(
            FRONTEND_ARCHITECTURE_CONTRACT.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read frontend architecture contract: {exc}") from exc
    if not isinstance(document, dict) or document.get("contract") != (
        "tofu.frontend-conversation-architecture/v1"
    ):
        raise ValueError("unexpected frontend conversation architecture contract")

    owners = document.get("owners")
    if not isinstance(owners, dict) or not owners:
        raise ValueError("frontend architecture contract must declare owners")
    for owner, relative_path in owners.items():
        path = _relative_contract_path(relative_path, field=f"owners.{owner}")
        if not path.exists():
            raise ValueError(f"owners.{owner} does not exist: {relative_path}")

    layers = document.get("targetLayers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("frontend architecture contract must declare targetLayers")
    layer_ids = {
        item.get("id") for item in layers if isinstance(item, dict)
    }
    if None in layer_ids or len(layer_ids) != len(layers):
        raise ValueError("targetLayers ids must be unique non-empty values")
    for item in layers:
        if not isinstance(item, dict):
            raise ValueError("targetLayers entries must be objects")
        _relative_contract_path(item.get("root"), field=f"targetLayers.{item.get('id')}")
        dependencies = item.get("mayDependOn")
        if not isinstance(dependencies, list) or any(
            value not in layer_ids or value == item.get("id")
            for value in dependencies
        ):
            raise ValueError(f"invalid dependencies for layer {item.get('id')}")

    debts = document.get("legacyDebt")
    if not isinstance(debts, list) or not debts:
        raise ValueError("frontend architecture contract must declare legacyDebt")
    debt_ids: set[str] = set()
    for debt in debts:
        if not isinstance(debt, dict):
            raise ValueError("legacyDebt entries must be objects")
        debt_id = debt.get("id")
        if not isinstance(debt_id, str) or not debt_id or debt_id in debt_ids:
            raise ValueError("legacyDebt ids must be unique non-empty strings")
        debt_ids.add(debt_id)
        if debt.get("metric") not in {"file_count", "byte_count", "regex_count"}:
            raise ValueError(f"legacyDebt.{debt_id} has an unsupported metric")
        roots = debt.get("roots")
        if not isinstance(roots, list) or not roots:
            raise ValueError(f"legacyDebt.{debt_id} must declare roots")
        for index, root in enumerate(roots):
            _relative_contract_path(root, field=f"legacyDebt.{debt_id}.roots[{index}]")
        suffixes = debt.get("suffixes")
        if not isinstance(suffixes, list) or not suffixes or any(
            not isinstance(value, str) or not value.startswith(".")
            for value in suffixes
        ):
            raise ValueError(f"legacyDebt.{debt_id} must declare suffixes")
        maximum = debt.get("maximum")
        target = debt.get("target")
        if (
            isinstance(maximum, bool) or not isinstance(maximum, int)
            or isinstance(target, bool) or not isinstance(target, int)
            or target < 0 or maximum < target
        ):
            raise ValueError(f"legacyDebt.{debt_id} has invalid limits")
        if debt.get("metric") == "regex_count":
            try:
                re.compile(str(debt.get("pattern") or ""))
            except re.error as exc:
                raise ValueError(
                    f"legacyDebt.{debt_id} has an invalid pattern: {exc}"
                ) from exc
    return document


def _debt_source_paths(debt: dict[str, Any]) -> list[Path]:
    suffixes = frozenset(debt["suffixes"])
    paths: list[Path] = []
    for relative_root in debt["roots"]:
        root = ROOT / relative_root
        if root.is_file() and root.suffix in suffixes:
            paths.append(root)
            continue
        if root.is_dir():
            paths.extend(
                path for path in root.rglob("*")
                if path.is_file() and path.suffix in suffixes
                and not any(part in EXCLUDED_DIRECTORIES for part in path.parts)
                and path not in GENERATED
            )
    return sorted(set(paths))


def measure_frontend_legacy_debt(debt: dict[str, Any]) -> tuple[int, list[tuple[Path, int]]]:
    """Return an aggregate plus per-file evidence for one debt metric."""
    paths = [path for path in _debt_source_paths(debt) if path.is_file()]
    metric = debt["metric"]
    if metric == "file_count":
        return len(paths), [(path, 1) for path in paths]
    if metric == "byte_count":
        rows = []
        for path in paths:
            try:
                rows.append((path, path.stat().st_size))
            except FileNotFoundError:
                # Concurrent generators use atomic/temp-file lifecycles. A
                # path removed after enumeration is no longer architecture.
                continue
        return sum(value for _, value in rows), rows
    pattern = re.compile(debt["pattern"])
    rows = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        count = len(pattern.findall(source))
        if count:
            rows.append((path, count))
    return sum(value for _, value in rows), rows


def frontend_legacy_debt_violations() -> tuple[list[str], list[str]]:
    """Measure every shrinking legacy metric and return output plus failures."""
    document = load_frontend_architecture_contract()
    output: list[str] = []
    violations: list[str] = []
    for debt in document["legacyDebt"]:
        measured, rows = measure_frontend_legacy_debt(debt)
        maximum = debt["maximum"]
        target = debt["target"]
        output.append(
            f"frontend-debt {debt['id']}: {measured} "
            f"(maximum {maximum}, target {target})"
        )
        if measured <= maximum:
            continue
        largest = sorted(rows, key=lambda item: (-item[1], str(item[0])))[:5]
        evidence = ", ".join(
            f"{path.relative_to(ROOT)}={value}" for path, value in largest
        )
        violations.append(
            f"frontend legacy debt {debt['id']} grew from {maximum} to {measured}; "
            f"{debt['remedy']} Largest owners: {evidence}"
        )
    return output, violations


def authored_sources() -> list[Path]:
    paths: list[Path] = [
        path
        for path in ROOT.iterdir()
        if path.is_file() and path.suffix in SOURCE_SUFFIXES
    ]
    for source_root in SOURCE_ROOTS:
        for directory, names, files in os.walk(source_root):
            names[:] = [
                name for name in names
                if not name.startswith(".") and name not in EXCLUDED_DIRECTORIES
            ]
            base = Path(directory)
            paths.extend(
                path
                for name in files
                if (path := base / name).suffix in SOURCE_SUFFIXES
                and path not in GENERATED
            )
    return sorted(paths)


def main() -> int:
    violations: list[str] = []
    debt_output: list[str] = []
    try:
        debt_output, debt_violations = frontend_legacy_debt_violations()
        violations.extend(debt_violations)
    except ValueError as exc:
        violations.append(str(exc))
    for path in RETIRED_PATHS:
        if path.exists():
            violations.append(f"{path.relative_to(ROOT)}: retired path exists")

    sources = authored_sources()
    checked_sources = 0
    for path in sources:
        try:
            source = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # A concurrently removed temp/generated file is absent from the
            # current architecture. Do not turn a safe unlink into a crash.
            continue
        except OSError as exc:
            violations.append(
                f"{path.relative_to(ROOT)}: cannot read authored source: {exc}"
            )
            continue
        checked_sources += 1
        for line_number, line in enumerate(
            source.splitlines(), start=1
        ):
            for pattern, reason in FORBIDDEN_TEXT:
                match = pattern.search(line)
                if match:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{line_number}: "
                        f"{reason}: {match.group(0)!r}"
                    )
                    break
            if len(violations) >= 100:
                break
        if len(violations) >= 100:
            break

    if violations:
        print("architecture-check: FAILED")
        if debt_output:
            print("\n".join(debt_output))
        print("\n".join(f"- {item}" for item in violations))
        return 1
    print("\n".join(debt_output))
    print(f"architecture-check: OK ({checked_sources} authored files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
