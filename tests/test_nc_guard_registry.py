"""Self-enforcing meta-guard for the NC source-restore belt.

WHY THIS EXISTS
---------------
``tests/conftest.py`` keeps ``_NC_GUARDED_SOURCES`` — a list of shipped source
files that a negative-control (NC) test byte-patches IN PLACE on disk (write a
neutered variant → run → restore in a ``finally``). The autouse
``_restore_nc_patched_sources`` fixture snapshots those files and heals any that
a crashed/interrupted test left dirty, so one aborted NC can't poison the whole
session.

The fragility that bit us: that list was HAND-MAINTAINED with no binding to
reality. An earlier pixel-diff test byte-patched a shipped frontend source in
place without registering it, so a SIGKILL of its node subprocess could leave
the source neutered with nothing to heal it. That test now uses a temporary
Vite runtime-section copy, but the incident still proves the registry must
police ITSELF for every remaining in-place writer.

THIS TEST closes that gap the same way ``test_db_guard.py`` polices the
standalone-runner guard: it AST-scans every ``tests/*.py`` for an IN-PLACE write
to a shipped-source path (a repo-relative ``open(...,'w')`` / ``write_text`` /
``os.replace`` / ``shutil.copy*`` whose target resolves under ``lib/``,
``routes/`` or ``static/`` and is NOT a ``tmp_path`` / ``mkdtemp`` tree), and
asserts every such target is present in ``_NC_GUARDED_SOURCES``. A newcomer that
forgets to register turns from a silent suite-poisoner into an immediate red
test that NAMES the culprit file + the missing registry entry.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_nc_guard_registry.py -p no:cacheprovider
"""
from __future__ import annotations

import ast
import glob
import os

import pytest

pytestmark = pytest.mark.unit

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)

# Shipped-source top-level dirs. An in-place write whose resolved target lands
# under one of these is a shipped-source mutation that MUST be on the belt.
_SHIPPED_PREFIXES = ('lib/', 'routes/', 'static/')

# Path-expression tokens that mark the target as a THROWAWAY temp / sibling copy
# (safe — a crash leaves litter, not a poisoned shipped file). If ANY of these
# substrings appears in the write-target expression source, the write is exempt.
_TEMP_TARGET_TOKENS = (
    'tmp_path', 'tmpdir', 'tmp_dir', 'mkdtemp', 'mktemp', 'TemporaryDirectory',
    'NamedTemporaryFile', 'gettempdir', '/tmp', 'tempfile',
    # fixed-name sibling copies / generated harnesses written NEXT TO a source
    # but never OVER it — they carry a distinguishing suffix:
    '.nc_copy', '_neuter', 'neutered', '_harness', 'harness.js', '.longpress.',
)

# Write sinks we recognise, by AST shape.
_MODE_WRITE_FUNCS = {'open'}                    # open(path, 'w' | 'wb' | ...)
_METHOD_WRITE = {'write_text', 'write_bytes'}   # Path(...).write_text(...)
_MODULE_WRITE = {                               # os.replace / shutil.copy*
    ('os', 'replace'), ('os', 'rename'),
    ('shutil', 'copy'), ('shutil', 'copy2'), ('shutil', 'copyfile'),
    ('shutil', 'move'),
}


def _load_guarded_registry() -> tuple:
    """Import the registry straight from conftest (single source of truth)."""
    import importlib
    conftest = importlib.import_module('tests.conftest')
    return tuple(conftest._NC_GUARDED_SOURCES)


def _seg(src: str, node) -> str:
    """Best-effort source text of an AST node (Python 3.8+)."""
    try:
        s = ast.get_source_segment(src, node)
    except Exception:
        s = None
    return s or ''


def _is_write_mode(arg_node, src: str) -> bool:
    """True if an open() mode argument denotes a WRITE ('w'/'a'/'x', text/bin)."""
    seg = _seg(src, arg_node).strip().strip('"\'')
    return bool(seg) and seg[0] in ('w', 'a', 'x')


