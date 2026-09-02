"""tests/test_published_pipeline_drift.py — the file we VALIDATE must be the
file that RUNS.

WHY THIS EXISTS
---------------
``tests/test_desktop_build_workflow.py`` pins 30+ invariants about the release
pipeline — the trigger, the runner labels, the completeness gate, the orphan-tag
retarget. Every one of them reads ``_ROOT / '.github/workflows/build-desktop.yml'``,
i.e. the LOCAL file. That file has been correct since 2026-07-29.

The file GitHub Actions actually executed for v0.15.0 / v0.15.1 / v0.15.2 was a
2026-07-23 copy still carrying ``on: push tags: v*`` and the retired ``macos-13``
label. Measured on the real repo: all three runs ended ``cancelled`` after the
x86_64 leg sat queued for exactly 24 h against a label GitHub retired on
2025-12-08, so ``release`` was skipped by ``needs:`` and nothing shipped.

So the guard suite was GREEN throughout a three-release outage, and structurally
had to be: nothing in it compares the validated file to the deployed one. That
is the defect class this module closes — **no consistency check existed between
the local source of truth and the artifact actually running in production.**

HOW THE DRIFT HAPPENS (measured, not inferred)
----------------------------------------------
``export.py`` publishes by re-``git init``-ing a sanitized copy and pushing it
over the mirror; ``_push_branch`` force-pushes on a non-fast-forward rejection.
It is a ONE-WAY overwrite: the local tree is the only source of truth and the
remote is a projection of it.

That is a coherent design, but it has a silent failure mode. Commit ``128ad422``
("fix Intel leg — retired macos-13 -> macos-15-intel") was authored directly on
the downstream repo and shipped v0.14.2 successfully. It never flowed back
upstream. Verified here: ``git merge-base --is-ancestor 128ad422 HEAD`` is
false, and fetching ``origin/main`` reports
``+ 128ad422...59bc8254 main -> origin/main (forced update)`` — the next export
force-pushed straight over the fix, restoring ``macos-13`` and re-breaking the
pipeline. No error, no log line, no failing test.

A downstream-only edit is therefore not merely lost: it is REVERTED, and the
revert is invisible. This guard makes that state observable BEFORE the next push
instead of three unreleased versions later.

WHY BYTE-EQUALITY IS THE RIGHT ASSERTION
----------------------------------------
Only because it was measured to be achievable. An opensource export applies a
CHAIN of transforms (``export.py`` 2488-2490): restore whitelisted keep-files,
then ``ruff check --fix --unsafe-fixes`` over the whole tree, then verify. The
composed chain was measured to be the IDENTITY on every guarded path, and
``test_guarded_paths_survive_the_whole_export_transform_chain`` keeps it that
way — modelling only the sanitizer would miss ruff, which really does get a vote
on ``scripts/release_assets.py``.

So "published == local" is a real invariant for these files, not an
approximation, and any weaker comparison (parse-and-compare-fields) would
re-admit the exact bug: a field nobody thought to compare.

Do NOT extend ``_GUARDED_PATHS`` to a file the export rewrites — this test would
then be permanently red for a legitimate reason, and a permanently-red guard
gets muted, which is how a suite goes blind. Add such a file only with a
transform-then-compare variant.

WHY BOTH REMOTES
----------------
``export.py::_GIT_REPOS['opensource']`` pushes to TWO remotes: ``rangehow/ToFu``
and ``NiuTrans/ToFu``. Both were measured at the same stale ``59bc8254``. A guard
covering only the first would call the fleet clean while half of it was stale.

WHY A SKIP (AND NOT A PASS) WHEN OFFLINE
-----------------------------------------
The evidence lives on the network. Unreachable network means the invariant was
NOT CHECKED, and reporting "not checked" as "consistent" is precisely the shape
of lie this module was written to eliminate. Offline therefore skips loudly.
"""

from __future__ import annotations

import difflib
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

# Repos that `export.py --push` publishes the opensource tree to, mirroring
# _GIT_REPOS['opensource']['remotes'] (tokens stripped — this is a public read).
_PUBLISHED_REPOS = (
    ('rangehow/ToFu', 'main'),
    ('NiuTrans/ToFu', 'main'),
)

