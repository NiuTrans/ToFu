"""Root-registry admission holds the process-wide config lock for map updates only.

Every project tool admission re-registers its task's root policy and status
polls read the registry, so filesystem containment probes and Git/cross-DC
status probes must run while other threads can take ``config._lock``.
"""
import threading

import pytest

from lib.project_mod import config

pytestmark = pytest.mark.unit


def _lock_free_for_other_threads():
    acquired = []

    def probe():
        if config._lock.acquire(timeout=2):
            config._lock.release()
            acquired.append(True)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(5)
    return acquired == [True]


@pytest.fixture
def key():
    name = 'root-registry-lock-io'
    yield name
    config.clear_conv_state(name)


def test_sticky_cwd_containment_is_probed_without_config_lock(tmp_path, monkeypatch, key):
    old, new = tmp_path / 'old', tmp_path / 'new'
    (old / 'pkg').mkdir(parents=True)
    new.mkdir()
    config.register_root_policy(key, config.build_root_policy(str(old)))
    assert config.set_conv_cwd(key, str(old / 'pkg'))
    probes = []
    original = config._cwd_within_conv_roots

    def probe(conv_id, cwd):
        probes.append(_lock_free_for_other_threads())
        return original(conv_id, cwd)

    monkeypatch.setattr(config, '_cwd_within_conv_roots', probe)
    policy = config.build_root_policy(str(new))
    config.register_root_policy(key, policy)
    assert probes == [True]
    assert config.get_conv_cwd(key) is None  # The old cwd left the registered roots.

    assert config.set_conv_cwd(key, str(new))
    probes.clear()
    # Each later admission re-registers the same task-owned policy object,
    # whose roots only grow in place: no filesystem walk, sticky cwd kept.
    config.register_root_policy(key, policy)
    assert probes == []
    assert config.get_conv_cwd(key) == str(new)


def test_project_state_probes_run_without_config_lock(tmp_path, monkeypatch, key):
    from lib import cross_dc
    root = tmp_path / 'repo'
    root.mkdir()
    probes = []

    def attach(state, primary):
        probes.append(('git', _lock_free_for_other_threads()))
        return state

    def latency_class(path):
        probes.append(('cross_dc', _lock_free_for_other_threads()))
        return 'unknown'

    monkeypatch.setattr(config, '_attach_git_branches', attach)
    monkeypatch.setattr(cross_dc, 'get_latency_class', latency_class)
    config.register_root_policy(key, config.build_root_policy(str(root)))
    assert config.get_state_for_conv(key)['path'] == str(root)
    assert config.get_state_for_conv(key + '-cold', held_roots=(str(root),))['path'] == str(root)
    config.get_state()
    assert probes[:3] == [('cross_dc', True), ('git', True), ('git', True)]
    assert all(free for _, free in probes) and probes[-1][0] == 'git'
