"""tests/test_data_layout_separation.py — user-state ⇄ code-tree physical
separation for a plain (non-frozen) SOURCE / git install.

The classification epic (lib/runtime_layout) makes an update SKIP in-tree user
state; this suite covers the complementary root-cause fix: a fresh source
checkout must default its writable data/logs OUT of the code tree entirely, so
``git pull`` (or a tarball overlay) can never race an in-tree open SQLite WAL or
touch an in-tree DB. Existing installs (a populated in-tree ``data/``) stay put
with zero migration.

Resolution policy under test (``lib/runtime_paths._source_checkout_base`` +
the byte-for-byte twin ``lib/log._writable_base_dir``):

  precedence: $TOFU_DATA_DIR  >  $TOFU_DATA_LAYOUT  >  auto-detect
    * TOFU_DATA_DIR      → honoured verbatim (both roots + DB_PATH follow).
    * TOFU_DATA_LAYOUT=intree → force repo root.
    * TOFU_DATA_LAYOUT=xdg    → force per-user dir.
    * auto (default):
        - repo ``data/`` exists AND populated → in-tree (existing install).
        - else (fresh clone, no populated data/) → per-user XDG dir.

Ground-truthed against a REAL relocated repo root in a subprocess (so
``_REPO_ROOT`` / ``BASE_DIR`` actually point at a throwaway tree we control),
with a NEUTER proving the populated-check reads the filesystem (an EMPTY
``data/`` is NOT "populated") and twin-agreement pins log.py to runtime_paths.
"""

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/.project_sessions/abc/modifications.json',
}

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.unit


def _run_py(code: str, env_extra=None) -> subprocess.CompletedProcess:
    """Run child Python with a CLEAN data-layout env (no ambient overrides)."""
    env = os.environ.copy()
    env['PYTHONPATH'] = _REPO + os.pathsep + env.get('PYTHONPATH', '')
    for k in ('TOFU_DATA_DIR', 'CHATUI_DATA_DIR', 'TOFU_DATA_LAYOUT',
              'CHATUI_DATA_LAYOUT', 'TOFU_DB_PATH'):
        env.pop(k, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, '-c', textwrap.dedent(code)],
                          capture_output=True, text=True, env=env)


def _mk_repo(populated_data: bool | None):
    """Make a throwaway 'repo root'.

    populated_data:
      None  → no ``data/`` dir at all (fresh clone).
      False → an EMPTY ``data/`` dir (exists but no entries) — the neuter case.
      True  → ``data/`` with a file inside (an existing install).
    """
    root = os.path.realpath(tempfile.mkdtemp(prefix='tofu-repo-'))
    if populated_data is not None:
        d = os.path.join(root, 'data')
        os.makedirs(d)
        if populated_data:
            with open(os.path.join(d, 'tofu.db'), 'w') as fh:
                fh.write('x')
    return root


def _resolve_base(tmprepo, env_extra=None, want_log=True):
    """Relocate _REPO_ROOT/BASE_DIR to ``tmprepo`` in a child and return
    (runtime_paths base, log.py base, stderr) after recomputing."""
    code = f"""
        import logging, sys, os
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        import lib.runtime_paths as rp
        rp._REPO_ROOT = {tmprepo!r}
        base = rp._resolve_base()
        print('RPBASE', base)
        rp._BASE = base
        print('RPDATA', rp.data_root())
        print('RPLOGS', rp.logs_root())
    """
    if want_log:
        code += """
        import lib.log as L
        L.BASE_DIR = %r
        print('LOGBASE', L._writable_base_dir())
        """ % tmprepo
    r = _run_py(code, env_extra=env_extra)
    assert r.returncode == 0, r.stderr
    out = {}
    for ln in r.stdout.splitlines():
        if ' ' in ln:
            k, v = ln.split(' ', 1)
            out[k] = v
    return out, r.stderr


