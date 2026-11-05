"""One git process observes the checkout and its HEAD for an update check.

``git rev-parse --git-dir HEAD`` answers what ``git rev-parse --git-dir``
(is this a checkout?) and ``git rev-parse HEAD`` (which commit?) answered
separately. Each spawned git costs several GIL hand-offs on a loaded server;
the update check spends one rev-parse plus the fingerprint diff.
"""
import asyncio
import subprocess
import time

import pytest

import lib.self_update as su
from lib import boot_identity
from lib.self_update import _git, _status

pytestmark = pytest.mark.unit

_GIT_IDENTITY = ['-c', 'user.name=Update Check', '-c', 'user.email=update@example.test']


def _repository(path, *, commit):
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    if not commit:
        return None
    subprocess.run(['git', '-C', str(path), *_GIT_IDENTITY, 'commit', '-q',
                    '--allow-empty', '-m', 'release'], check=True)
    return subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.parametrize('state', ['committed', 'unborn', 'not_a_repository'])
def test_observation_matches_the_separate_probes(tmp_path, monkeypatch, state):
    root = tmp_path / 'checkout'
    root.mkdir()
    if state != 'not_a_repository':
        _repository(root, commit=state == 'committed')
    monkeypatch.setattr(_git, '_ROOT', str(root))

    separate_discovery = _git._run_git(['rev-parse', '--git-dir']).returncode == 0
    observation = _git.observe_repository()
    assert observation.discovered is separate_discovery
    assert observation.head == _git._head_sha()
    assert _git.git_available(observation) is _git.git_available()


def test_missing_git_observes_no_checkout(monkeypatch):
    def missing(args, timeout=30):
        raise FileNotFoundError('git')
    monkeypatch.setattr(_git, '_run_git', missing)
    assert _git.observe_repository() == _git.RepositoryObservation(discovered=False, head=None)


def test_observation_never_overrides_a_missing_git_directory(tmp_path, monkeypatch):
    """A linked worktree's ``.git`` file still reports no checkout to update."""
    monkeypatch.setattr(_git, '_ROOT', str(tmp_path))
    observation = _git.RepositoryObservation(discovered=True, head='a' * 40)
    assert _git.git_available(observation) is False


def test_update_check_spends_one_rev_parse_and_one_fingerprint_diff(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    head = _repository(root, commit=True)
    monkeypatch.setattr(_git, '_ROOT', str(root))
    commands = []
    real_run = _git._run_git

    def counting(args, timeout=30):
        commands.append(tuple(args))
        return real_run(args, timeout)

    monkeypatch.setattr(_git, '_run_git', counting)
    monkeypatch.setattr(_status, '_run_git', counting)
    monkeypatch.setattr(boot_identity, '_FINGERPRINT_CACHE', None)
    boot_identity.code_fingerprint()  # Frozen once per process, before any check.
    # The release lookup and tree status keep their own cached probes.
    monkeypatch.setattr(_status, '_latest_release_cache', {
        'at': time.monotonic(), 'value': {'tag': 'v0.0.1', 'version': '0.0.1'}, 'err': None})
    monkeypatch.setattr(_status, '_tree_status_cache', {
        'at': time.monotonic(), 'value': {'clean': True, 'blocking': [], 'runtime': 0}})
    monkeypatch.setattr(su, 'current_version', lambda: '0.0.1')
    commands.clear()

    from quart import Quart, g
    import routes.api_v1.update as update_route
    monkeypatch.setattr(update_route, '_apply_state_path', lambda: str(tmp_path / 'apply.json'))
    app = Quart(__name__)
    app.register_blueprint(update_route.api_v1_update_bp)

    @app.before_request
    async def _principal():
        from lib.api_keys._context import local_admin_context
        from routes.api_v1.auth import _stamp_principal
        g.auth_ctx = local_admin_context()
        _stamp_principal()

    async def go():
        response = await app.test_client().get('/api/v1/update/check')
        return response.status_code, await response.get_json()

    code, body = asyncio.run(go())
    assert code == 200, body
    assert body['git_available'] is True and body['update_method'] == 'git'
    assert body['code_pending'] == {'pending': False, 'loaded_head': head[:12],
                                    'current_head': head[:12], 'dirty': False}
    assert commands == [('rev-parse', '--git-dir', 'HEAD'), ('diff', 'HEAD')]