def _write_target_expr(node: ast.AST, src: str):
    """If *node* is an in-place write CALL, return the source text of its TARGET
    path expression; else None.

      open(TARGET, 'w')            → TARGET
      TARGET.write_text(...)       → TARGET  (receiver)
      os.replace(SRC, TARGET)      → TARGET  (dest)
      shutil.copy(SRC, TARGET)     → TARGET  (dest)
    """
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    # open(path, 'w'...) — mode may be positional[1] or kw 'mode'
    if isinstance(fn, ast.Name) and fn.id in _MODE_WRITE_FUNCS:
        if not node.args:
            return None
        mode_node = node.args[1] if len(node.args) > 1 else None
        for kw in node.keywords:
            if kw.arg == 'mode':
                mode_node = kw.value
        if mode_node is not None and _is_write_mode(mode_node, src):
            return _seg(src, node.args[0])
        return None
    # RECEIVER.write_text(...) / .write_bytes(...)
    if isinstance(fn, ast.Attribute) and fn.attr in _METHOD_WRITE:
        return _seg(src, fn.value)
    # os.replace / shutil.copy*(SRC, DEST)  → DEST is target
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        key = (fn.value.id, fn.attr)
        if key in _MODULE_WRITE and len(node.args) >= 2:
            return _seg(src, node.args[1])
    return None


def _resolve_shipped_relpath(expr: str, module_consts: dict):
    """Resolve a write-target path EXPRESSION to a repo-relative shipped path,
    or None if it isn't (statically) a shipped-source file.

    Handles the two shapes real NC writers use:
      * a module-level constant  (SRC_PATH / _PEER_SRC / _SRC) whose value we
        pre-resolved from its ``os.path.join(ROOT, 'lib', ...)`` /
        ``ROOT / 'static' / ...`` definition;
      * an inline ``os.path.join(ROOT, 'lib', 'x.py')`` / ``ROOT / 'lib' / ...``.
    Anything that mentions a temp token is treated as non-shipped (None)."""
    if not expr:
        return None
    if any(tok in expr for tok in _TEMP_TARGET_TOKENS):
        return None
    # (1) bare constant name → its pre-resolved relpath
    name = expr.strip()
    if name in module_consts:
        return module_consts[name]
    # (2) inline path-join / pathlib expression → pull the shipped tail
    rel = _relpath_from_join_expr(expr)
    return rel


def _relpath_from_join_expr(expr: str):
    """Extract a repo-relative shipped path from an ``os.path.join(ROOT, 'a',
    'b.py')`` or ``ROOT / 'a' / 'b.py'`` expression string. Returns e.g.
    'lib/foo/bar.py' when the joined segments start at a shipped prefix, else
    None. Purely lexical (no eval) — robust enough for the constant shapes the
    NC tests use."""
    import re
    # collect quoted segments in order
    segs = re.findall(r"""['"]([^'"]+)['"]""", expr)
    if not segs:
        return None
    # find the first segment that begins a shipped tree
    for i, s in enumerate(segs):
        if s in ('lib', 'routes', 'static'):
            tail = segs[i:]
            rel = '/'.join(tail)
            if rel.endswith(('.py', '.js', '.css', '.html')):
                return rel
    # a single segment already like 'lib/foo/bar.py'
    for s in segs:
        if s.startswith(_SHIPPED_PREFIXES) and s.endswith(
                ('.py', '.js', '.css', '.html')):
            return s
    return None


def _module_path_constants(tree: ast.Module, src: str) -> dict:
    """Map module-level NAME → resolved shipped relpath for assignments whose
    RHS is an ``os.path.join(ROOT, 'lib', ...)`` / ``ROOT / 'static' / ...``
    that resolves under a shipped prefix. These are the ``SRC_PATH`` / ``_SRC``
    / ``_PEER_SRC`` constants the writers dereference."""
    consts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        rhs = _seg(src, node.value)
        rel = _relpath_from_join_expr(rhs)
        if not rel:
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                consts[tgt.id] = rel
    return consts


def _local_inplace_writer_helpers(tree: ast.Module, src: str) -> set:
    """Return the names of locally-defined functions whose body does an in-place
    write to their FIRST parameter (e.g. ``def _patch_restore(path, ...): ...
    open(path, 'w')`` / ``def _neuter(...): open(_TARGET, 'w')``).

    This catches the legacy on-disk NC INDIRECTION (test_project_peer.py passes
    the shipped-path constant to ``_patch_restore(_PEER_SRC, ...)`` and the
    actual ``open(path,'w')`` lives inside the helper on the parameter). We
    return the helper names so the caller can resolve the CONSTANT passed at
    each call site. Note: the SAFE in-memory ``patch_restore`` imported from
    tests/_nc_harness.py never writes disk, so a file that only imports it (no
    local disk-writing def) yields nothing here."""
    helpers = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or not fn.args.args:
            continue
        first_param = fn.args.args[0].arg
        for sub in ast.walk(fn):
            expr = _write_target_expr(sub, src)
            if expr and expr.strip() == first_param:
                helpers.add(fn.name)
                break
    return helpers


