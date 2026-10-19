"""Python projection admission and native search share generated volume geometry."""
import ast
import json
from pathlib import Path
import sys

import pytest

from scripts import gen_tofudb_ir_contract as generator

pytestmark = pytest.mark.unit


def generate_search(tmp_path, monkeypatch, policy):
    search = json.loads(generator.SEARCH_SOURCE.read_text())
    if policy is None:
        search['nativeProjection'].pop('volumeGuard', None)
    else:
        search['nativeProjection']['volumeGuard'] = policy
    source = tmp_path / 'search.json'
    source.write_text(json.dumps(search))
    monkeypatch.setattr(generator, 'SEARCH_SOURCE', source)
    monkeypatch.setattr(sys, 'argv', ['gen_tofudb_ir_contract'])
    outputs = {}

    def capture(path, content, **_kwargs):
        outputs[path] = content
        return len(content)

    # Capture every generator publication; tests never overwrite shipped files.
    monkeypatch.setattr(Path, 'write_text', capture)
    assert generator.main() == 0
    assignments = ast.parse(outputs[generator.MIGRATION_OUTPUT]).body
    policy_assignment = next(node for node in assignments if isinstance(node, ast.Assign)
                             and node.targets[0].id == 'NATIVE_SEARCH_VOLUME_GUARD')
    return ast.literal_eval(policy_assignment.value), outputs[generator.SEARCH_OUTPUT]


def volume_policy():
    return {
        'bytesReserveDivisor': 1001, 'minimumBytesReserve': 65536,
        'maximumBytesReserve': 1048576, 'inodesReserveDivisor': 1002,
        'minimumInodesReserve': 4, 'maximumInodesReserve': 100,
        'maximumReserveFractionDivisor': 10,
        'accounting': 'independent-known-dimensions',
    }


def test_python_and_rust_share_declared_search_volume_geometry(tmp_path, monkeypatch):
    policy = volume_policy()
    generated, rust = generate_search(tmp_path, monkeypatch, policy)
    assert generated == policy
    for name, key in {
        'SEARCH_VOLUME_BYTES_RESERVE_DIVISOR': 'bytesReserveDivisor',
        'SEARCH_VOLUME_MINIMUM_BYTES_RESERVE': 'minimumBytesReserve',
        'SEARCH_VOLUME_MAXIMUM_BYTES_RESERVE': 'maximumBytesReserve',
        'SEARCH_VOLUME_INODES_RESERVE_DIVISOR': 'inodesReserveDivisor',
        'SEARCH_VOLUME_MINIMUM_INODES_RESERVE': 'minimumInodesReserve',
        'SEARCH_VOLUME_MAXIMUM_INODES_RESERVE': 'maximumInodesReserve',
        'SEARCH_VOLUME_MAXIMUM_RESERVE_FRACTION_DIVISOR': 'maximumReserveFractionDivisor',
    }.items():
        assert f'pub const {name}: u64 = {policy[key]};' in rust


def test_absent_volume_policy_is_explicit_in_python(tmp_path, monkeypatch):
    generated, rust = generate_search(tmp_path, monkeypatch, None)
    assert generated is None
    assert 'SEARCH_VOLUME_' not in rust


@pytest.mark.parametrize('invalid', [0, -1, True, '1000'])
def test_invalid_volume_geometry_prevents_publication(tmp_path, monkeypatch, invalid):
    policy = volume_policy()
    policy['bytesReserveDivisor'] = invalid
    with pytest.raises(ValueError, match='invalid conversation search volume geometry'):
        generate_search(tmp_path, monkeypatch, policy)
