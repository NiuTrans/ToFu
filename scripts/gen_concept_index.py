#!/usr/bin/env python3
"""Generate the cross-layer concept index — one row per change concept.

WHY THIS EXISTS
---------------
A real change is never confined to one directory: "conversation delete" spans
a contract, a route, a sidecar operation, a frontend module, and a focused
test set. The repository already documents each layer separately
(``docs/README.md``'s change table, ``docs/catalog.json``'s contract guards,
the focused tests named inside each domain/authority document), but nothing
JOINED them — so a contributor editing one side discovered the others only
when a distant test broke.

This script derives that join instead of anyone maintaining it:

* **Concepts and owners** come from the change table in ``docs/README.md``
  (the hand-maintained documentation map — the single source).
* **Guard tests** come from ``docs/catalog.json`` (every cataloged contract
  names the test that pins it).
* **Focused tests** are the ``test_*.py`` references inside the documents
  each row links, resolved against the real ``tests/`` tree.

Derivation is STRICT: an owner path that no longer exists, a linked document
that cannot be read, a contract missing from the catalog, or a test reference
that does not resolve to exactly one (or, for globs, at least one) real file
fails the build with an actionable message. A stale-looking index that is
wrong is worse than none, so the generator refuses to render one.

The output doubles as the drift guard: ``--check`` fails when the committed
``docs/CONCEPT_INDEX.md`` does not match what the sources derive now.

USAGE
    python3 scripts/gen_concept_index.py            # write the index
    python3 scripts/gen_concept_index.py --check    # CI: fail if stale
    python3 scripts/gen_concept_index.py --json     # machine consumption
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "docs/README.md"
CATALOG_PATH = ROOT / "docs/catalog.json"
OUTPUT_PATH = ROOT / "docs/CONCEPT_INDEX.md"

INLINE_CODE = re.compile(r"`([^`\n]+)`")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
#: An inline-code span that is exactly one pytest file reference: either a
#: repo-relative path (``tests/test_x.py``) or a bare basename (``test_x.py``),
#: optionally with ``*`` globs. Pytest node ids (``::test_case``) and line
#: suffixes (``:131``) are stripped before matching.
TEST_TOKEN = re.compile(r"^(?:[A-Za-z0-9_./-]+/)?test_[A-Za-z0-9_*]+\.py$")
PATHLIKE_TOKEN = re.compile(
    r"^(?:[A-Za-z0-9_./*-]+/)+[A-Za-z0-9_./*-]*$"
    r"|^[A-Za-z0-9_./*-]+\.(?:py|pyi|ts|tsx|js|mjs|css|scss|html|md|json|"
    r"yaml|yml|toml|sh|sql|svg)$"
)


def _split_table_row(line: str) -> list[str]:
    """Split a Markdown table row on pipes, ignoring pipes inside code spans."""
    cells: list[str] = []
    current: list[str] = []
    in_code = False
    for char in line.strip():
        if char == "`":
            in_code = not in_code
            current.append(char)
        elif char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def parse_change_table(readme_text: str) -> list[tuple[str, str, str]]:
    """Return (concept, owner cell, contract/map cell) per README change row.

    The change table is the one whose header names "Runtime owner"; it is the
    hand-maintained source of the concept list. Structural drift (table gone,
    ragged rows) is an error — the map is an authority document, so the
    generator fails loudly instead of guessing.
    """
    failures: list[str] = []
    rows: list[tuple[str, str, str]] = []
    lines = readme_text.splitlines()
    header_at = None
    for index, line in enumerate(lines):
        if line.lstrip().startswith("|"):
            cells = _split_table_row(line)
            if "Runtime owner" in cells and "Contract / map" in cells:
                header_at = index
                break
    if header_at is None:
        raise ValueError("docs/README.md: change table with a "
                         "'Runtime owner' header was not found")
    # header_at + 1 is the `|---|` separator; data rows follow it.
    for line in lines[header_at + 2:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        cells = _split_table_row(stripped)
        if len(cells) != 3:
            failures.append(
                f"docs/README.md: change-table row does not have 3 cells: "
                f"{stripped[:80]}")
            continue
        concept, owners, contracts = cells
        if not concept:
            failures.append("docs/README.md: change-table row with an empty "
                            "concept cell")
            continue
        rows.append((concept, owners, contracts))
    if failures:
        raise ValueError("; ".join(failures))
    if not rows:
        raise ValueError("docs/README.md: change table has no data rows")
    return rows


def _repo_relative(target: str, *, base: Path) -> str | None:
    """Resolve a Markdown link target to a repo-relative posix path."""
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target = target.split("#", 1)[0].split("?", 1)[0].strip()
    if not target or "://" in target or target.startswith(("mailto:", "data:")):
        return None
    resolved = (base / target).resolve()
    root = ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"link escapes repository: {target}")
    return resolved.relative_to(root).as_posix()


def _cell_paths(cell: str, *, base: Path) -> list[str]:
    """All repository paths named by a table cell (inline code + links)."""
    found: list[str] = []
    # Strip whole Markdown links before scanning inline code: their link TEXT
    # is itself backticked (e.g. [`API_CONTRACT.md`](API_CONTRACT.md)) and
    # would otherwise be misread as a bare path without its docs/ prefix.
    linkless = MARKDOWN_LINK.sub("", cell)
    for span in INLINE_CODE.findall(linkless):
        token = span.strip()
        if PATHLIKE_TOKEN.fullmatch(token):
            found.append(token)
    for raw_target in MARKDOWN_LINK.findall(cell):
        resolved = _repo_relative(raw_target, base=base)
        if resolved:
            found.append(resolved)
    return found


def _classify(paths: list[str]) -> dict[str, list[str]]:
    """Sort repo-relative paths into the layers the index reports."""
    result: dict[str, list[str]] = {
        "owners": [], "contracts": [], "docs": []}
    for path in paths:
        if path.startswith("contracts/"):
            result["contracts"].append(path)
        elif path.endswith(".md"):
            result["docs"].append(path)
        else:
            result["owners"].append(path)
    return result


def _path_exists(relative_path: str) -> bool:
    if "*" in relative_path:
        return next(ROOT.glob(relative_path), None) is not None
    return (ROOT / relative_path).exists()


def _catalog_guards(catalog: dict) -> dict[str, list[str]]:
    """contract path -> sorted guard tests, from docs/catalog.json."""
    guards: dict[str, set[str]] = {}
    for row in catalog.get("contracts", []):
        path = row.get("path") if isinstance(row, dict) else None
        guard = row.get("guard") if isinstance(row, dict) else None
        if isinstance(path, str) and isinstance(guard, str):
            guards.setdefault(path, set()).add(guard)
    return {path: sorted(values) for path, values in guards.items()}


def _test_basename_index() -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        index.setdefault(path.name, []).append(
            path.relative_to(ROOT).as_posix())
    return index


def _normalize_test_span(span: str) -> str | None:
    token = span.strip().split("::", 1)[0]
    token = re.sub(r":\d+$", "", token).strip()
    return token if TEST_TOKEN.fullmatch(token) else None


def _resolve_test_token(
    token: str, basename_index: dict[str, list[str]]
) -> tuple[list[str], str | None]:
    """Resolve one test reference to real files; (paths, error) pair."""
    if "*" in token:
        pattern = token if "/" in token else f"tests/**/{token}"
        matches = sorted(
            p.relative_to(ROOT).as_posix()
            for p in ROOT.glob(pattern)
            if p.is_file() and p.relative_to(ROOT).parts[0] == "tests"
        )
        if not matches:
            return [], f"test glob matches nothing: {token}"
        return matches, None
    if "/" in token:
        if (ROOT / token).is_file():
            return [token], None
        return [], f"test file does not exist: {token}"
    hits = basename_index.get(token, [])
    if len(hits) == 1:
        return hits, None
    if not hits:
        return [], f"test file does not exist: tests/**/{token}"
    return [], (f"test name is ambiguous ({len(hits)} files): {token} -> "
                + ", ".join(hits))


def document_tests(
    doc_text: str, basename_index: dict[str, list[str]]
) -> tuple[list[str], list[str]]:
    """Focused tests named by one document; (resolved paths, failures)."""
    prose = FENCED_CODE.sub("", doc_text)
    resolved: set[str] = set()
    failures: list[str] = []
    for span in INLINE_CODE.findall(prose):
        token = _normalize_test_span(span)
        if token is None:
            continue
        paths, error = _resolve_test_token(token, basename_index)
        if error:
            failures.append(error)
        resolved.update(paths)
    return sorted(resolved), failures


def collect() -> dict:
    """Derive the full index; ``failures`` non-empty means sources are stale."""
    failures: list[str] = []
    readme_text = README_PATH.read_text(encoding="utf-8")
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    guards_by_contract = _catalog_guards(catalog)
    basename_index = _test_basename_index()

    concepts: list[dict] = []
    for concept, owner_cell, contract_cell in parse_change_table(readme_text):
        try:
            paths = _cell_paths(owner_cell, base=README_PATH.parent) + \
                _cell_paths(contract_cell, base=README_PATH.parent)
        except ValueError as exc:
            failures.append(f"{concept}: {exc}")
            continue
        layers = _classify(paths)
        owners = sorted(set(layers["owners"]))
        contracts = sorted(set(layers["contracts"]))
        docs = sorted(set(layers["docs"]))

        for owner in owners:
            if not _path_exists(owner):
                failures.append(f"{concept}: owner path does not exist: {owner}")
        guards: set[str] = set()
        for contract in contracts:
            if not (ROOT / contract).is_file():
                failures.append(f"{concept}: contract does not exist: {contract}")
            contract_guards = guards_by_contract.get(contract)
            if contract_guards is None:
                failures.append(
                    f"{concept}: contract is not cataloged with a guard: "
                    f"{contract} (docs/catalog.json)")
                continue
            for guard in contract_guards:
                if not (ROOT / guard).is_file():
                    failures.append(
                        f"{concept}: guard test does not exist: {guard}")
                guards.add(guard)

        focused: set[str] = set()
        for doc in docs:
            doc_path = ROOT / doc
            if not doc_path.is_file():
                failures.append(f"{concept}: linked document is missing: {doc}")
                continue
            doc_resolved, doc_failures = document_tests(
                doc_path.read_text(encoding="utf-8"), basename_index)
            focused.update(doc_resolved)
            failures.extend(f"{concept}: {doc}: {error}"
                            for error in doc_failures)

        concepts.append({
            "concept": concept,
            "owners": owners,
            "contracts": contracts,
            "guards": sorted(guards),
            "tests": sorted(focused - guards),
            "docs": docs,
        })
    return {"concepts": concepts, "failures": failures}


def _cell_code(paths: list[str]) -> str:
    return ", ".join(f"`{path}`" for path in paths) if paths else "-"


def _cell_doc_links(docs: list[str]) -> str:
    links = []
    for doc in docs:
        if doc.startswith("docs/"):
            # Basenames are unambiguous among cataloged docs.
            links.append(f"[{Path(doc).name}]({doc[len('docs/'):]})")
        else:
            # Outside docs/ (e.g. evaluations/swebench/README.md) a bare
            # basename collides, so show the repo-relative path.
            links.append(f"[{doc}](../{doc})")
    return ", ".join(links) if links else "-"


def render(data: dict) -> str:
    out: list[str] = []
    out.append("# Concept index (GENERATED — do not edit)")
    out.append("")
    out.append("Regenerate with `python3 scripts/gen_concept_index.py`; drift is")
    out.append("pinned by `make docs-check` and")
    out.append("`tests/meta/test_concept_index_generated.py`.")
    out.append("")
    out.append("One row per change concept from [the documentation")
    out.append("map](README.md). The join is DERIVED from that table,")
    out.append("`docs/catalog.json`, and the linked domain/authority documents;")
    out.append("nothing here is hand-maintained. When a change touches an owner")
    out.append("path in a row, the rest of the row is its semantic")
    out.append("neighbourhood: the contracts that pin the wire, the guard tests")
    out.append("that pin each contract, and the focused tests named by the")
    out.append("owning documents. Run the row's tests before and after the")
    out.append("change. If a path or test named here no longer exists, fix the")
    out.append("source document and regenerate — never edit this file.")
    out.append("")
    out.append("| Concept | Runtime owners | Contracts | Guard tests | "
               "Focused tests | Owning documents |")
    out.append("|---|---|---|---|---|---|")
    for row in data["concepts"]:
        out.append(
            f"| {row['concept']} "
            f"| {_cell_code(row['owners'])} "
            f"| {_cell_code(row['contracts'])} "
            f"| {_cell_code(row['guards'])} "
            f"| {_cell_code(row['tests'])} "
            f"| {_cell_doc_links(row['docs'])} |"
        )
    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the committed index is stale")
    parser.add_argument("--json", action="store_true",
                        help="dump the derived index as JSON to stdout")
    args = parser.parse_args()

    try:
        data = collect()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"concept-index: source parse failed: {exc}", file=sys.stderr)
        return 1
    if data["failures"]:
        print("concept-index: stale source references — fix the owning "
              "document, then regenerate:", file=sys.stderr)
        for failure in data["failures"]:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data["concepts"], indent=2, ensure_ascii=False))
        return 0

    rendered = render(data)
    if args.check:
        try:
            current = OUTPUT_PATH.read_text(encoding="utf-8")
        except OSError:
            print(f"MISSING: {OUTPUT_PATH} — run scripts/gen_concept_index.py",
                  file=sys.stderr)
            return 1
        if current != rendered:
            print(f"STALE: {OUTPUT_PATH} does not match the documentation "
                  f"sources. Run: python3 scripts/gen_concept_index.py",
                  file=sys.stderr)
            return 1
        print(f"OK: {OUTPUT_PATH} is in sync "
              f"({len(data['concepts'])} concepts)")
        return 0

    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({len(data['concepts'])} concepts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
