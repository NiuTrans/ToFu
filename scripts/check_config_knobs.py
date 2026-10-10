#!/usr/bin/env python3
"""Environment-knob registry: every configuration read is declared.

Canonical artifact: ``contracts/config_knobs_v1.json``. The registry exists
because hundreds of ``TOFU_*``/host variables were readable from hundreds of
files with no inventory — an agent could not discover an existing knob and
would mint a near-duplicate. The gate makes the inventory complete (every
literal env read in the tree appears exactly once) and ratchets metadata
quality: knobs enter as ``legacy`` and must migrate to ``documented``
(owner + description) over time; the legacy count may never grow.

Modes:
  (default)   verify the registry matches the tree and metadata rules hold
  --rescan    re-scan the tree, merge into the registry (new knobs enter as
              legacy, vanished knobs drop out) and write it back
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
REGISTRY = os.path.join(ROOT, "contracts", "config_knobs_v1.json")
CONTRACT_ID = "tofu.config-knobs/v1"
STATUSES = ("legacy", "documented", "deprecated")

_ENV_TARGETS = ("environ", "getenv")


def _env_reads(tree: ast.AST) -> set[str]:
    """Literal env reads, including resource and worker-environment adapters."""
    names: set[str] = set()

    def _literal(node: ast.AST) -> str | None:
        return node.value if isinstance(node, ast.Constant) and isinstance(
            node.value, str) else None

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            # os.environ['NAME']
            value = node.value
            if (
                isinstance(value, ast.Attribute) and value.attr == "environ"
                and isinstance(value.value, ast.Name) and value.value.id == "os"
            ):
                name = _literal(node.slice)
                if name:
                    names.add(name)
        elif isinstance(node, ast.Call):
            func = node.func
            is_env_call = (
                (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"get", "setdefault", "pop"}
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "os"
                )
                or (
                    isinstance(func, ast.Attribute)
                    and func.attr == "getenv"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                )
                or (isinstance(func, ast.Name) and func.id == "getenv")
                or (isinstance(func, ast.Name) and func.id == "resolve_resource_budget")
            )
            worker_env_call = isinstance(func, ast.Name) and func.id == 'proc_env_value'
            argument_index = 1 if worker_env_call else 0
            if (is_env_call or worker_env_call) and len(node.args) > argument_index:
                name = _literal(node.args[argument_index])
                if name:
                    names.add(name)
    return names


def scan_tree() -> dict[str, list[str]]:
    """Map every literal env knob to the repository files reading it."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard",
         "--", "*.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    readers: dict[str, list[str]] = {}
    for rel in sorted(out.stdout.split()):
        full = os.path.join(ROOT, rel)
        if not os.path.isfile(full):
            continue  # unstaged deletion in a dirty worktree
        try:
            with open(full, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=rel)
        except SyntaxError:
            continue
        for name in _env_reads(tree):
            readers.setdefault(name, []).append(rel)
    return readers


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate registry key: {key}')
        result[key] = value
    return result


def load_registry() -> dict:
    with open(REGISTRY, encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=_unique_object)


def _write_registry(document: dict) -> None:
    with open(REGISTRY, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, ensure_ascii=False)
        f.write("\n")


def rescan() -> None:
    readers = scan_tree()
    try:
        document = load_registry()
        knobs = document["knobs"]
    except FileNotFoundError:
        document = {
            "$comment": (
                "Environment configuration knob registry. Canonical source "
                "of the knob inventory; metadata is curated, membership is "
                "machine-verified by scripts/check_config_knobs.py."
            ),
            "contract": CONTRACT_ID,
            "knobs": {},
        }
        knobs = document["knobs"]
    for name in readers:
        entry = knobs.setdefault(name, {})
        entry.setdefault("status", "legacy")
        entry.setdefault("owner", "")
        entry.setdefault("description", "")
    for name in list(knobs):
        if name not in readers:
            del knobs[name]
    document["legacy_count"] = sum(
        1 for entry in knobs.values() if entry.get("status") == "legacy")
    _write_registry(document)
    print(
        f"registry now tracks {len(knobs)} knobs "
        f"({document['legacy_count']} legacy)"
    )


def check() -> list[str]:
    problems: list[str] = []
    try:
        document = load_registry()
    except FileNotFoundError:
        return [
            "contracts/config_knobs_v1.json is missing; bootstrap it with "
            "`python3 scripts/check_config_knobs.py --rescan`"
        ]
    if document.get("contract") != CONTRACT_ID:
        problems.append(f"unexpected contract id: {document.get('contract')}")
    knobs = document.get("knobs") or {}
    readers = scan_tree()

    unregistered = sorted(set(readers) - set(knobs))
    vanished = sorted(set(knobs) - set(readers))
    if unregistered:
        problems.append(
            f"{len(unregistered)} env knob(s) read but not registered "
            f"(first: {unregistered[0]}); register them with "
            "`python3 scripts/check_config_knobs.py --rescan` and document "
            "owner + description"
        )
    if vanished:
        problems.append(
            f"{len(vanished)} registered knob(s) no longer read anywhere "
            f"(first: {vanished[0]}); drop them with --rescan"
        )

    for name, entry in sorted(knobs.items()):
        status = entry.get("status")
        if status not in STATUSES:
            problems.append(f"[{name}] invalid status {status!r}")
        if status == "documented" and not (
            entry.get("owner") and entry.get("description")
        ):
            problems.append(
                f"[{name}] documented knobs require owner and description"
            )

    legacy_now = sum(
        1 for entry in knobs.values() if entry.get("status") == "legacy")
    legacy_cap = int(document.get("legacy_count") or 0)
    if legacy_now > legacy_cap:
        problems.append(
            f"legacy knob count grew {legacy_cap} -> {legacy_now}; new knobs "
            "must enter as documented (owner + description), not legacy"
        )
    elif legacy_now < legacy_cap:
        problems.append(
            f"legacy knob count SHRANK {legacy_cap} -> {legacy_now}; lock in "
            "the improvement with `python3 scripts/check_config_knobs.py "
            "--rescan`"
        )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rescan", action="store_true")
    args = parser.parse_args()
    if args.rescan:
        rescan()
        return 0
    problems = check()
    if problems:
        print("config-knobs: FAILED")
        print("\n".join(f"- {problem}" for problem in problems))
        return 1
    print("config-knobs: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