class SourceCheckoutLayoutTest(unittest.TestCase):

    def test_a_tofu_data_dir_honored_and_db_follows(self):
        """Scenario (a): $TOFU_DATA_DIR set → both roots AND DB_PATH follow it."""
        base = os.path.realpath(tempfile.mkdtemp())
        data = os.path.join(base, 'data')
        r = _run_py("""
            import lib.runtime_paths as rp
            print('DATA', rp.data_root())
            print('LOGS', rp.logs_root())
            import lib.database._core as core
            print('DB', core.DB_PATH)
        """, env_extra={'TOFU_DATA_DIR': data})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('DATA %s' % data, r.stdout)
        self.assertIn('LOGS %s' % os.path.join(base, 'logs'), r.stdout)
        # DB_PATH = data_root()/tofu.db — proves the DB follows the resolved base.
        self.assertIn('DB %s' % os.path.join(data, 'tofu.db'), r.stdout)

    def test_b_fresh_clone_goes_xdg(self):
        """Scenario (b): fresh tree, NO data/ → per-user XDG dir, NOT the repo."""
        xdg = os.path.realpath(tempfile.mkdtemp())
        repo = _mk_repo(populated_data=None)
        out, err = _resolve_base(repo, env_extra={'XDG_DATA_HOME': xdg})
        peruser = os.path.join(xdg, 'Tofu')
        self.assertEqual(out['RPBASE'], peruser,
                         'fresh clone must resolve to the per-user dir')
        self.assertEqual(out['RPDATA'], os.path.join(peruser, 'data'))
        self.assertEqual(out['RPLOGS'], os.path.join(peruser, 'logs'))
        self.assertNotIn(repo, out['RPDATA'],
                         'user state leaked back into the code tree')
        # INFO diagnosability (§2): the chosen base + why is logged.
        self.assertIn('Data layout: fresh source checkout', err)

    def test_c_populated_intree_stays(self):
        """Scenario (c): tree WITH a populated data/ → stays in-tree (existing
        install, zero migration)."""
        xdg = os.path.realpath(tempfile.mkdtemp())
        repo = _mk_repo(populated_data=True)
        out, err = _resolve_base(repo, env_extra={'XDG_DATA_HOME': xdg})
        self.assertEqual(out['RPBASE'], repo,
                         'existing populated in-tree data/ must keep the repo root')
        self.assertEqual(out['RPDATA'], os.path.join(repo, 'data'))
        self.assertIn('Data layout: existing in-tree data/ found', err)

    def test_d_neuter_empty_data_not_populated(self):
        """NEUTER: an EMPTY data/ dir (exists but no entries) must be treated as
        FRESH → per-user. Proves the check reads FS CONTENTS, not mere
        existence — an ``os.path.exists`` would wrongly keep it in-tree here."""
        xdg = os.path.realpath(tempfile.mkdtemp())
        repo = _mk_repo(populated_data=False)  # empty data/ present
        out, err = _resolve_base(repo, env_extra={'XDG_DATA_HOME': xdg})
        peruser = os.path.join(xdg, 'Tofu')
        self.assertEqual(out['RPBASE'], peruser,
                         'empty data/ was wrongly treated as populated — the '
                         'check is testing existence, not contents')
        self.assertIn('Data layout: fresh source checkout', err)

    def test_e_layout_intree_forces_repo(self):
        """TOFU_DATA_LAYOUT=intree forces the repo root even on a fresh tree."""
        xdg = os.path.realpath(tempfile.mkdtemp())
        repo = _mk_repo(populated_data=None)  # no data/ → auto would go XDG
        out, err = _resolve_base(
            repo, env_extra={'XDG_DATA_HOME': xdg, 'TOFU_DATA_LAYOUT': 'intree'})
        self.assertEqual(out['RPBASE'], repo)
        self.assertIn('TOFU_DATA_LAYOUT=intree', err)

    def test_f_layout_xdg_forces_peruser(self):
        """TOFU_DATA_LAYOUT=xdg forces per-user even with a populated in-tree
        data/ (opt-in relocation for an existing single-box user)."""
        xdg = os.path.realpath(tempfile.mkdtemp())
        repo = _mk_repo(populated_data=True)  # populated → auto would stay in-tree
        out, err = _resolve_base(
            repo, env_extra={'XDG_DATA_HOME': xdg, 'TOFU_DATA_LAYOUT': 'xdg'})
        self.assertEqual(out['RPBASE'], os.path.join(xdg, 'Tofu'))
        self.assertIn('TOFU_DATA_LAYOUT=xdg', err)


