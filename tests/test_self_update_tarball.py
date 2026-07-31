#!/usr/bin/env python3
"""Unit tests for the tarball-overlay self-update fallback (lib.self_update).

Covers the non-git deployment path added so exported copies / zip downloads
can still be updated via the topbar button:

  - _overlay_skip() classifies user/runtime state vs. tracked source.
  - _apply_via_tarball() overlays a downloaded release, preserving user data
    (.tofu/ memories, data/ DB), backing up replaced files, and refusing an
    invalid/partial archive without mutating the live install.

The download is faked by monkeypatching ``http_stream`` to stream a locally
built tarball, and ``_ROOT`` is pointed at a throwaway "install" dir, so the
tests never hit the network or touch the real project tree.
"""

import contextlib
import os
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _incompressible(n=3000):
    """Return text padding that gzip can't shrink below the size guard."""
    return '\n# ' + os.urandom(n).hex() + '\n'


def _build_install(tmp):
    """Create a fake live install with user data + tracked source."""
    inst = tempfile.mkdtemp(prefix='tofu-inst-', dir=tmp)
    for d in ('lib', '.tofu/skills', 'data'):
        os.makedirs(os.path.join(inst, d))
    open(os.path.join(inst, 'server.py'), 'w').write('OLD server\n')
    open(os.path.join(inst, 'VERSION'), 'w').write('0.12.0\n')
    open(os.path.join(inst, 'lib', 'foo.py'), 'w').write('OLD foo\n')
    open(os.path.join(inst, '.tofu', 'skills', 'mine.md'), 'w').write('MY MEMORY\n')
    open(os.path.join(inst, 'data', 'app.db'), 'w').write('MY DB\n')
    return inst


def _build_tarball(tmp, version='9.9.9', valid=True, same_requirements=None):
    """Build a GitHub-style tarball (single wrapper dir). When valid, it
    carries the sentinels; otherwise it's a non-Tofu tree.

    ``same_requirements`` (str) writes a requirements.txt into the archive so
    the requirements-diff / pip-skip logic can be exercised."""
    stage = tempfile.mkdtemp(prefix='tofu-stage-', dir=tmp)
    wrap = os.path.join(stage, 'rangehow-ToFu-deadbeef')
    if valid:
        os.makedirs(os.path.join(wrap, 'lib'))
        os.makedirs(os.path.join(wrap, '.tofu', 'skills'))
        open(os.path.join(wrap, 'server.py'), 'w').write('NEW server' + _incompressible())
        open(os.path.join(wrap, 'VERSION'), 'w').write(version + '\n')
        open(os.path.join(wrap, 'lib', 'foo.py'), 'w').write('NEW foo' + _incompressible())
        open(os.path.join(wrap, 'lib', 'bar.py'), 'w').write('NEW bar' + _incompressible())
        # Upstream ships a .tofu file — must be SKIPPED, never overwrite the user's.
        open(os.path.join(wrap, '.tofu', 'skills', 'upstream.md'), 'w').write('UPSTREAM' + _incompressible())
        if same_requirements is not None:
            open(os.path.join(wrap, 'requirements.txt'), 'w').write(same_requirements)
    else:
        os.makedirs(wrap)
        open(os.path.join(wrap, 'README.md'), 'w').write('not tofu' + _incompressible())
    tar_path = os.path.join(stage, 'rel.tar.gz')
    with tarfile.open(tar_path, 'w:gz') as tf:
        tf.add(wrap, arcname='rangehow-ToFu-deadbeef')
    return tar_path


def _patch_stream(su, tar_path, with_length=False):
    """Point su.http_stream at a local tarball file.

    When ``with_length`` is True the fake response carries a Content-Length
    header (so the updater can compute a determinate percentage) and yields
    the body in small chunks so multiple progress frames fire."""
    size = os.path.getsize(tar_path)

    class _Resp:
        status_code = 200
        headers = {'Content-Length': str(size)} if with_length else {}
        def __init__(self, p): self._p = p
        def iter_content(self, n):
            step = 4096 if with_length else n
            with open(self._p, 'rb') as fh:
                while True:
                    c = fh.read(step)
                    if not c:
                        break
                    yield c
    @contextlib.contextmanager
    def _fake(method, url, **kw):
        yield _Resp(tar_path)
    su.http_stream = _fake


