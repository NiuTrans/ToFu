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


def _build_tarball(tmp, version='9.9.9', valid=True):
    """Build a GitHub-style tarball (single wrapper dir). When valid, it
    carries the sentinels; otherwise it's a non-Tofu tree."""
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
    else:
        os.makedirs(wrap)
        open(os.path.join(wrap, 'README.md'), 'w').write('not tofu' + _incompressible())
    tar_path = os.path.join(stage, 'rel.tar.gz')
    with tarfile.open(tar_path, 'w:gz') as tf:
        tf.add(wrap, arcname='rangehow-ToFu-deadbeef')
    return tar_path


def _patch_stream(su, tar_path):
    """Point su.http_stream at a local tarball file."""
    class _Resp:
        status_code = 200
        def __init__(self, p): self._p = p
        def iter_content(self, n):
            with open(self._p, 'rb') as fh:
                while True:
                    c = fh.read(n)
                    if not c:
                        break
                    yield c
    @contextlib.contextmanager
    def _fake(method, url, **kw):
        yield _Resp(tar_path)
    su.http_stream = _fake


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
            su._install_requirements = lambda: {'ok': True, 'detail': 'stubbed'}
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
            su._install_requirements = lambda: {'ok': True, 'detail': 'stubbed'}
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