class UploadsCoLocationTest(unittest.TestCase):
    """User-uploaded / generated assets (uploads/) must follow the SAME
    resolved base as the DB. Historically every consumer recomputed
    ``<repo>/uploads`` from its own ``__file__``, so a relocated install
    (``$TOFU_DATA_DIR`` / XDG) split the images away from the DB that
    references them by ``/api/images/`` URL. ``runtime_paths.uploads_root()``
    is now the one authority and every consumer derives from it."""

    def test_uploads_root_colocates_with_data(self):
        """$TOFU_DATA_DIR set → uploads_root() and data_root() share the base
        (no split between the DB and the images it points at)."""
        base = os.path.realpath(tempfile.mkdtemp())
        data = os.path.join(base, 'data')
        r = _run_py("""
            import lib.runtime_paths as rp
            print('DATA', rp.data_root())
            print('UPLOADS', rp.uploads_root())
        """, env_extra={'TOFU_DATA_DIR': data})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('DATA %s' % os.path.join(base, 'data'), r.stdout)
        self.assertIn('UPLOADS %s' % os.path.join(base, 'uploads'), r.stdout)

    def test_fresh_clone_uploads_go_xdg(self):
        """Fresh tree → uploads land under the per-user XDG dir, NOT the repo."""
        xdg = os.path.realpath(tempfile.mkdtemp())
        repo = _mk_repo(populated_data=None)
        code = f"""
            import lib.runtime_paths as rp
            rp._REPO_ROOT = {repo!r}
            rp._BASE = rp._resolve_base()
            print('UPLOADS', rp.uploads_root())
        """
        r = _run_py(code, env_extra={'XDG_DATA_HOME': xdg})
        self.assertEqual(r.returncode, 0, r.stderr)
        peruser_uploads = os.path.join(xdg, 'Tofu', 'uploads')
        self.assertIn('UPLOADS %s' % peruser_uploads, r.stdout)
        self.assertNotIn('UPLOADS %s' % os.path.join(repo, 'uploads'), r.stdout)

    def test_default_intree_uploads_byte_identical(self):
        """Populated in-tree data/ → uploads stays <repo>/uploads (byte-identical
        to the legacy path, zero migration for existing installs)."""
        xdg = os.path.realpath(tempfile.mkdtemp())
        repo = _mk_repo(populated_data=True)
        code = f"""
            import lib.runtime_paths as rp
            rp._REPO_ROOT = {repo!r}
            rp._BASE = rp._resolve_base()
            print('UPLOADS', rp.uploads_root())
        """
        r = _run_py(code, env_extra={'XDG_DATA_HOME': xdg})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('UPLOADS %s' % os.path.join(repo, 'uploads'), r.stdout)

    def test_real_consumers_follow_relocated_base(self):
        """TEETH: import the ACTUAL shipped uploads consumers with a relocated
        $TOFU_DATA_DIR and assert each asset dir sits under the relocated base
        and NOT under the code tree (``_REPO``). A consumer that still computed
        ``<repo>/uploads`` from its own ``__file__`` would land under ``_REPO``
        and fail the anti-split assertion — this is the neuter-equivalent."""
        base = os.path.realpath(tempfile.mkdtemp())
        data = os.path.join(base, 'data')
        code = """
            import os
            import lib.runtime_paths as rp
            base_uploads = rp.uploads_root()
            print('AUTHORITY', base_uploads)
            # paper PDFs + figure manifests (DB-referenced)
            import lib.paper.hashing as ph
            print('PAPER', ph.PAPER_DIR)
            # translated PPTX (download-URL referenced)
            import lib.translate.pptx as pptx
            print('PPTX', pptx._PPTX_UPLOAD_DIR)
            # generated chat images (/api/images/ referenced)
            import lib.tasks_pkg.executor_image as ei
            print('GENIMG', ei._images_dir())
            # chat image validation resolves /api/images/ from the same dir
            import lib.llm.body as body
            _blocks = [{'role': 'user', 'content': [
                {'type': 'image_url',
                 'image_url': {'url': '/api/images/does_not_exist.png'}}]}]
            body._validate_image_blocks(_blocks)
            # (import-time success is the signal; path asserted via GENIMG)
        """
        r = _run_py(code, env_extra={'TOFU_DATA_DIR': data})
        self.assertEqual(r.returncode, 0, r.stderr)
        vals = {}
        for ln in r.stdout.splitlines():
            if ' ' in ln:
                k, v = ln.split(' ', 1)
                vals[k] = v
        # Every consumer's asset dir must sit under the relocated base…
        for key in ('AUTHORITY', 'PAPER', 'PPTX', 'GENIMG'):
            self.assertIn(key, vals, f'consumer {key} did not report a path')
            self.assertTrue(
                vals[key].startswith(base),
                f'{key}={vals[key]} did not follow the relocated base {base}')
            # …and must NOT still be computed from the code tree.
            self.assertFalse(
                vals[key].startswith(_REPO + os.sep),
                f'{key}={vals[key]} is still anchored to the code tree {_REPO} '
                f'(consumer bypasses uploads_root — the split is not closed)')