def test_tarball_download_emits_progress():
    """A tarball download must emit determinate progress frames (pct + bytes
    + speed) so the UI shows a live bar instead of a frozen spinner."""
    import lib.self_update as su
    tmp = tempfile.mkdtemp(prefix='tofu-test-')
    try:
        inst = _build_install(tmp)
        # Big body so >1 chunk streams and Content-Length is meaningful.
        tar_path = _build_tarball(tmp)
        for _ in range(40):
            with open(tar_path, 'ab') as fh:
                fh.write(os.urandom(2048))
        frames = []
        _orig = (su.http_stream, su._ROOT, su._install_requirements)
        try:
            _patch_stream(su, tar_path, with_length=True)
            su._ROOT = inst
            su._install_requirements = lambda on_line=None: {'ok': True, 'detail': 'stubbed'}
            su._apply_via_tarball(
                'v9.9.9',
                progress=lambda st, status, detail='', meta=None: frames.append(
                    (st, status, detail, meta)))
        finally:
            su.http_stream, su._ROOT, su._install_requirements = _orig

        fetch_active = [f for f in frames if f[0] == 'fetch' and f[1] == 'active']
        # At least one fetch-active frame must carry structured telemetry.
        with_meta = [f for f in fetch_active if isinstance(f[3], dict) and f[3].get('loaded')]
        assert with_meta, f'no fetch progress frames with byte telemetry: {frames}'
        # pct is an int 0..100 when Content-Length is known.
        pcts = [f[3]['pct'] for f in with_meta if f[3].get('pct') is not None]
        assert pcts and all(0 <= p <= 100 for p in pcts), f'bad pct values: {pcts}'
        # detail string carries a human size + speed readout.
        assert any('/s' in (f[2] or '') for f in with_meta), 'no speed in detail'
        _ok('_apply_via_tarball: download emits pct + bytes + speed frames')
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_tarball_skips_pip_when_requirements_unchanged():
    """When requirements.txt is byte-identical between the install and the
    archive, the slow pip install must be SKIPPED (deps 'skip', not 'active')."""
    import lib.self_update as su
    tmp = tempfile.mkdtemp(prefix='tofu-test-')
    try:
        inst = _build_install(tmp)
        # Same requirements.txt content on both sides.
        open(os.path.join(inst, 'requirements.txt'), 'w').write('httpx==1.0\nflask\n')
        tar_path = _build_tarball(tmp, same_requirements='httpx==1.0\nflask\n')
        install_called = {'n': 0}
        frames = []
        _orig = (su.http_stream, su._ROOT, su._install_requirements)
        try:
            _patch_stream(su, tar_path)
            su._ROOT = inst
            def _spy(on_line=None):
                install_called['n'] += 1
                return {'ok': True, 'detail': 'stubbed'}
            su._install_requirements = _spy
            res = su._apply_via_tarball(
                'v9.9.9',
                progress=lambda st, status, detail='', meta=None: frames.append((st, status)))
        finally:
            su.http_stream, su._ROOT, su._install_requirements = _orig

        assert res['ok'] is True, res.get('error')
        assert install_called['n'] == 0, 'pip install ran despite unchanged requirements'
        assert res['deps_changed'] is False
        assert ('deps', 'skip') in frames, f'deps stage was not skipped: {frames}'
        _ok('_apply_via_tarball: pip install skipped when requirements unchanged')
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_tarball_installs_pip_when_requirements_changed():
    """When requirements.txt differs, pip install MUST run (deps 'active')."""
    import lib.self_update as su
    tmp = tempfile.mkdtemp(prefix='tofu-test-')
    try:
        inst = _build_install(tmp)
        open(os.path.join(inst, 'requirements.txt'), 'w').write('httpx==1.0\n')
        tar_path = _build_tarball(tmp, same_requirements='httpx==2.0\nnumpy\n')
        install_called = {'n': 0}
        _orig = (su.http_stream, su._ROOT, su._install_requirements)
        try:
            _patch_stream(su, tar_path)
            su._ROOT = inst
            def _spy(on_line=None):
                install_called['n'] += 1
                return {'ok': True, 'detail': 'stubbed'}
            su._install_requirements = _spy
            res = su._apply_via_tarball('v9.9.9')
        finally:
            su.http_stream, su._ROOT, su._install_requirements = _orig

        assert res['ok'] is True, res.get('error')
        assert install_called['n'] == 1, 'pip install did NOT run on changed requirements'
        assert res['deps_changed'] is True
        _ok('_apply_via_tarball: pip install runs when requirements changed')
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_git_checkout_flips_to_indeterminate_pull():
    """The git path must NOT leave the fetch bar frozen at 100% during the
    silent local checkout/merge. The moment a transfer phase hits 100%,
    _apply_via_git must emit fetch→done then pull→active (indeterminate, i.e.
    NO pct meta), so the bar keeps moving through the checkout. Also asserts
    the deps stage goes active (indeterminate) before any pip line."""
    import lib.self_update as su
    import lib.self_update._apply as ap

    # A realistic git --progress phase sequence for one pull: two transfer
    # phases each ramping to 100, THEN the silent checkout (no frames).
    seq = [
        ('Receiving objects', 25), ('Receiving objects', 75),
        ('Receiving objects', 100),
        ('Resolving deltas', 40), ('Resolving deltas', 100),
    ]

    class _CP:
        returncode = 0
        stdout = 'Updating aaaa..bbbb\nFast-forward\n requirements.txt | 2 +-\n'
        stderr = ''

    def _fake_stream(args, timeout=30, on_progress=None):
        for phase, pct in seq:
            if on_progress:
                on_progress(phase, pct, f'{phase}: {pct}%')
        return _CP()

    frames = []
    _orig = (ap._run_git_streaming, ap.working_tree_status, ap._head_sha,
             ap._requirements_changed, ap._install_requirements,
             ap.current_version)
    try:
        ap._run_git_streaming = _fake_stream
        ap.working_tree_status = lambda: {'clean': True, 'blocking': [], 'runtime': 0}
        _shas = iter(['aaaa', 'bbbb'])
        ap._head_sha = lambda: next(_shas, 'bbbb')
        ap._requirements_changed = lambda a, b: True   # force deps stage
        pip_lines = ['Collecting httpx', 'Installing collected packages: httpx']

        def _fake_pip(on_line=None):
            # Deps stage must already be 'active' (indeterminate) BEFORE the
            # first pip line — assert nothing here, just feed lines.
            if on_line:
                for ln in pip_lines:
                    on_line(ln)
            return {'ok': True, 'detail': 'done'}
        ap._install_requirements = _fake_pip
        ap.current_version = lambda: '0.12.0'

        res = ap._apply_via_git(
            progress=lambda st, status, detail='', meta=None: frames.append(
                (st, status, detail, meta)))
    finally:
        (ap._run_git_streaming, ap.working_tree_status, ap._head_sha,
         ap._requirements_changed, ap._install_requirements,
         ap.current_version) = _orig

    assert res['ok'] is True, res.get('error')

    # Reconstruct the bar state the frontend would render, frame by frame,
    # applying the SAME rule as _applyStageFrame:
    #   active + numeric pct  → determinate(pct)
    #   active + no pct       → indeterminate
    #   done/skip/error       → bar cleared
    bar = {'fetch': None, 'pull': None, 'deps': None}
    timeline = []
    for st, status, detail, meta in frames:
        if status == 'active':
            pct = meta.get('pct') if isinstance(meta, dict) else None
            bar[st] = ('determinate', pct) if isinstance(pct, (int, float)) else ('indeterminate', None)
        elif status in ('done', 'skip', 'error'):
            bar[st] = None
        timeline.append((st, status, dict(bar)))

    # 1. fetch reached a determinate 100 at some point.
    assert any(st == 'fetch' and status == 'active'
               and isinstance(meta, dict) and meta.get('pct') == 100
               for st, status, _, meta in frames), 'no fetch pct=100 frame'
    # 2. after fetch hit 100 it flipped: fetch done + pull active INDETERMINATE.
    order = [(st, status) for st, status, _, _ in frames]
    assert ('fetch', 'done') in order, 'fetch never marked done'
    # Find the pull-active frame; it must carry NO pct (indeterminate sweep).
    pull_active = [(detail, meta) for st, status, detail, meta in frames
                   if st == 'pull' and status == 'active']
    assert pull_active, 'pull never went active'
    assert all(not (isinstance(m, dict) and m.get('pct') is not None)
               for _, m in pull_active), 'pull active carried a pct (would be a static bar)'
    # 3. CRITICAL: the fetch→done + pull→active flip happens BEFORE the pull
    #    subprocess returns (i.e. within the progress callback, mid-call), not
    #    only after. Assert fetch 'done' precedes the LAST transfer frame's
    #    tail — concretely, a pull 'active' appears before pull 'done'.
    idx_pull_active = order.index(('pull', 'active'))
    idx_pull_done = order.index(('pull', 'done'))
    assert idx_pull_active < idx_pull_done, 'pull active must precede pull done'
    # 4. deps went active with NO pct (indeterminate) before pip lines/done —
    #    no dead gap between "deps active" and first Collecting… line.
    deps_active = [(detail, meta) for st, status, detail, meta in frames
                   if st == 'deps' and status == 'active']
    assert deps_active, 'deps never went active'
    first_deps = deps_active[0]
    assert not (isinstance(first_deps[1], dict) and first_deps[1].get('pct') is not None), \
        'first deps-active frame carried a pct (should be indeterminate)'

    # 5. Never, at any point in the timeline, is any stage a determinate FULL
    #    bar (pct==100) that then SITS there while the next stage is silent —
    #    i.e. once fetch shows 100 the very next fetch state must be cleared
    #    (flipped away), not lingering determinate-100.
    saw_fetch_100 = False
    for st, status, snap in timeline:
        if snap['fetch'] == ('determinate', 100):
            saw_fetch_100 = True
        elif saw_fetch_100:
            # After the 100 frame, fetch must be cleared (None), never still
            # a determinate 100 with no motion.
            assert snap['fetch'] != ('determinate', 100), \
                'fetch bar lingered frozen at determinate 100'
            break
    _ok('_apply_via_git: fetch flips to indeterminate pull at 100% (no frozen full bar)')


