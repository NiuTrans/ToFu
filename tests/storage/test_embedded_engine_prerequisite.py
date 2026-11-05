"""Missing embedded artifacts use the certified engine prerequisite boundary."""
import pytest

from lib.storage import supervisor
from serverctl_pkg.native_engine import EnginePreparationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('source_checkout', [False, True])
def test_missing_library_reports_typed_pair_refusal(tmp_path, monkeypatch, source_checkout):
    binary = tmp_path / 'engine' / 'tofu-db'
    binary.parent.mkdir()
    binary.write_bytes(b'not-executed')
    checkout = tmp_path / 'checkout'
    if source_checkout:
        crate = checkout / 'packages/tofu-db'
        crate.mkdir(parents=True)
        (crate / 'build.rs').write_text('// source witness fixture')
    monkeypatch.setattr(supervisor, '_resolve_tofudb_binary', lambda: str(binary))
    monkeypatch.setattr(supervisor, '_tofudb_checkout_root', lambda: checkout)
    with pytest.raises(EnginePreparationError, match='engine pair is incomplete or mismatched'):
        supervisor._resolve_tofudb_embedded_engine()
