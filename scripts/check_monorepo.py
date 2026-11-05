#!/usr/bin/env python3
"""Validate Tofu's first-party package and dependency boundaries.

This is a read-only release guard. It checks source authority, package names,
version compatibility, retired sibling paths, and dependency direction without
importing application or member code.
"""

from __future__ import annotations

import argparse
import ast
from collections import deque
from collections.abc import Iterator
import json
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


def _sdk_version_constant(path: Path) -> str:
    """Read a pure SDK identity leaf without importing or running client code."""
    source = path.read_text(encoding="utf-8")
    if path.suffix == '.ts':
        # The leaf permits documentation and one literal export, not arbitrary
        # TypeScript whose runtime value cannot be established by this guard.
        source = source.lstrip()
        while source.startswith('/*'):
            end = source.find('*/', 2)
            if end == -1:
                raise ValueError('unterminated SDK version documentation')
            source = source[end + 2:].lstrip()
        match = re.fullmatch(
            r'export\s+const\s+VERSION\s*=\s*'
            r'([\'"])([^\'"\n]+)\1\s*;\s*', source)
        if match:
            return match.group(2)
    else:
        body = ast.parse(source, filename=str(path)).body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                body = body[1:]  # Optional module docstring.
        if len(body) == 1 and isinstance(body[0], ast.Assign):
            assignment = body[0]
            if (len(assignment.targets) == 1
                    and isinstance(assignment.targets[0], ast.Name)
                    and assignment.targets[0].id == '__version__'
                    and isinstance(assignment.value, ast.Constant)
                    and isinstance(assignment.value.value, str)):
                return assignment.value.value
    raise ValueError('expected a single literal SDK version constant')


def _locked_application_version(path: Path) -> str:
    packages = [package for package in _toml(path)['package']
                if package['name'] == 'tofu-agent']
    if len(packages) != 1 or packages[0].get('source') != {'editable': '.'}:
        raise ValueError('expected exactly one tofu-agent package from the repository root')
    return packages[0]['version']


def check_release_identity(root: Path, *, tag: str | None = None) -> list[str]:
    """Validate one source tree's release identity; never prepare or publish it.

    VERSION owns the application and public SDK release. Independent workspace
    packages keep their own versions. Every metadata/runtime/lock consumer is
    checked here so local, frozen-export and CI gates enforce the same boundary.
    """
    try:
        expected = (root / 'VERSION').read_text(encoding='utf-8').strip()
        if not expected or any(character.isspace() for character in expected):
            raise ValueError('expected one nonempty version')
    except (OSError, ValueError) as exc:
        return [f'VERSION: {exc}']

    def read_json(relative: str) -> dict:
        return json.loads((root / relative).read_text(encoding='utf-8'))

    readers = (
        ('pyproject.toml:project.version', lambda: _toml(root / 'pyproject.toml')['project']['version']),
        ('uv.lock:tofu-agent.version', lambda: _locked_application_version(root / 'uv.lock')),
        ('clients/python/pyproject.toml:project.version',
         lambda: _toml(root / 'clients/python/pyproject.toml')['project']['version']),
        ('clients/python/tofu_sdk/_version.py:__version__',
         lambda: _sdk_version_constant(root / 'clients/python/tofu_sdk/_version.py')),
        ('clients/typescript/package.json:version', lambda: read_json('clients/typescript/package.json')['version']),
        ('clients/typescript/package-lock.json:version', lambda: read_json('clients/typescript/package-lock.json')['version']),
        ("clients/typescript/package-lock.json:packages[''].version",
         lambda: read_json('clients/typescript/package-lock.json')['packages']['']['version']),
        ('clients/typescript/src/version.ts:VERSION',
         lambda: _sdk_version_constant(root / 'clients/typescript/src/version.ts')),
    )
    failures = []
    for label, read in readers:
        try:
            actual = read()
        except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
            failures.append(f'{label}: cannot read release identity ({exc})')
            continue
        if actual != expected:
            failures.append(f'{label}={actual!r} differs from VERSION={expected!r}')
    if tag is not None and tag != f'v{expected}':
        failures.append(f'release tag={tag!r} differs from VERSION tag v{expected}')
    return failures


def _import_statements(tree: ast.AST) -> Iterator[ast.Import | ast.ImportFrom]:
    """Visit import-bearing syntax in the same breadth-first order as ast.walk.

    Python expressions cannot contain import statements. Skipping their children
    avoids traversing large call/literal payloads while retaining every statement
    scope, exception handler, and match case. Parsing still validates all syntax.
    """
    pending = deque([tree])
    while pending:
        node = pending.popleft()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif not isinstance(node, ast.expr):
            pending.extend(ast.iter_child_nodes(node))