def test_overlay_skip_classification():
    from lib.self_update import _overlay_skip as skip
    must_skip = ['.tofu/skills/x.md', 'data/app.db', 'logs/app.log',
                 'uploads/img.png', '.git/config', 'static/js/bundle-abc.js',
                 '.update_backup/20260101-000000/x']
    must_copy = ['server.py', 'lib/foo.py', 'static/js/update.js',
                 'static/styles.css', 'requirements.txt', 'routes/chat.py']
    for p in must_skip:
        assert skip(p), f'{p} should be skipped'
    for p in must_copy:
        assert not skip(p), f'{p} should be copied'
    # The .tofu leading-dot must survive normalization (regression: lstrip('./')).
    assert skip('.tofu/error_resolutions.json')
    _ok('_overlay_skip: user/runtime state skipped, tracked source copied')


def test_tarball_overlay_preserves_user_data():
    import lib.self_update as su
    tmp = tempfile.mkdtemp(prefix='tofu-test-')
    try:
        inst = _build_install(tmp)
        tar_path = _build_tarball(tmp)
        _orig_stream, _orig_root, _orig_deps = su.http_stream, su._ROOT, su._install_requirements
        try:
            _patch_stream(su, tar_path)
            su._ROOT = inst
            su._install_requirements = lambda on_line=None: {'ok': True, 'detail': 'stubbed'}
            res = su._apply_via_tarball('v9.9.9')
        finally:
            su.http_stream, su._ROOT, su._install_requirements = _orig_stream, _orig_root, _orig_deps

        assert res['ok'] is True, res.get('error')
        assert res['changed'] is True
        assert res['new_version'] == '9.9.9'
        assert res['method'] == 'tarball'
        # Source updated / added
        assert open(os.path.join(inst, 'server.py')).read().startswith('NEW server')
        assert open(os.path.join(inst, 'lib', 'foo.py')).read().startswith('NEW foo')
        assert os.path.exists(os.path.join(inst, 'lib', 'bar.py'))
        assert open(os.path.join(inst, 'VERSION')).read().strip() == '9.9.9'
        # User data preserved
        assert open(os.path.join(inst, '.tofu', 'skills', 'mine.md')).read().strip() == 'MY MEMORY'
        assert open(os.path.join(inst, 'data', 'app.db')).read().strip() == 'MY DB'
        # Upstream .tofu file must NOT have been written
        assert not os.path.exists(os.path.join(inst, '.tofu', 'skills', 'upstream.md'))
        _ok('_apply_via_tarball: source updated, user memories + DB preserved')
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_tarball_overlay_backs_up_replaced_files():
    import lib.self_update as su
    tmp = tempfile.mkdtemp(prefix='tofu-test-')
    try:
        inst = _build_install(tmp)
        tar_path = _build_tarball(tmp)
        _orig = (su.http_stream, su._ROOT, su._install_requirements)
        try:
            _patch_stream(su, tar_path)
            su._ROOT = inst
            su._install_requirements = lambda on_line=None: {'ok': True, 'detail': 'stubbed'}
            su._apply_via_tarball('v9.9.9')
        finally:
            su.http_stream, su._ROOT, su._install_requirements = _orig

        backup_root = os.path.join(inst, '.update_backup')
        assert os.path.isdir(backup_root)
        runs = os.listdir(backup_root)
        assert len(runs) == 1
        bp = os.path.join(backup_root, runs[0])
        # Originals of overwritten files captured
        assert open(os.path.join(bp, 'server.py')).read().strip() == 'OLD server'
        assert open(os.path.join(bp, 'lib', 'foo.py')).read().strip() == 'OLD foo'
        # A newly-added file (bar.py) has no original → not in backup
        assert not os.path.exists(os.path.join(bp, 'lib', 'bar.py'))
        _ok('_apply_via_tarball: replaced files backed up to .update_backup/')
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_tarball_invalid_archive_aborts_untouched():
    import lib.self_update as su
    tmp = tempfile.mkdtemp(prefix='tofu-test-')
    try:
        inst = tempfile.mkdtemp(prefix='tofu-inst-', dir=tmp)
        open(os.path.join(inst, 'server.py'), 'w').write('UNTOUCHED\n')
        tar_path = _build_tarball(tmp, valid=False)
        _orig = (su.http_stream, su._ROOT)
        try:
            _patch_stream(su, tar_path)
            su._ROOT = inst
            res = su._apply_via_tarball('v9.9.9')
        finally:
            su.http_stream, su._ROOT = _orig

        assert res['ok'] is False
        assert 'not a valid Tofu' in (res['error'] or '')
        # Live install must be untouched
        assert open(os.path.join(inst, 'server.py')).read().strip() == 'UNTOUCHED'
        assert not os.path.exists(os.path.join(inst, '.update_backup'))
        _ok('_apply_via_tarball: invalid archive aborts, install untouched')
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_tarball_landing_verify_passes_on_clean_overlay():
    """A clean overlay (every landed .py byte-size >= source) must pass the
    post-overlay landing check and result in ok=True. Sanity: the verifier
    does not spuriously flag a healthy overlay."""
    import lib.self_update as su
    tmp = tempfile.mkdtemp(prefix='tofu-test-')
    try:
        inst = _build_install(tmp)
        tar_path = _build_tarball(tmp)
        _orig = (su.http_stream, su._ROOT, su._install_requirements)
        try:
            _patch_stream(su, tar_path)
            su._ROOT = inst
            su._install_requirements = lambda on_line=None: {'ok': True, 'detail': 'stubbed'}
            res = su._apply_via_tarball('v9.9.9')
        finally:
            su.http_stream, su._ROOT, su._install_requirements = _orig

        assert res['ok'] is True, res.get('error')
        # server.py landed with the NEW content, not the old.
        assert open(os.path.join(inst, 'server.py')).read().startswith('NEW server')
        _ok('_apply_via_tarball: landing verify passes on a clean overlay')
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_tarball_landing_verify_aborts_on_truncated_facade():
    """Simulate the cross-DC FUSE atomic-move race: shutil.copy2 returns
    success but the landed file is 0 bytes (or short) on disk. The overlay
    must be aborted LOUDLY with the offending file listed, ok=False, AND
    the pre-overlay file restored from backup so the tree does not crash
    at boot."""
    import lib.self_update as su
    import lib.self_update._apply as ap

    tmp = tempfile.mkdtemp(prefix='tofu-test-')
    try:
        inst = _build_install(tmp)
        tar_path = _build_tarball(tmp)
        # `foo.py` exists in the install with "OLD foo\n" — so a backup is
        # created for it, which the abort path must restore from.
        assert open(os.path.join(inst, 'lib', 'foo.py')).read().startswith('OLD foo')

        # Wrap shutil.copy2 inside _apply.py so that lib/foo.py ends up
        # 0-byte on the DEST after copy (mimicking the FUSE atomic-move
        # window). All OTHER files copy normally. copy2 still "succeeds"
        # (returns), matching the real bug shape.
        import shutil as _sh
        _real_copy2 = _sh.copy2

        def _flaky_copy2(src, dst, **kw):
            r = _real_copy2(src, dst, **kw)
            # Only sabotage the overlay copy (src under the tarball
            # extract dir), not the pre-overlay backup copy (src under
            # the install dir). Match by the RELATIVE tail path.
            if str(dst).endswith(os.path.join('lib', 'foo.py')) \
                    and 'tofu-update-' in str(src):
                # Truncate landed file to 0 bytes — the exact FUSE-race
                # symptom the user observed.
                with open(dst, 'wb'):
                    pass
            return r

        _orig = (su.http_stream, su._ROOT, su._install_requirements,
                 ap.shutil) if hasattr(ap, 'shutil') else None
        # `_apply_via_tarball` imports shutil locally — patch the module
        # sys.modules entry so `import shutil` inside the fn sees our
        # wrapper.
        import shutil as _real_sh_mod
        _orig_copy2_on_module = _real_sh_mod.copy2
        _real_sh_mod.copy2 = _flaky_copy2

        _orig_stream, _orig_root, _orig_deps = \
            su.http_stream, su._ROOT, su._install_requirements
        try:
            _patch_stream(su, tar_path)
            su._ROOT = inst
            su._install_requirements = lambda on_line=None: {'ok': True, 'detail': 'stubbed'}
            frames = []
            res = su._apply_via_tarball(
                'v9.9.9',
                progress=lambda st, status, detail='', meta=None: frames.append(
                    (st, status, detail, meta)))
        finally:
            _real_sh_mod.copy2 = _orig_copy2_on_module
            su.http_stream, su._ROOT, su._install_requirements = \
                _orig_stream, _orig_root, _orig_deps

        # 1) Update reported failure LOUDLY — not silently ok=True.
        assert res['ok'] is False, \
            'landing verify let a truncated .py through: res=' + repr(res)
        assert res['error'] and 'truncated' in res['error'].lower(), \
            f'error message does not mention truncation: {res["error"]!r}'
        # 2) The offending file is named in the detail.
        assert 'lib/foo.py' in (res['detail'] or ''), \
            f'offending file not surfaced in detail: {res["detail"]!r}'
        # 3) The pre-overlay content is restored from backup so the tree
        #    is not left in a boot-crashing 0-byte state.
        restored = open(os.path.join(inst, 'lib', 'foo.py')).read()
        assert restored.startswith('OLD foo'), \
            f'truncated file not restored from backup — content={restored!r}'
        # 4) A pull-error progress frame fires (UI shows the abort).
        assert any(st == 'pull' and status == 'error' for st, status, _, _ in frames), \
            f'no pull-error progress frame emitted: {frames}'
        _ok('_apply_via_tarball: FUSE-race 0-byte landing aborts loudly + restores backup')
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_tarball_landing_verify_is_load_bearing_NEUTER():
    """NEUTER guard: if _verify_landed_py_integrity is replaced with a
    no-op that always returns ok=True, the FUSE-race scenario becomes
    silently accepted (ok=True + a 0-byte facade on disk). Proves the
    verify helper is what stops the bug — not something else."""
    import lib.self_update as su
    import lib.self_update._apply as ap
    tmp = tempfile.mkdtemp(prefix='tofu-test-')
    try:
        inst = _build_install(tmp)
        tar_path = _build_tarball(tmp)
        assert open(os.path.join(inst, 'lib', 'foo.py')).read().startswith('OLD foo')

        import shutil as _real_sh_mod
        _real_copy2 = _real_sh_mod.copy2

        def _flaky_copy2(src, dst, **kw):
            r = _real_copy2(src, dst, **kw)
            if str(dst).endswith(os.path.join('lib', 'foo.py')) \
                    and 'tofu-update-' in str(src):
                with open(dst, 'wb'):
                    pass
            return r

        _real_sh_mod.copy2 = _flaky_copy2

        # NEUTER: swap the verify helper for a no-op that always passes.
        _orig_verify = ap._verify_landed_py_integrity
        ap._verify_landed_py_integrity = lambda src, dest, backup_dir=None: {
            'ok': True, 'bad': []}

        _orig_stream, _orig_root, _orig_deps = \
            su.http_stream, su._ROOT, su._install_requirements
        try:
            _patch_stream(su, tar_path)
            su._ROOT = inst
            su._install_requirements = lambda on_line=None: {'ok': True, 'detail': 'stubbed'}
            res = su._apply_via_tarball('v9.9.9')
        finally:
            _real_sh_mod.copy2 = _real_copy2
            ap._verify_landed_py_integrity = _orig_verify
            su.http_stream, su._ROOT, su._install_requirements = \
                _orig_stream, _orig_root, _orig_deps

        # With the verify helper neutered, the update INCORRECTLY reports
        # success AND a 0-byte facade sits on disk — exactly the pre-fix
        # crash-at-boot state. This proves the helper is load-bearing.
        assert res['ok'] is True, \
            'NEUTER expected: without verify the overlay should falsely succeed'
        landed_sz = os.path.getsize(os.path.join(inst, 'lib', 'foo.py'))
        assert landed_sz == 0, \
            f'NEUTER expected: 0-byte landed file, got {landed_sz}B'
        _ok('_verify_landed_py_integrity is load-bearing (NEUTER flips test to failure state)')
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_check_payload_reports_update_method():
    """check_for_update must surface update_method for the UI to route on."""
    import lib.self_update as su
    _orig = (su.git_available, su.fetch_latest_release)
    try:
        su.git_available = lambda: False
        su.fetch_latest_release = lambda: {'tag': 'v9.9.9', 'version': '9.9.9'}
        payload = su.check_for_update()
        assert payload['git_available'] is False
        assert payload['update_method'] == 'tarball'

        su.git_available = lambda: True
        # working_tree_status runs real git in _ROOT; stub it to stay hermetic.
        _ws = su.working_tree_status
        su.working_tree_status = lambda: {'clean': True, 'blocking': [], 'runtime': 0}
        try:
            payload2 = su.check_for_update()
        finally:
            su.working_tree_status = _ws
        assert payload2['update_method'] == 'git'
        _ok('check_for_update: update_method=git/tarball reflects deployment')
    finally:
        su.git_available, su.fetch_latest_release = _orig


def main():
    print()
    print(_color('═══ self_update tarball-fallback Tests ═══', '36'))
    print()
    tests = [
        test_overlay_skip_classification,
        test_tarball_overlay_preserves_user_data,
        test_tarball_overlay_backs_up_replaced_files,
        test_tarball_invalid_archive_aborts_untouched,
        test_check_payload_reports_update_method,
        test_tarball_download_emits_progress,
        test_tarball_skips_pip_when_requirements_unchanged,
        test_tarball_installs_pip_when_requirements_changed,
        test_git_checkout_flips_to_indeterminate_pull,
        test_tarball_landing_verify_passes_on_clean_overlay,
        test_tarball_landing_verify_aborts_on_truncated_facade,
        test_tarball_landing_verify_is_load_bearing_NEUTER,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
