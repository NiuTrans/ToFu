"""Executable self-checks read the real pipes owned by their child process."""
import pytest

pytestmark = pytest.mark.unit


def test_native_self_check_accepts_real_binary_pipe_streams(tmp_path):
    from lib.agent_core.native_artifact import _self_check
    script = tmp_path / 'identity.sh'
    script.write_text("printf '%s' '{\"identity\":\"verified\"}'\n")
    with open('/bin/sh', 'rb') as image:
        assert _self_check(image, str(script), tmp_path, 5, 1024) == {'identity': 'verified'}
