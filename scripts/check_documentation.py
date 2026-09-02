#!/usr/bin/env python3
"""Validate the current-only documentation catalog and local references."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "docs/catalog.json"
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
ROOT_DOC_REFERENCE = re.compile(r"(?<![A-Za-z0-9_./-])(docs/[A-Za-z0-9_./-]+\.md)")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
INLINE_REPOSITORY_PATH = re.compile(
    r"(?:lib|routes|frontend/src|scripts|tests|docs|contracts)/"
    r"[A-Za-z0-9_./*-]+"
    r"(?:/|\.(?:py|pyi|js|mjs|cjs|ts|tsx|css|scss|html|md|json|"
    r"yaml|yml|toml|sh|sql|svg))"
)
FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)


def _catalog_paths(
    catalog: dict,
) -> tuple[set[str], list[str], dict[str, str]]:
    paths: list[str] = []
    groups: dict[str, str] = {}
    for key, rows in catalog.items():
        if key in {"version", "policy", "contracts"}:
            continue
        if not isinstance(rows, list):
            raise ValueError(f"catalog group {key!r} must be a list")
        for row in rows:
            value = row.get("path") if isinstance(row, dict) else row
            if not isinstance(value, str):
                raise ValueError(f"catalog group {key!r} has an invalid row")
            paths.append(value)
            groups[value] = key
            if isinstance(row, dict) and key == "generated":
                generator = row.get("generator")
                if not isinstance(generator, str) or not (ROOT / generator).is_file():
                    raise ValueError(f"generated document {value} has no live generator")
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    return set(paths), duplicates, groups


def _line_limits(catalog: dict, groups: set[str]) -> dict[str, int]:
    policy = catalog.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("catalog policy must be an object")
    limits = policy.get("maximum_lines_by_group")
    if not isinstance(limits, dict):
        raise ValueError("catalog policy has no maximum_lines_by_group object")
    missing = sorted(groups - set(limits))
    if missing:
        raise ValueError(
            "catalog groups have no line limit: " + ", ".join(missing)
        )
    for group, value in limits.items():
        if not isinstance(group, str) or not isinstance(value, int) or value < 1:
            raise ValueError(f"catalog line limit {group!r} must be a positive integer")
    return limits


def _contract_paths(catalog: dict) -> tuple[set[str], list[str]]:
    rows = catalog.get("contracts")
    if not isinstance(rows, list):
        raise ValueError("catalog contracts must be a list")
    paths: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("catalog contract row must be an object")
        path = row.get("path")
        guard = row.get("guard")
        if not isinstance(path, str) or not path.startswith("contracts/"):
            raise ValueError("catalog contract has an invalid path")
        if not isinstance(guard, str) or not (ROOT / guard).is_file():
            raise ValueError(f"contract {path} has no live guard")
        generator = row.get("generator")
        if generator is not None and (
            not isinstance(generator, str) or not (ROOT / generator).is_file()
        ):
            raise ValueError(f"contract {path} has no live generator")
        paths.append(path)
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    return set(paths), duplicates


def _repository_markdown() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "docs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        path for path in result.stdout.splitlines()
        if path.endswith(".md") and (ROOT / path).is_file()
    }


def _repository_contracts() -> set[str]:
    result = subprocess.run(
        [
            "git", "ls-files", "--cached", "--others", "--exclude-standard",
            "--", "contracts",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        path for path in result.stdout.splitlines()
        if Path(path).suffix in {".json", ".yaml", ".yml"}
        and (ROOT / path).is_file()
    }


def _local_link_target(document: Path, raw_target: str) -> Path | None:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target = target.split("#", 1)[0].split("?", 1)[0].strip()
    if not target or "://" in target or target.startswith(("mailto:", "data:")):
        return None
    return (document.parent / unquote(target)).resolve()


def _inline_repository_paths(prose: str) -> set[str]:
    """Return exact repository paths named in inline-code spans."""
    return {
        token.strip()
        for token in INLINE_CODE.findall(prose)
        if INLINE_REPOSITORY_PATH.fullmatch(token.strip())
    }


def _repository_path_exists(relative_path: str) -> bool:
    if "*" in relative_path:
        return next(ROOT.glob(relative_path), None) is not None
    return (ROOT / relative_path).exists()


def main() -> int:
    failures: list[str] = []
    try:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        if catalog.get("version") != 1:
            failures.append("docs/catalog.json has an unsupported version")
        cataloged, duplicates, document_groups = _catalog_paths(catalog)
        line_limits = _line_limits(catalog, set(document_groups.values()))
        contract_catalog, contract_duplicates = _contract_paths(catalog)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"documentation catalog invalid: {exc}", file=sys.stderr)
        return 1

    if duplicates:
        failures.append("catalog duplicates: " + ", ".join(duplicates))
    if contract_duplicates:
        failures.append(
            "contract catalog duplicates: " + ", ".join(contract_duplicates)
        )
    present = _repository_markdown()
    for path in sorted(cataloged - present):
        failures.append(f"cataloged document is missing: {path}")
    for path in sorted(present - cataloged):
        failures.append(f"uncataloged document: {path}")
    present_contracts = _repository_contracts()
    for path in sorted(contract_catalog - present_contracts):
        failures.append(f"cataloged contract is missing: {path}")
    for path in sorted(present_contracts - contract_catalog):
        failures.append(f"uncataloged contract: {path}")

    root_resolved = ROOT.resolve()
    for relative_path in sorted(cataloged & present):
        document = ROOT / relative_path
        source = document.read_text(encoding="utf-8")
        prose = FENCED_CODE.sub("", source)
        line_count = len(source.splitlines())
        group = document_groups[relative_path]
        maximum_lines = line_limits[group]
        if line_count > maximum_lines:
            failures.append(
                f"document exceeds {group} line budget: {relative_path} "
                f"({line_count} > {maximum_lines})"
            )
        if not source.startswith("# "):
            failures.append(f"document has no single H1 entry point: {relative_path}")
        for raw_target in MARKDOWN_LINK.findall(prose):
            target = _local_link_target(document, raw_target)
            if target is None:
                continue
            if root_resolved not in target.parents and target != root_resolved:
                failures.append(f"link escapes repository: {relative_path} -> {raw_target}")
            elif not target.exists():
                failures.append(f"broken local link: {relative_path} -> {raw_target}")
        for referenced in ROOT_DOC_REFERENCE.findall(prose):
            if not (ROOT / referenced).is_file():
                failures.append(
                    f"reference to removed documentation: {relative_path} -> {referenced}"
                )
        for referenced in sorted(_inline_repository_paths(prose)):
            if not _repository_path_exists(referenced):
                failures.append(
                    f"stale inline repository path: {relative_path} -> {referenced}"
                )

    if failures:
        print("documentation-check: FAILED", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(
        "documentation-check: OK "
        f"({len(present)} current documents, {len(present_contracts)} contracts)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
