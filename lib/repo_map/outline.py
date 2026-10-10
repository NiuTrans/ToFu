"""lib/repo_map/outline.py — Data model and definition extraction.

A :class:`Tag` is one extracted definition (a function/class/method heading
or a top-level constant assignment). :class:`RepoMap` holds the whole scan
result: every file seen, the tags per file, and the *referenced* identifiers
(for the ranking graph).

The extractor has two backends, picked per file:

* Python files use the stdlib :mod:`ast` module — fast, zero-dependency,
  and the majority language on our evals.
* Everything else goes through ``tree_sitter_language_pack`` via the
  ``grep_ast`` helpers. A per-file failure (grammar gap, odd encoding)
  produces an empty tag list for that file; it never aborts the scan.

Both backends emit the same Tag shape so ranking and rendering are
backend-agnostic.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Iterable

from lib.log import get_logger

logger = get_logger(__name__)

# Extensions we never try to parse as code, even if a tree-sitter grammar
# technically exists for them. These either carry no useful structure for a
# model (data/config), or produce noisy tag streams (markup).
SKIP_EXTENSIONS = frozenset({
    '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.lock', '.csv',
    '.tsv', '.psv', '.xml', '.html', '.css', '.scss', '.less', '.svg',
    '.md', '.rst', '.txt', '.tex', '.bib', '.png', '.jpg', '.jpeg', '.gif',
    '.webp', '.bmp', '.ico', '.pdf', '.zip', '.tar', '.gz', '.whl', '.pyc',
    '.pyo', '.so', '.o', '.a', '.dylib', '.dll', '.exe', '.bin', '.wasm',
    '.mp4', '.mp3', '.wav', '.woff', '.woff2', '.ttf', '.eot', '.map',
})

# Identifiers too generic to be worth counting as cross-file references.
# Mirroring aider's caution: a word like "setup" or "run" occurs in every
# project and would dominate PageRank without adding orientation value.
STOP_WORDS = frozenset({
    'init', 'setup', 'run', 'main', 'test', 'tests', 'self', 'cls', 'args',
    'kwargs', 'data', 'value', 'key', 'item', 'obj', 'config', 'logger',
    'get', 'set', 'new', 'create', 'update', 'delete', 'make', 'build',
    'handle', 'process', 'start', 'stop', 'load', 'save',
    # very common framework / stdlib nouns — they rank noise to the top
    'result', 'results', 'response', 'request', 'context', 'state',
    'manager', 'error', 'index', 'node', 'match', 'params', 'callback',
})

_WORD_RE = re.compile(r'\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b')
_IDENT_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

# Files larger than this are almost always generated bundles or data dumps —
# mapping them would cost seconds of parse time for zero orientation value.
_MAX_SOURCE_BYTES = 512 * 1024

# Reference extraction samples only the head of a file; the identifiers a
# model needs for orientation live in imports and top-level wiring.
_MAX_REF_BYTES = 64 * 1024

# Path fragments that mark a file as machine-generated. These are skipped for
# tags AND references — their 1000+ symbol dumps swamp PageRank and the map.
_GENERATED_MARKERS = (
    'generated', '.min.js', '.min.css', '.bundle.js', 'app-runtime.js',
    '/dist/', '/build/', 'package-lock.json', 'yarn.lock', 'pnpm-lock',
)


def _is_generated(rel_path: str) -> bool:
    lower = rel_path.lower()
    return any(marker in lower for marker in _GENERATED_MARKERS)


@dataclass(frozen=True)
class Tag:
    """One definition extracted from a source file.

    ``line`` is 1-based (matches what read_files / editors display).
    ``signature`` is the verbatim source line, stripped and truncated.
    """
    rel_path: str
    name: str
    line: int
    kind: str          # 'class' | 'def' | 'var'
    signature: str
    indent: int = 0    # 0 = top-level; >0 for methods/nested defs


@dataclass
class RepoMap:
    """Result of scanning one repository root."""
    root: str
    files: list[str] = field(default_factory=list)
    # rel_path → definition tags in that file (ordered by line)
    tags: dict[str, list[Tag]] = field(default_factory=dict)
    # identifier → {rel_path that references it}
    references: dict[str, set[str]] = field(default_factory=dict)
    # identifier → {rel_path where it is defined}
    definitions: dict[str, set[str]] = field(default_factory=dict)

    def add_tag(self, tag: Tag) -> None:
        self.tags.setdefault(tag.rel_path, []).append(tag)
        self.definitions.setdefault(tag.name, set()).add(tag.rel_path)

    def add_references(self, rel_path: str, identifiers: Iterable[str]) -> None:
        for ident in identifiers:
            if ident in STOP_WORDS or not _IDENT_RE.match(ident):
                continue
            self.references.setdefault(ident, set()).add(rel_path)


def _extract_python_tags(rel_path: str, source: str) -> list[Tag]:
    """Fast path for Python using the stdlib ast module."""
    tags: list[Tag] = []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, MemoryError):
        return tags

    # AST columns are UTF-8 byte offsets. Index physical lines once instead
    # of asking get_source_segment to split the entire file for every tag.
    lines = source.split('\n')

    def _signature(node) -> str:
        line = lines[node.lineno - 1].encode('utf-8')
        end = node.end_col_offset if node.end_lineno == node.lineno else None
        segment = line[node.col_offset:end].decode('utf-8')
        head = segment.splitlines()[0] if segment else ''
        return head.strip()[:160]

    # A recursive local visitor closes over itself and retains every result
    # Tag/line until cyclic GC. Explicit iterator frames preserve preorder and
    # indentation while releasing file-local derivations on ordinary refcount.
    pending = [(iter(tree.body), 0)]
    while pending:
        body, indent = pending[-1]
        node = next(body, None)
        if node is None:
            pending.pop()
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = 'class' if isinstance(node, ast.ClassDef) else 'def'
            tags.append(Tag(rel_path, node.name, node.lineno, kind,
                            _signature(node), indent))
            pending.append((iter(node.body), indent + 1))
        elif isinstance(node, ast.Assign) and indent == 0:
            # Top-level public constants only; nested/control-flow admission
            # remains exactly the same as the original body-only traversal.
            for target in node.targets:
                if (isinstance(target, ast.Name)
                        and target.id.isupper() and len(target.id) >= 3
                        and not target.id.startswith('_')):
                    tags.append(Tag(rel_path, target.id, node.lineno,
                                    'var', _signature(node), 0))
    return tags


def _extract_treesitter_tags(rel_path: str, source: str) -> list[Tag]:
    """Extract ordered tags without grep display metadata; failures stay file-local."""
    try:
        from grep_ast.parsers import filename_to_lang
        from grep_ast.tsl import get_parser
    except ImportError:
        logger.warning('[repo_map] grep_ast unavailable — only Python will be mapped')
        return []
    try:
        language = filename_to_lang(rel_path)
        if not language:
            raise ValueError(f'Unknown language for {rel_path}')
        tree = get_parser(language).parse(source.encode('utf8'))
        return _collect_tree_sitter_tags(tree, rel_path, source)
    except Exception as exc:
        logger.debug('[repo_map] tree-sitter extraction failed for %s: %s', rel_path, exc)
        return []


def _collect_tree_sitter_tags(tree, rel_path: str, source: str) -> list[Tag]:
    """Cursor traversal owns only per-file type metadata and accepted tags."""
    lines = source.splitlines()
    cursor = tree.walk()
    tags = []
    depth = 0
    type_metadata = {}

    def metadata(node_type):
        found = type_metadata.get(node_type)
        if found is None:
            kind = ('class' if 'class' in node_type else
                    'def' if any(part in node_type for part in ('function', 'method', 'def')) else '')
            contributes_depth = any(part in node_type for part in ('class', 'function', 'method'))
            found = kind, contributes_depth
            type_metadata[node_type] = found
        return found

    while True:
        node = cursor.node
        kind, contributes_depth = metadata(node.type)
        if kind:
            name_node = node.child_by_field_name('name')
            name = name_node.text.decode('utf8', 'replace') if name_node else ''
            if name and _IDENT_RE.match(name):
                start_line = node.start_point[0]
                signature = lines[min(start_line, len(lines) - 1)].strip()[:160] if lines else ''
                tags.append(Tag(rel_path, name, start_line + 1, kind, signature, depth))
        if cursor.goto_first_child():
            depth += contributes_depth
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                # Canonical TreeContext groups nodes by line, in preorder.
                tags.sort(key=lambda tag: tag.line)
                return tags
            depth -= metadata(cursor.node.type)[1]


_CODE_EXTENSIONS_CACHE: frozenset[str] | None = None


def _code_extensions() -> frozenset[str]:
    """Extensions tree-sitter can parse, minus the SKIP list."""
    global _CODE_EXTENSIONS_CACHE
    if _CODE_EXTENSIONS_CACHE is None:
        try:
            from grep_ast.parsers import PARSERS
            exts = set(PARSERS)
        except Exception as exc:
            # Parser catalog unavailable (broken/missing pack) — map Python only.
            logger.debug('[repo_map] parser catalog unavailable, Python-only: %s', exc)
            exts = {'.py'}
        _CODE_EXTENSIONS_CACHE = frozenset(exts - SKIP_EXTENSIONS)
    return _CODE_EXTENSIONS_CACHE


def _is_code_path(rel_path: str) -> bool:
    _, ext = os.path.splitext(rel_path)
    return ext.lower() in _code_extensions()


def _read_code_source(root: str, rel_path: str) -> tuple[str, bool] | None:
    """Read one admitted source, shared by tag and reference extraction."""
    abs_path = os.path.join(root, rel_path)
    try:
        admitted = os.path.getsize(abs_path) <= _MAX_SOURCE_BYTES
        with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
            return fh.read() if admitted else fh.read(_MAX_REF_BYTES), admitted
    except OSError:
        return None


def _collect_source_tags(rel_path: str, source: str) -> list[Tag]:
    if rel_path.endswith('.py'):
        return _extract_python_tags(rel_path, source)
    return _extract_treesitter_tags(rel_path, source)


def collect_tags(root: str, rel_path: str) -> list[Tag]:
    """Extract definition tags from one file. Never raises."""
    source = _read_code_source(root, rel_path)
    return ([] if source is None or not source[1]
            else _collect_source_tags(rel_path, source[0]))


def extract_identifiers(source: str) -> set[str]:
    """All word-ish identifiers in a blob of source (reference candidates)."""
    return set(_WORD_RE.findall(source))


def list_files(root: str, limit: int = 100_000) -> list[str]:
    """Project files, fastest available source, FUSE-safe.

    Preference order: ``git ls-files`` (tracked + others, so brand-new files
    not yet ``git add``ed are still mapped) → ``rg --files`` (respects
    .gitignore without git) → bounded walk.
    """
    try:
        out = subprocess.run(
            ['git', '-C', root, 'ls-files', '-z',
             '--cached', '--others', '--exclude-standard'],
            capture_output=True, timeout=60, check=False,
        )
        if out.returncode == 0:
            seen: set[str] = set()
            files: list[str] = []
            for raw in out.stdout.split(b'\x00'):
                path = raw.decode('utf8', 'replace')
                if path and path not in seen:
                    seen.add(path)
                    files.append(path)
            if files:
                return sorted(files)
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        out = subprocess.run(
            ['rg', '--files', '-0', '--no-messages'],
            cwd=root, capture_output=True, timeout=60, check=False,
        )
        if out.returncode == 0 and out.stdout:
            files = [p.decode('utf8', 'replace')
                     for p in out.stdout.split(b'\x00') if p]
            if files:
                return sorted(files)
    except (OSError, subprocess.TimeoutExpired):
        pass

    return _walk_files(root, limit)


def _walk_files(root: str, limit: int) -> list[str]:
    """Pruned walk fallback; every directory is skipped by name or depth."""
    from lib.project_mod.config import IGNORE_DIRS
    out: list[str] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == '.' else rel_dir.count(os.sep) + 1
        if depth > 8:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in IGNORE_DIRS and not d.startswith('.')]
        for name in filenames:
            if name.startswith('.'):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
            if len(out) >= limit:
                return sorted(out)
    return sorted(out)


def scan(root: str,
         path: str = '',
         focus: str = '',
         include_refs: bool = True,
         include_tests: bool = False) -> RepoMap:
    """Full scan: file list → per-file tags → reference map.

    ``path`` restricts to a subdirectory; ``focus`` keeps only files whose
    relative path contains the given substring (case-insensitive).
    """
    root = os.path.abspath(root)
    files = list_files(root)
    if path:
        prefix = path.strip('/').rstrip('/') + '/'
        files = [f for f in files
                 if f.startswith(prefix) or f == prefix.rstrip('/')]
    if focus:
        needle = focus.lower()
        files = [f for f in files if needle in f.lower()]

    result = RepoMap(root=root, files=files)
    for rel_path in files:
        if not _is_code_path(rel_path) or _is_generated(rel_path):
            continue
        if not include_tests and _is_test_path(rel_path):
            continue
        source = _read_code_source(root, rel_path)
        if source is None:
            continue
        text, admitted = source
        tags = _collect_source_tags(rel_path, text) if admitted else []
        for tag in tags:
            result.add_tag(tag)
        if include_refs:
            result.add_references(
                rel_path, extract_identifiers(text[:_MAX_REF_BYTES]))


    return result


_TEST_DIR_NAMES = {'tests', 'test', 'spec', 'e2e', 'specs', '__tests__'}


def _is_test_path(rel_path: str) -> bool:
    """True for conventional test file locations, matched on path *parts*.

    Substring matching would misfire on ''/tmp/pytest-0/test_x/…'' absolute
    paths (pytest's tmp_path naming) and on files like ''latest_result.py''.
    """
    parts = rel_path.lower().replace('\\', '/').split('/')
    if any(part in _TEST_DIR_NAMES for part in parts[:-1]):
        return True
    name = parts[-1]
    return (name.startswith(('conftest.py', 'test_', 'spec_'))
            or name.endswith(('_test.py', '_spec.py',
                              '.test.ts', '.spec.ts',
                              '.test.tsx', '.spec.tsx',
                              '.test.js', '.spec.js',
                              '_test.go', '_test.rs')))
