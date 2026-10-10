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
import ast
import subprocess
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

_ROUTE_LLM_DISPATCH_IMPORT_NAMES = frozenset({
    "astream",
    "async_dispatch_stream",
    "chat",
    "dispatch_chat",
    "dispatch_stream",
    "pick_key_for_model",
    "smart_chat",
    "stream_chat",
})
_ROUTE_LLM_READ_ONLY_IMPORT_NAMES = frozenset({
    "get_dispatcher",
    "reset_dispatcher",
})

# ── Import-direction gates ────────────────────────────────────────────────
# The storage sidecar is a separate process: it may depend on schemas, backend
# adapters and dependency-light contract modules, never on the task-execution
# graph or the HTTP delivery layer.
_SIDECAR_ROOT = (ROOT / "lib" / "storage_sidecar").resolve()
_SIDECAR_FORBIDDEN_IMPORTS = (
    "lib.tasks_pkg",
    "lib.agent_core",
    "lib.orchestration",
    "routes",
)

# Sidecar-shared contract modules: dependency-light logic that BOTH the
# task-execution graph and the sidecar process consume. Each lives in its
# current package for historical import compatibility, but is a cross-process
# contract — its own imports may only point at the standard library or other
# modules in this set, so the shared closure can never grow heavy.
_SIDECAR_SHARED_MODULES = frozenset({
    "lib.event_envelope",
    "lib.turn_trace_contract",
    "lib.tool_round_identity",
    "lib.agent_core.output_settlement",
    "lib.orchestration.contract_schema",
    "lib.orchestration.definition_contract_registry",
    "lib.orchestration.definition_write_field_registry",
    "lib.orchestration.message_compat",
    "lib.orchestration.run_status",
    "lib.orchestration.visible_identity",
    "lib.orchestration.wire_formats",
})
_SIDECAR_SHARED_PATHS = frozenset(
    (ROOT / (name.replace(".", "/") + ".py")).resolve()
    for name in _SIDECAR_SHARED_MODULES
)

# Composition roots wire routes into the app; they are the ONE place a lib
# module may touch the delivery layer.
_COMPOSITION_ROOTS = frozenset({(ROOT / "lib" / "app_assembly.py").resolve()})

# Grandfathered lib → routes imports. Dependency inversion (registration
# callbacks or ports) is the fix; this set may only shrink.
_LIB_ROUTES_IMPORT_DEBT = frozenset({
    (ROOT / "lib" / "auto_restart.py").resolve(),
    (ROOT / "lib" / "search_bridge.py").resolve(),
    (ROOT / "lib" / "server_background_services.py").resolve(),
    (ROOT / "lib" / "server_shutdown.py").resolve(),
})

# Replacing a module object's class defeats static symbol navigation and
# hides import cost from local reading; explicit re-export facades are the
# supported pattern. The one legacy facade is frozen here.
_MODULE_CLASS_REPLACEMENT_ALLOWED = frozenset({
    (ROOT / "lib" / "llm_dispatch" / "api.py").resolve(),
})

# lib/__init__.py runs on EVERY lib.* import and historically accumulated
# import-time config reads (a second, implicit configuration system). It may
# only shrink; when it does, tighten these constants in the same change.
_LIB_INIT = ROOT / "lib" / "__init__.py"
_LIB_INIT_MAX_LINES = 424
_LIB_INIT_MAX_ENV_READS = 17

