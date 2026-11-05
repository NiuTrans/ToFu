"""Guard for the environment-knob registry contract.

``contracts/config_knobs_v1.json`` is the complete inventory of literal env
reads in the tree; these assertions keep the registry honest (shape, sane
size, metadata rules) and re-run the drift check so a silent scan collapse
or an unregistered knob cannot stay green.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
REGISTRY = os.path.join(ROOT, "contracts", "config_knobs_v1.json")
GATE = os.path.join(ROOT, "scripts", "check_config_knobs.py")


def _load() -> dict:
    with open(REGISTRY, encoding="utf-8") as f:
        return json.load(f)


def test_registry_shape_and_metadata_rules():
    document = _load()
    assert document["contract"] == "tofu.config-knobs/v1"
    knobs = document["knobs"]
    assert len(knobs) > 400, (
        f"registry collapsed to {len(knobs)} knobs — the env scan is not "
        "reaching the tree"
    )
    for name, entry in knobs.items():
        assert entry.get("status") in ("legacy", "documented", "deprecated"), \
            (name, entry)
        if entry["status"] == "documented":
            assert entry.get("owner") and entry.get("description"), name
    assert document["legacy_count"] == sum(
        1 for entry in knobs.values() if entry["status"] == "legacy")


def test_registry_is_not_stale():
    result = subprocess.run(
        [sys.executable, GATE], cwd=ROOT,
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, (
        f"env knob registry drifted:\n{result.stdout.strip()}\n"
        f"{result.stderr.strip()}"
    )


def test_worker_environment_adapter_is_a_configuration_reader():
    import ast
    from scripts.check_config_knobs import _env_reads

    tree = ast.parse("""
proc_env_value(pid, 'TOFU_MANAGED_BY')
proc_env_value('not-a-knob', unknown_name)
os.environ.get('TOFU_NORMAL')
resolve_resource_budget('TOFU_BUDGET')
""")
    assert _env_reads(tree) == {'TOFU_MANAGED_BY', 'TOFU_NORMAL', 'TOFU_BUDGET'}


def test_table_driven_environment_reads_preserve_the_declared_knobs():
    import ast
    from scripts.check_config_knobs import _env_reads

    tree = ast.parse('''
MODE_KNOBS = {PURPOSE_A: 'TOFU_A', PURPOSE_B: 'TOFU_B', PURPOSE_C: dynamic_name}
UNRELATED = {'key': 'not-a-knob'}
environment.get(MODE_KNOBS[purpose], '')
other.get(UNRELATED[key], '')
''')
    assert _env_reads(tree) == {'TOFU_A', 'TOFU_B'}


def test_duplicate_registry_keys_fail_instead_of_discarding_metadata(tmp_path, monkeypatch):
    from scripts import check_config_knobs

    registry = tmp_path / 'knobs.json'
    registry.write_text('{"knobs": {"TOFU_EXAMPLE": {"status": "documented"}, "TOFU_EXAMPLE": {}}}')
    monkeypatch.setattr(check_config_knobs, 'REGISTRY', str(registry))
    with pytest.raises(ValueError, match='duplicate registry key: TOFU_EXAMPLE'):
        check_config_knobs.load_registry()


def test_drift_report_names_every_unregistered_and_vanished_knob(tmp_path, monkeypatch):
    """A gate that is already red must not hide later drift behind its first name."""
    from scripts import check_config_knobs

    registry = tmp_path / 'knobs.json'
    registry.write_text(json.dumps({
        'contract': 'tofu.config-knobs/v1', 'legacy_count': 0,
        'knobs': {name: {'status': 'documented', 'owner': 'owner.py', 'description': 'd'}
                  for name in ('TOFU_GONE_A', 'TOFU_GONE_B', 'TOFU_KEPT')}}))
    monkeypatch.setattr(check_config_knobs, 'REGISTRY', str(registry))
    monkeypatch.setattr(check_config_knobs, 'scan_tree', lambda: {
        'TOFU_KEPT': ['a.py'], 'TOFU_NEW_A': ['a.py'], 'TOFU_NEW_B': ['b.py']})
    report = '\n'.join(check_config_knobs.check())
    for name in ('TOFU_NEW_A', 'TOFU_NEW_B', 'TOFU_GONE_A', 'TOFU_GONE_B'):
        assert name in report, report
    assert 'TOFU_KEPT' not in report


@pytest.mark.parametrize('call', [
    "read_limit('TOFU_HELPER_ONLY', 7)",
    "read_limit(default=7, name='TOFU_HELPER_ONLY')",
    "forward('TOFU_HELPER_ONLY')",
])
def test_environment_helper_reads_follow_parameters_and_forwarding(call):
    import ast
    from scripts.check_config_knobs import _env_reads

    tree = ast.parse("""
def read_limit(name, default):
    return int(os.environ.get(name, str(default)))
def forward(key):
    return read_limit(key, 7)
""" + call)
    assert _env_reads(tree) == {'TOFU_HELPER_ONLY'}


def test_environment_helper_analysis_does_not_invent_reads_from_other_scopes():
    import ast
    from scripts.check_config_knobs import _env_reads

    tree = ast.parse("""
def unrelated(name):
    def nested(name):
        return os.getenv(name)
    return name
def overwritten(name):
    name = 'TOFU_ACTUAL'
    return os.getenv(name)
unrelated('NOT_AN_ENVIRONMENT_VARIABLE')
overwritten('ALSO_NOT_AN_ENVIRONMENT_VARIABLE')
""")
    assert not _env_reads(tree)