def _scan_file_for_inplace_shipped_writes(path: str):
    """Return a set of repo-relative shipped-source paths that *path* writes IN
    PLACE (mode-write / write_text / replace / copy dest, OR via a local helper
    that writes its path parameter in place), excluding temp/copy targets."""
    try:
        src = open(path, encoding='utf-8').read()
    except OSError:
        return set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    consts = _module_path_constants(tree, src)
    helpers = _local_inplace_writer_helpers(tree, src)
    hits = set()

    def _consider(expr):
        rel = _resolve_shipped_relpath(expr, consts)
        # Only count it if the file actually exists (a fabricated fake tree like
        # _make_served_tree writes 'lib/llm/cache.py' under a mkdtemp root —
        # already excluded by the temp-token check, but double-guard on real
        # existence so synthetic layouts aren't flagged).
        if rel and rel.startswith(_SHIPPED_PREFIXES) and \
                os.path.isfile(os.path.join(_REPO_ROOT, rel)):
            hits.add(rel)

    for node in ast.walk(tree):
        # (a) direct in-place write
        expr = _write_target_expr(node, src)
        if expr:
            _consider(expr)
            continue
        # (b) indirection: call to a local helper that writes its first param
        #     in place → resolve the FIRST positional arg constant at this site.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in helpers and node.args:
            _consider(_seg(src, node.args[0]))
    return hits


def _discover_inplace_shipped_writers() -> dict:
    """{test_file_relpath: {shipped_relpath, ...}} for every tests/*.py that
    writes a real shipped source in place."""
    found = {}
    for p in glob.glob(os.path.join(_TESTS_DIR, '*.py')):
        if os.path.basename(p) == os.path.basename(__file__):
            continue  # don't scan ourselves
        hits = _scan_file_for_inplace_shipped_writes(p)
        if hits:
            found[os.path.relpath(p, _REPO_ROOT)] = hits
    return found


def test_every_inplace_shipped_source_writer_is_registered():
    """SELF-ENFORCING REGISTRY: every tests/*.py that byte-patches a shipped
    source file IN PLACE must have that file in conftest._NC_GUARDED_SOURCES, so
    a crashed patch is always healed by the autouse belt. A new writer that
    forgets to register fails HERE, named, instead of silently poisoning the
    tree on the next aborted run."""
    guarded = set(_load_guarded_registry())
    writers = _discover_inplace_shipped_writers()

    # Sanity: the scanner must find at least one currently registered writer,
    # else the AST heuristic silently regressed and this test passes vacuously.
    all_targets = {t for hits in writers.values() for t in hits}
    assert 'static/styles.css' in all_targets, (
        'scanner did not detect the retained stylesheet writers — heuristic '
        'regressed. '
        f'Detected: {writers}')

    unregistered = []
    for test_file, targets in sorted(writers.items()):
        for tgt in sorted(targets):
            if tgt not in guarded:
                unregistered.append(f'{test_file} → writes {tgt}')
    assert not unregistered, (
        'These test files write a SHIPPED SOURCE file IN PLACE but the target '
        'is NOT in conftest._NC_GUARDED_SOURCES — a SIGKILL/timeout mid-patch '
        'would leave the shipped tree poisoned with no belt to heal it. Add '
        'each target to _NC_GUARDED_SOURCES (or write to a tmp_path copy '
        'instead):\n  ' + '\n  '.join(unregistered))


def test_scanner_detects_a_synthetic_unregistered_writer(tmp_path):
    """NEGATIVE CONTROL: prove the scanner actually BITES on a brand-new
    unregistered in-place shipped-source writer (not a vacuous pass). Synthesize
    a test file that writes a real shipped path via a module constant and assert
    the scanner flags it and the registry check would fail."""
    synthetic = tmp_path / 'test_synthetic_inplace_writer.py'
    # Writes a REAL shipped file (routes/common.py exists) in place, via a
    # module-level os.path.join constant + open(...,'w') — the exact shape.
    synthetic.write_text(
        "import os\n"
        "ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
        "_VICTIM = os.path.join(ROOT, 'routes', 'common.py')\n"
        "def test_thing():\n"
        "    with open(_VICTIM, 'w') as f:\n"
        "        f.write('neutered')\n",
        encoding='utf-8')
    hits = _scan_file_for_inplace_shipped_writes(str(synthetic))
    assert 'routes/common.py' in hits, (
        f'scanner failed to flag a synthetic in-place shipped-source writer: {hits}')
    # And it is NOT in the registry → the real assertion would fire.
    assert 'routes/common.py' not in set(_load_guarded_registry()), (
        'precondition: routes/common.py should not be a registered NC target')


