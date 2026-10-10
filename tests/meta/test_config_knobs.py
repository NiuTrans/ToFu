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


def test_duplicate_registry_keys_fail_instead_of_discarding_metadata(tmp_path, monkeypatch):
    from scripts import check_config_knobs

    registry = tmp_path / 'knobs.json'
    registry.write_text('{"knobs": {"TOFU_EXAMPLE": {"status": "documented"}, "TOFU_EXAMPLE": {}}}')
    monkeypatch.setattr(check_config_knobs, 'REGISTRY', str(registry))
    with pytest.raises(ValueError, match='duplicate registry key: TOFU_EXAMPLE'):
        check_config_knobs.load_registry()
