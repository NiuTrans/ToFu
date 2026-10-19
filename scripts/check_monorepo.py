#!/usr/bin/env python3
"""Validate Tofu's first-party package and dependency boundaries.

This is a read-only release guard. It checks source authority, package names,
version compatibility, retired sibling paths, and dependency direction without
importing application or member code.
"""

from __future__ import annotations

import ast
from functools import cache
import re
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEARCH_ROOT = ROOT / "packages" / "tofu-search"
TRADING_ROOT = ROOT / "plugins" / "tofu-trading"
OVERLEAF_ROOT = ROOT / "packages" / "tofu-overleaf-mcp"


def _toml(path: Path) -> dict:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _python_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


@cache
def _tracked_paths() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return tuple(
        Path(raw_name.decode("utf-8", "surrogateescape"))
        for raw_name in result.stdout.split(b"\0")
        if raw_name
    )


def _tracked_text_hits(literal: str) -> list[str]:
    """Search bounded source-like tracked files without inflating Git packs."""
    suffixes = {
        ".cfg", ".ini", ".java", ".js", ".json", ".kt", ".md", ".mjs",
        ".ps1", ".py", ".sh", ".toml", ".ts", ".txt", ".yaml", ".yml",
    }
    hits: list[str] = []
    for relative in _tracked_paths():
        path = ROOT / relative
        if path.resolve() == Path(__file__).resolve():
            continue  # The guard necessarily names the paths it rejects.
        if path.suffix.lower() not in suffixes or not path.is_file():
            continue
        try:
            if path.stat().st_size > 512 * 1024:
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
            ):
                if literal in line:
                    hits.append(f"{relative}:{line_number}:{line.strip()}")
                    if len(hits) >= 5:
                        return hits
        except OSError:
            continue
    return hits


_SCRIPT_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_./-])(scripts/[A-Za-z0-9_./-]+\.(?:py|mjs|cjs|js|sh))\b")
_LOCAL_JS_IMPORT = re.compile(
    r"(?:from\s*|import\s*\(?\s*)['\"](\.[^'\"]+\.(?:mjs|cjs|js))['\"]")


def _script_import_dependencies(root: Path, path: Path) -> set[str]:
    """Read static script imports, including imports owned by retained tests."""
    dependencies: set[str] = set()
    source = path.read_text()
    if 'scripts' not in source:
        return dependencies
    # Parse names without importing modules or executing generators.
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules = ([f'scripts.{alias.name}' for alias in node.names]
                       if node.module == 'scripts' else [node.module or ''])
        else:
            continue
        for module in modules:
            if not module.startswith('scripts.'):
                continue
            dependency = root / module.replace('.', '/')
            if dependency.is_dir():
                initializer = dependency / '__init__.py'
                if initializer.is_file():
                    dependencies.add(initializer.relative_to(root).as_posix())
            else:
                dependencies.add(dependency.with_suffix('.py').relative_to(root).as_posix())
    return dependencies


def check_declared_scripts(root: Path, tracked: set[str]) -> list[str]:
    """A clean checkout must contain release entrypoints and local script imports.

    Read declared build/workflow sources and retained test imports.
    Never execute scripts or follow documentation/generated command examples.
    """
    declarations = [root / 'Makefile', root / 'package.json']
    declarations.extend(sorted((root / '.github/workflows').glob('*.yml')))
    declarations.extend(sorted((root / '.github/workflows').glob('*.yaml')))
    pending = {name for name in tracked
               if name.startswith('scripts/') and name.endswith('.py')}
    for name in sorted(tracked):
        if name.startswith('tests/') and name.endswith('.py'):
            source = root / name
            if source.is_file():
                pending.update(_script_import_dependencies(root, source))
    for source in declarations:
        if not source.is_file():
            continue
        text = '\n'.join(line for line in source.read_text().splitlines()
                         if not line.lstrip().startswith('#'))
        pending.update(_SCRIPT_REFERENCE.findall(text))
    visited, failures = set(), []
    while pending:
        relative = min(pending)
        pending.remove(relative)
        if relative in visited:
            continue
        visited.add(relative)
        path = root / relative
        if not path.is_file():
            failures.append(f'declared build script is missing: {relative}')
            continue
        if relative not in tracked:
            failures.append(f'declared build script is not tracked: {relative}')
        if path.suffix == '.py':
            pending.update(_script_import_dependencies(root, path))
        if path.suffix in {'.js', '.mjs', '.cjs'}:
            for imported in _LOCAL_JS_IMPORT.findall(path.read_text()):
                dependency = (path.parent / imported).resolve()
                if dependency.is_relative_to(root.resolve() / 'scripts'):
                    pending.add(dependency.relative_to(root.resolve()).as_posix())
    return failures