class ProjectSessionsCoLocationTest(unittest.TestCase):
    """The per-project undo/redo store (``.project_sessions`` — session_id/
    modifications.json, holding the PRE-image of every edited file) is mutable
    USER STATE that was historically written INTO the code tree at
    ``<repo>/lib/.project_sessions``. On a frozen / read-only / relocated
    install that targets a read-only ``lib/``. ``project_sessions_root()`` is
    the one authority: legacy path in-tree (byte-identical, zero migration),
    ``<base>/data/project_sessions`` when the base moves off the code tree."""

    def test_relocated_base_colocates_sessions_with_data(self):
        """$TOFU_DATA_DIR set → sessions live under the relocated base's data/,
        NOT the code tree."""
        base = os.path.realpath(tempfile.mkdtemp())
        data = os.path.join(base, 'data')
        r = _run_py("""
            import lib.runtime_paths as rp
            print('SESS', rp.project_sessions_root())
            print('DATA', rp.data_root())
        """, env_extra={'TOFU_DATA_DIR': data})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('SESS %s' % os.path.join(data, 'project_sessions'), r.stdout)
        # …and it must NOT be anchored to the code tree.
        self.assertNotIn('SESS %s' % os.path.join(_REPO, 'lib', '.project_sessions'),
                         r.stdout)

    def test_intree_base_byte_identical_legacy_path(self):
        """In-tree base → the legacy ``<repo>/lib/.project_sessions`` exactly, so
        an existing install's undo history is never orphaned."""
        xdg = os.path.realpath(tempfile.mkdtemp())
        repo = _mk_repo(populated_data=True)  # populated → auto stays in-tree
        code = f"""
            import lib.runtime_paths as rp
            rp._REPO_ROOT = {repo!r}
            rp._BASE = rp._resolve_base()
            print('SESS', rp.project_sessions_root())
        """
        r = _run_py(code, env_extra={'XDG_DATA_HOME': xdg})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('SESS %s' % os.path.join(repo, 'lib', '.project_sessions'),
                      r.stdout)

    def test_fresh_clone_sessions_go_xdg(self):
        """Fresh tree → sessions land under the per-user XDG data/, NOT the repo."""
        xdg = os.path.realpath(tempfile.mkdtemp())
        repo = _mk_repo(populated_data=None)
        code = f"""
            import lib.runtime_paths as rp
            rp._REPO_ROOT = {repo!r}
            rp._BASE = rp._resolve_base()
            print('SESS', rp.project_sessions_root())
        """
        r = _run_py(code, env_extra={'XDG_DATA_HOME': xdg})
        self.assertEqual(r.returncode, 0, r.stderr)
        peruser_sess = os.path.join(xdg, 'Tofu', 'data', 'project_sessions')
        self.assertIn('SESS %s' % peruser_sess, r.stdout)
        self.assertNotIn(repo, r.stdout.split('SESS ', 1)[-1].splitlines()[0])

    def test_real_consumer_config_sessions_dir_follows_base(self):
        """TEETH: the ACTUAL consumers (``config.SESSIONS_DIR`` and its
        re-export in ``modifications``) must resolve under the relocated base,
        not the code tree. A constant still computed from ``__file__`` would
        land under ``_REPO/lib`` and fail the anti-split assertion."""
        base = os.path.realpath(tempfile.mkdtemp())
        data = os.path.join(base, 'data')
        code = """
            import os
            import lib.project_mod.config as cfg
            print('CFG', cfg.SESSIONS_DIR)
            import lib.project_mod.modifications as m
            print('MOD', m.SESSIONS_DIR)
        """
        r = _run_py(code, env_extra={'TOFU_DATA_DIR': data})
        self.assertEqual(r.returncode, 0, r.stderr)
        vals = {}
        for ln in r.stdout.splitlines():
            if ' ' in ln:
                k, v = ln.split(' ', 1)
                vals[k] = v
        for key in ('CFG', 'MOD'):
            self.assertIn(key, vals)
            self.assertTrue(
                vals[key].startswith(base),
                f'{key}={vals[key]} did not follow the relocated base {base}')
            self.assertFalse(
                vals[key].startswith(_REPO + os.sep),
                f'{key}={vals[key]} is still anchored to the code tree — '
                f'SESSIONS_DIR bypasses project_sessions_root')
        # The two consumers must agree (modifications imports the config const).
        self.assertEqual(vals['CFG'], vals['MOD'])