# Files whose published copy must be byte-identical to the local source.
# Every entry must survive export.py's sanitizer unchanged — see the module
# docstring before adding to this list.
_GUARDED_PATHS = (
    '.github/workflows/build-desktop.yml',
    '.github/workflows/ci.yml',
    'scripts/release_assets.py',
)

_FETCH_TIMEOUT = 20


def _fetch_published(repo: str, branch: str, path: str) -> str | None:
    """Return the published file text, or None when it does not exist there.

    Raises OSError when the network itself is unusable, so the caller can
    distinguish "absent upstream" (a real finding) from "could not look"
    (a skip).
    """
    url = f'https://raw.githubusercontent.com/{repo}/{branch}/{path}'
    try:
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT) as resp:
            return resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise OSError(f'HTTP {e.code} fetching {url}') from e


@pytest.fixture(scope='module')
def _network():
    """Skip the module unless raw.githubusercontent is actually reachable."""
    if os.environ.get('TOFU_SKIP_NETWORK_TESTS'):
        pytest.skip('TOFU_SKIP_NETWORK_TESTS set')
    try:
        urllib.request.urlopen(
            'https://raw.githubusercontent.com/rangehow/ToFu/main/VERSION',
            timeout=_FETCH_TIMEOUT).read()
    except Exception as e:
        pytest.skip(f'raw.githubusercontent unreachable ({type(e).__name__}: {e}) '
                    '— drift NOT checked, which is not the same as no drift')


@pytest.mark.parametrize('repo,branch', _PUBLISHED_REPOS)
@pytest.mark.parametrize('path', _GUARDED_PATHS)
@pytest.mark.slow
def test_published_release_pipeline_matches_local_source(_network, repo, branch, path):
    """The deployed pipeline file must be byte-identical to the local one.

    This is the assertion that was missing while v0.15.0/0.15.1/0.15.2 all
    failed to publish: the local file said ``macos-15-intel`` and the file
    GitHub ran said ``macos-13``, and no test could see the difference.
    """
    local_file = _ROOT / path
    assert local_file.is_file(), f'{path} missing locally — guard list is stale'
    local = local_file.read_text(encoding='utf-8')

    try:
        published = _fetch_published(repo, branch, path)
    except OSError as e:
        pytest.skip(f'could not read {path} from {repo} ({e}) — drift NOT checked')

    if published is None:
        pytest.fail(
            f'{path} does not exist on {repo}@{branch}, but it does locally.\n'
            'The published pipeline is running WITHOUT a file the local gates '
            'depend on. Publish the current tree (export.py --push).'
        )

    if published != local:
        diff = '\n'.join(difflib.unified_diff(
            published.splitlines(), local.splitlines(),
            fromfile=f'PUBLISHED {repo}@{branch}:{path}',
            tofile=f'LOCAL {path}',
            lineterm='', n=2,
        ))
        pytest.fail(
            f'DEPLOYMENT DRIFT: {path} on {repo}@{branch} differs from the local '
            f'source of truth.\n\n'
            'The pipeline that actually RUNS is not the one this test suite '
            'validates, so every other guard over this file is reporting on a '
            'copy nobody executes.\n\n'
            'Decide which side is right BEFORE pushing: export.py force-pushes '
            'over the remote, so a downstream-only fix (as happened with '
            'commit 128ad422) is silently reverted rather than merged.\n\n'
            f'{diff}'
        )