def check() -> list[str]:
    failures = check_declared_scripts(ROOT, {p.as_posix() for p in _tracked_paths()})
    root = _toml(ROOT / "pyproject.toml")
    search = _toml(SEARCH_ROOT / "pyproject.toml")
    trading = _toml(TRADING_ROOT / "pyproject.toml")
    overleaf = _toml(OVERLEAF_ROOT / "pyproject.toml")

    expected_names = {
        ROOT: "tofu-agent",
        SEARCH_ROOT: "tofu-search",
        TRADING_ROOT: "tofu-trading",
        OVERLEAF_ROOT: "tofu-overleaf-mcp",
    }
    documents = {ROOT: root, SEARCH_ROOT: search, TRADING_ROOT: trading, OVERLEAF_ROOT: overleaf}
    for owner, expected in expected_names.items():
        actual = documents[owner].get("project", {}).get("name")
        if actual != expected:
            failures.append(f"{owner}: project.name={actual!r}, expected {expected!r}")
        if owner != ROOT and (owner / ".git").exists():
            failures.append(f"{owner}: nested .git creates a second source authority")

    root_version = root["project"]["version"]
    version_file = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if version_file != root_version:
        failures.append(
            f"VERSION={version_file!r} differs from tofu-agent {root_version!r}"
        )

    members = set(root.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", []))
    required_members = {"packages/tofu-search", "plugins/tofu-trading"}
    missing_members = sorted(required_members - members)
    if missing_members:
        failures.append(f"workspace misses members: {', '.join(missing_members)}")
    excluded = root.get('tool', {}).get('uv', {}).get('workspace', {}).get('exclude', [])
    if 'packages/tofu-overleaf-mcp' not in excluded or 'packages/tofu-overleaf-mcp' in members:
        failures.append('Overleaf MCP must resolve in its isolated process environment')
    for path in (OVERLEAF_ROOT / 'tofu_overleaf_mcp').glob('*.py'):
        forbidden = _python_import_roots(path) & {'lib', 'routes', 'tofu_agent'}
        if forbidden:
            failures.append(f'{path.relative_to(ROOT)} imports host modules {sorted(forbidden)}')

    sources = root.get("tool", {}).get("uv", {}).get("sources", {})
    if sources.get("tofu-search") != {"workspace": True}:
        failures.append("root tofu-search dependency must resolve from workspace")
    trading_sources = trading.get("tool", {}).get("uv", {}).get("sources", {})
    if trading_sources.get("tofu-agent") != {"workspace": True}:
        failures.append("tofu-trading must resolve tofu-agent from workspace")

    root_minor = ".".join(root_version.split(".")[:2])
    trading_host = next(
        (item for item in trading["project"].get("dependencies", [])
         if item.startswith("tofu-agent")),
        "",
    )
    if not trading_host.startswith(f"tofu-agent>={root_minor},"):
        failures.append(
            f"tofu-trading host range {trading_host!r} does not admit {root_minor}"
        )

    for retired_path in ("../tofu-search", "../tofu-trade"):
        hits = _tracked_text_hits(retired_path)
        if hits:
            failures.append(
                f"retired sibling path {retired_path!r}: " + "; ".join(hits[:5])
            )

    forbidden_search_imports = {"lib", "routes", "tofu", "tofu_agent"}
    search_prefix = Path("packages/tofu-search/tofu_search")
    search_python = (
        ROOT / relative for relative in _tracked_paths()
        if relative.suffix == ".py" and relative.is_relative_to(search_prefix)
    )
    for path in search_python:
        found = _python_import_roots(path) & forbidden_search_imports
        if found:
            failures.append(
                f"{path.relative_to(ROOT)} imports host roots {sorted(found)}"
            )

    core_prefixes = (Path("lib"), Path("routes"), Path("tofu"), Path("tofu_agent"))
    core_python = (
        ROOT / relative for relative in _tracked_paths()
        if relative.suffix == ".py"
        and any(relative.is_relative_to(prefix) for prefix in core_prefixes)
    )
    for path in core_python:
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if "tofu_trading" not in source:
            continue
        if "tofu_trading" in _python_import_roots(path):
            failures.append(
                f"{path.relative_to(ROOT)} imports plugin tofu_trading"
            )

    trading_init = (TRADING_ROOT / "tofu_trading" / "__init__.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)", trading_init, re.M)
    if not match or match.group(1) != trading["project"]["version"]:
        failures.append("tofu-trading __version__ differs from member pyproject.toml")

    return failures


def main() -> int:
    failures = check()
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print("monorepo contract: OK (tofu-agent, tofu-search, tofu-trading, tofu-overleaf-mcp)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