def _python_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in _import_statements(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


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


def _grep_line_records(output: bytes) -> Iterator[tuple[bytes, bytes, bytes]]:
    """Decode git grep -z -n records; a filename can itself contain newlines."""
    offset = 0
    while offset < len(output):
        name_end = output.index(b"\0", offset)
        number_end = output.index(b"\0", name_end + 1)
        line_end = output.find(b"\n", number_end + 1)
        if line_end < 0:
            line_end = len(output)
        yield (output[offset:name_end], output[name_end + 1:number_end],
               output[number_end + 1:line_end])
        offset = line_end + 1


def _tracked_text_hits(literals: tuple[str, ...]) -> dict[str, list[str]]:
    """Search tracked source once and retain separate diagnostics per literal.

    ``git grep`` scans the worktree copies of tracked files natively; a Python
    walk that resolves, stats and reads every tracked path exceeds this
    guard's budget on a network-mounted checkout.
    """
    suffixes = (
        ".cfg", ".ini", ".java", ".js", ".json", ".kt", ".md", ".mjs",
        ".ps1", ".py", ".sh", ".toml", ".ts", ".txt", ".yaml", ".yml",
    )
    encoded_literals = {literal: literal.encode("utf-8") for literal in literals}
    hits: dict[str, list[str]] = {literal: [] for literal in encoded_literals}
    if not hits:
        return hits
    patterns = [argument for literal in encoded_literals for argument in ("-e", literal)]
    result = subprocess.run(
        ["git", "grep", "-z", "-n", "-I", "-F", "--no-color", *patterns,
         "--", *(f":(icase)*{suffix}" for suffix in suffixes)],
        cwd=ROOT, capture_output=True, check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    own_path = Path(__file__).resolve().relative_to(ROOT.resolve()).as_posix()
    for raw_name, line_number, line in _grep_line_records(result.stdout):
        relative = raw_name.decode("utf-8", "surrogateescape")
        if relative == own_path:
            continue  # The guard necessarily names the paths it rejects.
        try:
            if (ROOT / relative).stat().st_size > 512 * 1024:
                continue
        except OSError:
            continue
        text = line.decode("utf-8", "ignore").strip()
        for literal, encoded in encoded_literals.items():
            if len(hits[literal]) < 5 and encoded in line:
                hits[literal].append(f"{relative}:{int(line_number)}:{text}")
        if all(len(matches) >= 5 for matches in hits.values()):
            break
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
    for node in _import_statements(ast.parse(source)):
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


def _tracked_files_mentioning(literal: str, pathspec: str) -> set[str]:
    result = subprocess.run(
        ["git", "grep", "-l", "-z", "-F", "-e", literal, "--", pathspec],
        cwd=ROOT, capture_output=True, check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return {
        raw_name.decode("utf-8", "surrogateescape")
        for raw_name in result.stdout.split(b"\0")
        if raw_name
    }


def check_declared_scripts(root: Path, tracked: set[str], *,
                           script_mentions: set[str] | None = None) -> list[str]:
    """A clean checkout must contain release entrypoints and local script imports.

    Read declared build/workflow sources and retained test imports.
    Never execute scripts or follow documentation/generated command examples.
    ``script_mentions`` may narrow the retained tests that are read to those
    whose text contains ``scripts``; a test without it imports no script.
    """
    declarations = [root / 'Makefile', root / 'package.json']
    declarations.extend(sorted((root / '.github/workflows').glob('*.yml')))
    declarations.extend(sorted((root / '.github/workflows').glob('*.yaml')))
    pending = {name for name in tracked
               if name.startswith('scripts/') and name.endswith('.py')}
    for name in sorted(tracked):
        if name.startswith('tests/') and name.endswith('.py'):
            if script_mentions is not None and name not in script_mentions:
                continue
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
    # One index snapshot per check; a later invocation must observe new edits.
    tracked_paths = _tracked_paths()
    failures = check_declared_scripts(
        ROOT, {p.as_posix() for p in tracked_paths},
        script_mentions=_tracked_files_mentioning("scripts", "tests/*.py"))
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
    failures.extend(check_release_identity(ROOT))

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

    retired_paths = ("../tofu-search", "../tofu-trade")
    for retired_path, hits in _tracked_text_hits(retired_paths).items():
        if hits:
            failures.append(
                f"retired sibling path {retired_path!r}: " + "; ".join(hits[:5])
            )

    forbidden_search_imports = {"lib", "routes", "tofu", "tofu_agent"}
    search_prefix = Path("packages/tofu-search/tofu_search")
    search_python = (
        ROOT / relative for relative in tracked_paths
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
        ROOT / relative for relative in tracked_paths
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-identity', action='store_true',
                        help='check only release metadata, runtime constants and lockfiles')
    parser.add_argument('--tag', help='also require this exact release tag (vVERSION)')
    args = parser.parse_args(argv)
    if args.tag is not None and not args.release_identity:
        parser.error('--tag requires --release-identity')
    failures = check_release_identity(ROOT, tag=args.tag) if args.release_identity else check()
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    if args.release_identity:
        print('release identity: OK (application, SDKs, runtime constants and lockfiles)')
    else:
        print("monorepo contract: OK (tofu-agent, tofu-search, tofu-trading, tofu-overleaf-mcp)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