def test_scanner_ignores_tmp_path_and_copy_writers(tmp_path):
    """CONTROL: a test that writes only to tmp_path / a .nc_copy sibling must
    NOT be flagged (those are safe — a crash leaves litter, not a poisoned
    shipped file)."""
    safe = tmp_path / 'test_synthetic_safe_writer.py'
    safe.write_text(
        "import os\n"
        "ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
        "_SRC = os.path.join(ROOT, 'static', 'js', 'tofu-scene.js')\n"
        "def test_tmp(tmp_path):\n"
        "    (tmp_path / 'copy.js').write_text('x')\n"
        "def test_sibling_copy():\n"
        "    with open(_SRC + '.nc_copy.js', 'w') as f:\n"
        "        f.write('x')\n",
        encoding='utf-8')
    hits = _scan_file_for_inplace_shipped_writes(str(safe))
    assert hits == set(), (
        f'scanner wrongly flagged a tmp_path/.nc_copy-only writer: {hits}')


# ═══════════════════════════════════════════════════════════════════════════
#  Session-start crash-poison detector (conftest.warn_on_nc_source_poison...)
# ═══════════════════════════════════════════════════════════════════════════
# The autouse belt heals WITHIN a session, but its finally is skipped by a HARD
# crash (SIGKILL/OOM) and the NEXT session's lazy snapshot would adopt the
# leftover neuter as baseline. warn_on_nc_source_poison_at_session_start() is
# the start-of-run surfacing that uses git HEAD as the known-good oracle. These
# tests pin that it (a) detects a guarded file drifted from HEAD, and (b) stays
# quiet when every guarded file is clean.

def test_session_start_poison_detector_flags_drift_from_head(tmp_path, monkeypatch):
    """Detect a simulated leftover neuter in an isolated Git repository.

    This used to poison a real guarded source in the shared working tree.  In
    an xdist run, the syntax smoke test could read that file inside the brief
    write/restore window and report a spurious production SyntaxError.  A tiny
    temporary repository exercises the same real ``git diff`` boundary without
    exposing any shipped source to another worker.
    """
    import importlib
    import subprocess
    conftest = importlib.import_module('tests.conftest')
    repo = tmp_path / 'isolated_repo'
    repo.mkdir()
    victim = 'lib/conversations/project_board.py'
    guarded = repo / victim
    guarded.parent.mkdir(parents=True)
    guarded.write_text('BASELINE = True\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'add', victim], cwd=repo, check=True)
    subprocess.run(
        ['git', '-c', 'user.name=NC Guard', '-c', 'user.email=nc@example.invalid',
         'commit', '-q', '-m', 'baseline'],
        cwd=repo, check=True,
    )

    guarded.write_text(
        'BASELINE = True\n# NC-SIMULATED-LEFTOVER-NEUTER\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(conftest, '_ROOT_DIR', str(repo))
    monkeypatch.setattr(conftest, '_NC_GUARDED_SOURCES', (victim,))

    drifted = conftest.warn_on_nc_source_poison_at_session_start()

    assert drifted == [victim], (
        f'session-start detector failed to flag the isolated poisoned source: '
        f'{victim} not in {drifted}')


def test_session_start_detector_is_quiet_when_clean(monkeypatch):
    """CONTROL: if every guarded file matches HEAD, the detector reports no
    drift. We simulate a fully-clean tree by stubbing the git probe to 'clean'
    for every path (returncode 0) so the test is independent of ambient WIP."""
    import importlib
    conftest = importlib.import_module('tests.conftest')

    class _Clean:
        returncode = 0
    monkeypatch.setattr(conftest.__dict__['subprocess'] if 'subprocess'
                        in conftest.__dict__ else __import__('subprocess'),
                        'run', lambda *a, **k: _Clean(), raising=False)
    # The detector imports subprocess locally; patch the module object it uses.
    import subprocess as _sp
    monkeypatch.setattr(_sp, 'run', lambda *a, **k: _Clean())
    drifted = conftest.warn_on_nc_source_poison_at_session_start()
    assert drifted == [], f'detector reported drift on a clean tree: {drifted}'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