# Loose modules parked directly at lib/ root bypass the domain-package
# routing table and turn the root into a junk drawer. The count may only
# shrink — new modules belong in (or as) a routed domain package. (The one
# sanctioned exception class is the sidecar-shared contract set above:
# lib/event_envelope.py joined it 2026-09-18 when the event-format stamp was
# wired into the sidecar write authority.)
_LIB_TOPLEVEL_MAX_FILES = 194


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _imported_modules(tree: ast.AST):
    """Yield (lineno, dotted module) for every import, including function-level."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, str(node.module or "")


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def import_direction_violations(
    paths: list[Path],
    *,
    lib_root: Path | None = None,
    routes_root: Path | None = None,
    sidecar_root: Path | None = None,
    composition_roots: frozenset[Path] | None = None,
    lib_routes_debt: frozenset[Path] | None = None,
    shared_modules: frozenset[str] | None = None,
    shared_paths: frozenset[Path] | None = None,
) -> list[str]:
    """Enforce the layer import direction lib ← routes, sidecar ↛ execution."""
    violations: list[str] = []
    lib_root = (lib_root or ROOT / "lib").resolve()
    routes_root = (routes_root or ROOT / "routes").resolve()
    sidecar_root = (sidecar_root or _SIDECAR_ROOT).resolve()
    composition_roots = composition_roots if composition_roots is not None \
        else _COMPOSITION_ROOTS
    lib_routes_debt = lib_routes_debt if lib_routes_debt is not None \
        else _LIB_ROUTES_IMPORT_DEBT
    shared_modules = shared_modules if shared_modules is not None \
        else _SIDECAR_SHARED_MODULES
    shared_paths = shared_paths if shared_paths is not None \
        else _SIDECAR_SHARED_PATHS
    for path in paths:
        if path.suffix != ".py":
            continue
        resolved = path.resolve()
        in_sidecar = _under(resolved, sidecar_root)
        in_lib = _under(resolved, lib_root)
        in_routes = _under(resolved, routes_root)
        if not (in_sidecar or in_lib or in_routes):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
        except (FileNotFoundError, SyntaxError, OSError):
            continue
        for lineno, module in _imported_modules(tree):
            if in_sidecar:
                offender = next(
                    (prefix for prefix in _SIDECAR_FORBIDDEN_IMPORTS
                     if _matches(module, prefix)
                     and module not in shared_modules),
                    None,
                )
                if offender:
                    violations.append(
                        f"{path.name}:{lineno}: the storage sidecar is a "
                        f"separate process and cannot import {offender} "
                        f"({module}); share pure logic through a "
                        "dependency-light contract module"
                    )
                continue
            if in_lib:
                if resolved in shared_paths:
                    if (
                        _matches(module, "lib") or _matches(module, "routes")
                    ) and module not in shared_modules:
                        violations.append(
                            f"{path.name}:{lineno}: sidecar-shared contract "
                            f"modules may only import the standard library "
                            f"or other shared modules ({module})"
                        )
                    continue
                if _matches(module, "routes") and resolved not in (
                    composition_roots | lib_routes_debt
                ):
                    violations.append(
                        f"{path.name}:{lineno}: backend libraries never "
                        f"import routes ({module}); invert the dependency "
                        "through a registration callback or port"
                    )
            elif in_routes and _matches(module, "lib.storage_sidecar"):
                violations.append(
                    f"{path.name}:{lineno}: HTTP routes reach storage "
                    f"through the client layer, not sidecar internals "
                    f"({module})"
                )
    return violations


def module_facade_mutation_violations(
    paths: list[Path],
    *,
    allowed: frozenset[Path] | None = None,
) -> list[str]:
    """Forbid new ``sys.modules[...].__class__ = ...`` module replacements."""
    allowed = allowed if allowed is not None \
        else _MODULE_CLASS_REPLACEMENT_ALLOWED
    violations: list[str] = []
    for path in paths:
        if path.suffix != ".py" or path.resolve() in allowed:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
        except (FileNotFoundError, SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "__class__"
                    and isinstance(target.value, ast.Subscript)
                    and isinstance(target.value.value, ast.Attribute)
                    and target.value.value.attr == "modules"
                    and isinstance(target.value.value.value, ast.Name)
                    and target.value.value.value.id == "sys"
                ):
                    violations.append(
                        f"{path.name}:{node.lineno}: module-object class "
                        "replacement defeats static navigation; use an "
                        "explicit re-export facade"
                    )
    return violations


def lib_toplevel_ratchet_violations(*, maximum: int = _LIB_TOPLEVEL_MAX_FILES) -> list[str]:
    """One-way ratchet on loose modules parked directly at lib/ root."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "lib/*.py"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, 'stderr', '') or str(exc)
        if 'not a git repository' in detail.lower():
            # Isolated harness trees (unit tests, scratch exports) have no
            # git basis — the count check only means anything on a real
            # checkout, so it skips there instead of manufacturing a
            # violation out of the harness.
            return []
        return [f"cannot enumerate lib/ top-level modules: {exc}"]
    count = sum(1 for rel in out.stdout.split() if rel.count("/") == 1)
    if count > maximum:
        return [
            f"lib/ top-level modules grew {maximum} -> {count}; new modules "
            "belong in (or as) a routed domain package — see lib/AGENTS.md"
        ]
    if count < maximum:
        return [
            f"lib/ top-level modules SHRANK {maximum} -> {count} — tighten "
            "_LIB_TOPLEVEL_MAX_FILES in scripts/check_architecture.py in "
            "the same change"
        ]
    return []


def lib_init_ratchet_violations(
    *,
    init_path: Path | None = None,
    max_lines: int = _LIB_INIT_MAX_LINES,
    max_env_reads: int = _LIB_INIT_MAX_ENV_READS,
) -> list[str]:
    """One-way ratchet on lib/__init__.py import-time side effects."""
    init_path = init_path or _LIB_INIT
    try:
        source = init_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read lib/__init__.py: {exc}"]
    lines = len(source.splitlines())
    env_reads = len(re.findall(
        r"os\.environ|os\.getenv|\bgetenv\(", source))
    violations: list[str] = []
    if lines > max_lines or env_reads > max_env_reads:
        violations.append(
            f"lib/__init__.py side-effect debt grew "
            f"(lines {lines}/{max_lines}, env reads "
            f"{env_reads}/{max_env_reads}); configuration belongs in the "
            "declared config owners, not import time"
        )
    elif lines < max_lines or env_reads < max_env_reads:
        violations.append(
            f"lib/__init__.py side-effect debt SHRANK "
            f"(lines {lines}/{max_lines}, env reads "
            f"{env_reads}/{max_env_reads}) — tighten "
            "_LIB_INIT_MAX_LINES/_LIB_INIT_MAX_ENV_READS in "
            "scripts/check_architecture.py in the same change"
        )
    return violations



def route_llm_bypass_violations(
    paths: list[Path],
    *,
    routes_root: Path | None = None,
) -> list[str]:
    """Forbid provider execution in HTTP adapters.

    Routes may reset/read dispatcher state and call the provider-discovery
    service. Model execution, provider pins, and slot reservation belong to a
    task/application execution owner with the shared lifecycle contract.
    """
    violations: list[str] = []
    routes_root = (routes_root or (ROOT / "routes")).resolve()
    for path in paths:
        try:
            path.resolve().relative_to(routes_root)
        except ValueError:
            continue
        if path.suffix != ".py":
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (FileNotFoundError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
                imported = {alias.name for alias in node.names}
                if module == "lib.llm_dispatch":
                    forbidden = imported - _ROUTE_LLM_READ_ONLY_IMPORT_NAMES
                    if forbidden:
                        violations.append(
                            f"{path.name}:{node.lineno}: HTTP routes "
                            "must call a task/application execution owner, not "
                            f"LLM dispatch directly: {sorted(forbidden)}"
                        )
                elif module.startswith("lib.llm_dispatch.") \
                        and not module.startswith("lib.llm_dispatch.discovery"):
                    violations.append(
                        f"{path.name}:{node.lineno}: HTTP routes "
                        f"cannot import dispatcher internals: {module}"
                    )
                elif module == "lib.llm" and imported & _ROUTE_LLM_DISPATCH_IMPORT_NAMES:
                    violations.append(
                        f"{path.name}:{node.lineno}: HTTP routes "
                        "cannot invoke provider transport directly"
                    )
            elif isinstance(node, ast.Attribute) and node.attr in {
                "pick_and_reserve", "pick_key_for_model",
            }:
                violations.append(
                    f"{path.name}:{node.lineno}: HTTP routes "
                    f"cannot reserve provider slots directly: {node.attr}"
                )
    return violations


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
    violations.extend(route_llm_bypass_violations(sources))
    violations.extend(import_direction_violations(sources))
    violations.extend(module_facade_mutation_violations(sources))
    violations.extend(lib_init_ratchet_violations())
    violations.extend(lib_toplevel_ratchet_violations())
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
