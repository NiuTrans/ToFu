"""Trusted repository capacity follows issued launch policy without new owners."""
from dataclasses import FrozenInstanceError
import sys

import pytest

from lib.repo_map import outline
from tofu_agent import NativeRepositoryConfiguration

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('capacity', [1 << 20, 128 << 20, 2 << 30])
def test_repository_limits_scale_from_existing_policy_and_keep_source_admission(tmp_path, capacity):
    policy = {'frame_maximum': capacity}
    configuration = NativeRepositoryConfiguration.from_launch_policy(
        grammar_directory=str(tmp_path), launch_policy=policy)
    assert configuration.limits[:3] == (capacity, capacity, capacity)
    assert configuration.limits[3] == max(outline._MAX_SOURCE_BYTES, outline._MAX_REF_BYTES * 4 + 4)
    policy['frame_maximum'] = 1
    assert configuration.limits[0] == capacity
    with pytest.raises(FrozenInstanceError): configuration.limits = (1, 1, 1, 1)


@pytest.mark.parametrize('policy', [None, [], {}, {'frame_maximum': True},
    {'frame_maximum': 0}, {'frame_maximum': -1}, {'frame_maximum': 1.0},
    {'frame_maximum': sys.maxsize}, {'frame_maximum': 1024}])
def test_repository_capacity_refuses_invalid_or_inadequate_issued_policy(tmp_path, policy):
    with pytest.raises(ValueError, match='launch capacity'):
        NativeRepositoryConfiguration.from_launch_policy(grammar_directory=str(tmp_path), launch_policy=policy)


def test_repository_configuration_reuses_policy_and_keeps_explicit_overrides(tmp_path, monkeypatch):
    import runtime_guards
    def reprobe(*args, **kwargs): raise AssertionError('configuration must consume the issued probe')
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', reprobe)
    configuration = NativeRepositoryConfiguration.from_launch_policy(
        grammar_directory=str(tmp_path), launch_policy={'frame_maximum': 128 << 20}, timeout=12)
    runtime = configuration.create_runtime()
    try:
        assert runtime.retained_image_bytes() == 0
        assert runtime.timeout == 12 and runtime.limits.arguments() == configuration.limits
    finally: runtime.close()
    explicit = NativeRepositoryConfiguration(str(tmp_path), (8 << 20, 8 << 20, 32 << 20, 16 << 20), timeout=7)
    assert explicit.limits == (8 << 20, 8 << 20, 32 << 20, 16 << 20) and explicit.timeout == 7