def _load_export():
    """Import export.py as a module (it is not on the import path)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location('_export_mod', _ROOT / 'export.py')
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


#: TRAP-4: export.py itself is NOT shipped in the opensource export — the
#: published tree cannot contain the transform chain that produces it. Tests
#: that drive the export transforms only have a subject in the SOURCE tree.
_requires_export_py = pytest.mark.skipif(
    not (_ROOT / 'export.py').is_file(),
    reason='export.py is not shipped in the opensource export (TRAP-4) — the '
           'transform chain these tests drive does not exist in this tree')


@_requires_export_py
@pytest.mark.unit
def test_guarded_paths_survive_the_whole_export_transform_chain():
    """Byte-equality above is only valid for files the export does not rewrite.

    The precondition is NOT "the sanitizer leaves it alone" — that models only
    the first of the transforms an opensource export applies. ``export.py``
    (2488-2490) runs, in order:

        _restore_opensource_kept_files(dest)   # copy whitelisted files back
        _run_ruff_autofix(dest)                # ruff check --fix --unsafe-fixes
        _verify_opensource(dest)

    ``scripts/release_assets.py`` is a ``.py`` file sitting under that autofix,
    so ruff — not just the sanitizer — decides whether its published bytes match
    the local ones. Modelling only the sanitizer would leave the drift test able
    to go red for a LEGITIMATE reason (ruff reformatted the shipped copy), and a
    guard that is red for a legitimate reason gets muted, which is exactly the
    blindness this module exists to prevent.

    So this asserts the composed transform is the identity. The ``.yml`` files
    are unaffected by ruff, which is why they pass trivially — but they are
    still run through the chain rather than special-cased, so the day ruff (or
    any future step) grows a YAML formatter, this fails instead of the drift
    test.
    """
    import shutil
    import subprocess
    import tempfile

    mod = _load_export()
    sanitize = getattr(mod, '_sanitize_source_opensource', None)
    assert sanitize is not None, 'export.py no longer exposes _sanitize_source_opensource'

    # Mirror _run_ruff_autofix's real argv — in particular --unfixable F401,F811,
    # which is load-bearing there (it stops ruff deleting facade re-exports).
    ruff_argv = ['python', '-m', 'ruff', 'check', '--fix', '--unsafe-fixes',
                 '--unfixable', 'F401,F811']

    for path in _GUARDED_PATHS:
        original = (_ROOT / path).read_text(encoding='utf-8')

        # Stage 1 — sanitizer.
        staged = sanitize(original, path)
        assert staged == original, (
            f'{path} is REWRITTEN by the export sanitizer, so its published copy '
            'can never be byte-identical to the local one. Either drop it from '
            '_GUARDED_PATHS or compare the sanitized text instead of raw text.'
        )

        # Stage 2 — ruff autofix, run on a real temp copy at the same relative
        # path so per-file-ignores in ruff.toml resolve the way they do on the
        # exported tree.
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(staged, encoding='utf-8')
            shutil.copy2(_ROOT / 'ruff.toml', Path(td) / 'ruff.toml')
            try:
                subprocess.run(ruff_argv + [str(target)], cwd=td,
                               capture_output=True, timeout=120)
            except (OSError, subprocess.SubprocessError) as e:
                pytest.skip(f'ruff unavailable ({e}) — transform chain NOT verified')
            after = target.read_text(encoding='utf-8')

        assert after == original, (
            f'{path} is REWRITTEN by _run_ruff_autofix during the opensource '
            'export, so the published bytes will never equal the local bytes and '
            'the drift test above would be permanently red for a LEGITIMATE '
            'reason.\n'
            'Fix the source so ruff has nothing to change, or compare the '
            'post-transform text instead of the raw text. Do NOT mute the drift '
            'test.'
        )


@_requires_export_py
@pytest.mark.unit
def test_guarded_paths_are_actually_published_by_the_export():
    """A guarded file that the export never ships is a guard that can only fail.

    ``scripts/`` is an opensource-excluded DIRECTORY (it holds benchmark scripts
    with internal paths). ``scripts/release_assets.py`` reaches the public tree
    only because it is listed by hand in ``_OPENSOURCE_KEEP_FILES`` and copied
    back by ``_restore_opensource_kept_files``.

    That whitelist has no guard of its own. Add a second script the pipeline
    depends on, forget the list entry, and the published workflow calls a file
    that is not there — the same silent break as the stale workflow, one level
    down. This closes it by asserting every guarded path is genuinely reachable
    in the published tree: either not excluded at all, or excluded-but-restored.
    """
    mod = _load_export()
    should_exclude = getattr(mod, '_should_exclude', None)
    keep_files = getattr(mod, '_OPENSOURCE_KEEP_FILES', None)
    assert should_exclude is not None, 'export.py no longer exposes _should_exclude'
    assert keep_files is not None, 'export.py no longer exposes _OPENSOURCE_KEEP_FILES'

    for path in _GUARDED_PATHS:
        reason = should_exclude(path, os.path.basename(path), 'opensource')
        if reason is None:
            continue  # shipped directly
        assert path in keep_files, (
            f'{path} is EXCLUDED from the opensource export ({reason}) and is NOT '
            'in _OPENSOURCE_KEEP_FILES, so it will never appear in the published '
            'tree. The drift guard above would then report it missing forever, '
            'and — more importantly — the published release pipeline would call '
            'a file that does not exist. Add it to _OPENSOURCE_KEEP_FILES.'
        )
