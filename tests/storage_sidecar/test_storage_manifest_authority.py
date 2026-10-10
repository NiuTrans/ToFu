"""An unreadable authority manifest never grants permission to use older data."""
import pytest

from lib.storage_sidecar import fastpath

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('payload', [b'{truncated', b'[]', b'null', b'\xff', b' ' * 65537],
                         ids=['truncated', 'array', 'null', 'encoding', 'oversized'])
def test_corrupt_shadow_refuses_classic_authority(tmp_path, payload):
    shadow = tmp_path / fastpath.SHADOW_DIRNAME
    shadow.mkdir()
    manifest = shadow / fastpath.SHADOW_MANIFEST_NAME
    manifest.write_bytes(payload)
    classic = tmp_path / 'tofu.db'
    classic.write_bytes(b'old authority remains untouched')
    with pytest.raises(RuntimeError, match='manifest'):
        fastpath.require_classic_authority_is_current(tmp_path)
    assert manifest.read_bytes() == payload
    assert classic.read_bytes() == b'old authority remains untouched'


@pytest.mark.parametrize('kind', ['directory', 'dangling-symlink'])
def test_nonregular_manifest_is_not_treated_as_absent(tmp_path, kind):
    manifest = tmp_path / fastpath.LOCAL_MANIFEST_NAME
    if kind == 'directory':
        manifest.mkdir()
    else:
        manifest.symlink_to(tmp_path / 'missing')
    with pytest.raises(RuntimeError, match='manifest'):
        fastpath.read_local_manifest(tmp_path)


def test_only_absent_manifest_permits_classic_authority(tmp_path):
    fastpath.require_classic_authority_is_current(tmp_path)
    assert fastpath.read_local_manifest(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_manifest_read_failure_refuses_fallback_without_mutating_files(tmp_path, monkeypatch):
    from pathlib import Path
    shadow = tmp_path / fastpath.SHADOW_DIRNAME
    shadow.mkdir()
    manifest = shadow / fastpath.SHADOW_MANIFEST_NAME
    manifest.write_text('{"authority_uuid":"current"}')
    original_open = Path.open
    def fail_read(path, *args, **kwargs):
        if path == manifest:
            raise PermissionError('injected filesystem denial')
        return original_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', fail_read)
        with pytest.raises(RuntimeError, match='refusing authority fallback'):
            fastpath.require_classic_authority_is_current(tmp_path)
    assert manifest.read_text() == '{"authority_uuid":"current"}'