class ProjectSessionsRegistryTest(unittest.TestCase):
    """The in-tree undo store ``lib/.project_sessions`` must be recognised by
    the SINGLE runtime-state authority (``lib.runtime_layout``), not only by a
    hand-maintained ``.gitignore`` line. Before this was registered, the
    relocation shipped a false docstring claim AND the predicates returned
    False → an overlay update could clobber undo history and a
    registry-regenerated ``.gitignore`` would silently drop the ignore."""

    def test_overlay_skip_and_runtime_state_recognise_undo_store(self):
        import lib.runtime_layout as rl
        p = 'lib/.project_sessions/abc/modifications.json'
        self.assertTrue(rl.is_overlay_skipped(p),
                        'undo store not overlay-skipped — an update could '
                        'clobber it')
        self.assertTrue(rl.is_runtime_state(p),
                        'undo store not classified runtime state — a dirty '
                        'tree would block updates on it')

    def test_gitignore_lines_emits_undo_store(self):
        import lib.runtime_layout as rl
        joined = '\n'.join(rl.gitignore_lines())
        self.assertIn('lib/.project_sessions/', joined,
                      'gitignore_lines() omits the undo store — a regenerated '
                      '.gitignore would leak it into version control')

    def test_live_gitignore_line_is_registry_derived(self):
        """The live .gitignore must carry the scoped ``lib/.project_sessions/``
        entry (matching the registry), NOT the old over-broad ``**/`` glob —
        proving the parallel hand-maintained list was retired."""
        with open(os.path.join(_REPO, '.gitignore'), encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('lib/.project_sessions/', text)
        self.assertNotIn('**/.project_sessions/', text,
                         'stale over-broad glob still present — the parallel '
                         'list was not retired')

    def test_neuter_drop_undo_store_entry_defeats_guard(self):
        """NEUTER: remove the registry entry → both predicates must go False
        AND gitignore_lines() must drop it, proving the entry is load-bearing
        (not covered incidentally by another prefix)."""
        import lib.runtime_layout as rl
        orig = rl.INSTALL_STATE
        orig_rt = rl.RUNTIME_STATE_PREFIXES
        orig_skip = rl.OVERLAY_SKIP_PREFIXES
        try:
            rl.INSTALL_STATE = tuple(
                e for e in orig if e.prefix != 'lib/.project_sessions/')
            rl.RUNTIME_STATE_PREFIXES = ('.tofu/',) + tuple(
                e.prefix for e in rl.INSTALL_STATE)
            rl.OVERLAY_SKIP_PREFIXES = (rl.RUNTIME_STATE_PREFIXES
                                        + rl._VCS_BUILD_PREFIXES)
            p = 'lib/.project_sessions/abc/modifications.json'
            self.assertFalse(rl.is_overlay_skipped(p),
                             'neuter failed: still overlay-skipped after '
                             'removal — not reading the registry')
            self.assertFalse(rl.is_runtime_state(p))
            self.assertNotIn('lib/.project_sessions/',
                             '\n'.join(rl.gitignore_lines()))
        finally:
            rl.INSTALL_STATE = orig
            rl.RUNTIME_STATE_PREFIXES = orig_rt
            rl.OVERLAY_SKIP_PREFIXES = orig_skip
        # Restored → guard active again.
        self.assertTrue(rl.is_overlay_skipped(
            'lib/.project_sessions/abc/modifications.json'))


class NonRuntimeStateDirsTest(unittest.TestCase):
    """``outputs/`` and ``overleaf_cache/`` are declared in
    ``runtime_layout.INSTALL_STATE`` (so an update never CLOBBERS them) but,
    unlike data/logs/uploads/.project_sessions, they have NO shipped-runtime
    path CONSTRUCTOR that anchors them to the code tree — so there is nothing
    to route through the resolved base. This test PROVES that claim so the
    boundary can't silently regress (a future ``os.path.join(BASE, 'outputs')``
    in lib/ or routes/ would trip it)."""

    _SRC_DIRS = ('lib', 'routes')

    def _grep_for_intree_ctor(self, dirname_literal):
        """Return offending "<code>/<dir>" path constructions in lib/ + routes/,
        excluding the runtime_layout registry (declarations, not constructors)
        and export.py (sanitizer axis)."""
        import re
        # A path join that ends in the literal dir name — the anchoring shape.
        pat = re.compile(
            r"""os\.path\.join\([^)]*['"]%s['"]""" % re.escape(dirname_literal))
        hits = []
        for d in self._SRC_DIRS:
            root = os.path.join(_REPO, d)
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune heavy/irrelevant trees (FUSE-safety per project memory).
                dirnames[:] = [x for x in dirnames if x not in (
                    '__pycache__', '.project_sessions', '.git', '.tofu',
                    'node_modules', '.ruff_cache', '.pytest_cache')]
                for fn in filenames:
                    if not fn.endswith('.py'):
                        continue
                    fp = os.path.join(dirpath, fn)
                    try:
                        with open(fp, encoding='utf-8', errors='replace') as fh:
                            txt = fh.read()
                    except OSError:
                        continue
                    for m in pat.finditer(txt):
                        hits.append('%s: %s' % (
                            os.path.relpath(fp, _REPO), m.group(0)))
        return hits

    def test_outputs_has_no_intree_runtime_constructor(self):
        hits = self._grep_for_intree_ctor('outputs')
        self.assertEqual(hits, [],
                         'a shipped module now constructs an in-tree outputs/ '
                         'path — route it through runtime_paths (data_root or a '
                         'new outputs_root) like uploads/sessions: %r' % hits)

    def test_overleaf_cache_has_no_intree_runtime_constructor(self):
        hits = self._grep_for_intree_ctor('overleaf_cache')
        self.assertEqual(hits, [],
                         'a shipped module now constructs an in-tree '
                         'overleaf_cache/ path (it was previously only written '
                         'by the external overleaf-mcp subprocess): %r' % hits)


class LogTwinAgreementTest(unittest.TestCase):
    """lib/log.py's inline twin must reach the IDENTICAL base as runtime_paths
    for the new source-checkout branch — else logs/ and data/ split across the
    in-tree/XDG boundary. (log.py can't import runtime_paths — cycle.)"""

    def _assert_agree(self, populated, env_extra):
        repo = _mk_repo(populated_data=populated)
        out, _ = _resolve_base(repo, env_extra=env_extra)
        self.assertEqual(out['RPBASE'], out['LOGBASE'],
                         'log.py twin disagrees with runtime_paths: '
                         f'{out["LOGBASE"]} != {out["RPBASE"]}')

    def test_twin_agrees_fresh_clone(self):
        xdg = os.path.realpath(tempfile.mkdtemp())
        self._assert_agree(None, {'XDG_DATA_HOME': xdg})

    def test_twin_agrees_populated_intree(self):
        xdg = os.path.realpath(tempfile.mkdtemp())
        self._assert_agree(True, {'XDG_DATA_HOME': xdg})

    def test_twin_agrees_empty_data_neuter(self):
        xdg = os.path.realpath(tempfile.mkdtemp())
        self._assert_agree(False, {'XDG_DATA_HOME': xdg})

    def test_twin_agrees_layout_xdg(self):
        xdg = os.path.realpath(tempfile.mkdtemp())
        self._assert_agree(True, {'XDG_DATA_HOME': xdg, 'TOFU_DATA_LAYOUT': 'xdg'})


if __name__ == '__main__':
    unittest.main()
