"""Architecture gates for import direction across lib/routes/sidecar layers.

The layer table in ``docs/ARCHITECTURE.md`` is only real if the import graph
cannot quietly grow upward edges: the storage sidecar (a separate process)
must never pull in the task-execution graph, backend libraries must never
reach up into the HTTP delivery layer, and routes must stay on the storage
client seam. These tests pin the gate behavior with synthetic trees and then
assert the real tree stays clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_architecture as architecture

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_sidecar_cannot_import_execution_or_delivery(tmp_path):
    sidecar = tmp_path / "lib" / "storage_sidecar"
    bad = _write(
        sidecar / "ops.py",
        "def f():\n"
        "    from lib.tasks_pkg.turn_trace import fold_task_trace\n"
        "    import routes\n",
    )
    violations = architecture.import_direction_violations(
        [bad], lib_root=tmp_path / "lib", routes_root=tmp_path / "routes",
        sidecar_root=sidecar, composition_roots=frozenset(),
        lib_routes_debt=frozenset(), shared_modules=frozenset(),
        shared_paths=frozenset(),
    )
    assert len(violations) == 2
    assert "separate process" in violations[0]


def test_sidecar_may_import_shared_contract_modules(tmp_path):
    sidecar = tmp_path / "lib" / "storage_sidecar"
    good = _write(
        sidecar / "ops.py",
        "from lib.orchestration.run_status import is_run_status\n"
        "from lib.storage_sidecar import schema\n",
    )
    assert architecture.import_direction_violations(
        [good], lib_root=tmp_path / "lib", routes_root=tmp_path / "routes",
        sidecar_root=sidecar, composition_roots=frozenset(),
        lib_routes_debt=frozenset(),
        shared_modules=frozenset({"lib.orchestration.run_status"}),
        shared_paths=frozenset(),
    ) == []


def test_shared_module_closure_cannot_grow_heavy(tmp_path):
    lib = tmp_path / "lib"
    shared = _write(
        lib / "orchestration" / "run_status.py",
        "from lib.agent_core.events import Phase\n",
    )
    assert architecture.import_direction_violations(
        [shared], lib_root=lib, routes_root=tmp_path / "routes",
        sidecar_root=lib / "storage_sidecar", composition_roots=frozenset(),
        lib_routes_debt=frozenset(),
        shared_modules=frozenset({"lib.orchestration.run_status"}),
        shared_paths=frozenset({shared.resolve()}),
    ) != []
    ok = _write(
        lib / "orchestration" / "wire_formats.py",
        "import uuid\n",
    )
    assert architecture.import_direction_violations(
        [ok], lib_root=lib, routes_root=tmp_path / "routes",
        sidecar_root=lib / "storage_sidecar", composition_roots=frozenset(),
        lib_routes_debt=frozenset(),
        shared_modules=frozenset({"lib.orchestration.wire_formats"}),
        shared_paths=frozenset({ok.resolve()}),
    ) == []


def test_lib_cannot_import_routes_outside_composition_root(tmp_path):
    lib = tmp_path / "lib"
    bad = _write(lib / "worker.py", "from routes.api_v1 import auth\n")
    root = _write(lib / "app_assembly.py", "from routes import register_all\n")
    violations = architecture.import_direction_violations(
        [bad, root], lib_root=lib, routes_root=tmp_path / "routes",
        sidecar_root=lib / "storage_sidecar",
        composition_roots=frozenset({root.resolve()}),
        lib_routes_debt=frozenset(), shared_modules=frozenset(),
        shared_paths=frozenset(),
    )
    assert len(violations) == 1
    assert "never import routes" in violations[0]


def test_routes_cannot_import_sidecar_internals(tmp_path):
    routes = tmp_path / "routes"
    bad = _write(
        routes / "api.py",
        "from lib.storage_sidecar.schema import SCHEMA_VERSION\n",
    )
    violations = architecture.import_direction_violations(
        [bad], lib_root=tmp_path / "lib", routes_root=routes,
        sidecar_root=tmp_path / "lib" / "storage_sidecar",
        composition_roots=frozenset(), lib_routes_debt=frozenset(),
        shared_modules=frozenset(), shared_paths=frozenset(),
    )
    assert len(violations) == 1
    assert "client layer" in violations[0]


def test_module_class_replacement_is_rejected(tmp_path):
    bad = _write(
        tmp_path / "facade.py",
        "import sys\n"
        "sys.modules[__name__].__class__ = type('_M', (), {})\n",
    )
    violations = architecture.module_facade_mutation_violations(
        [bad], allowed=frozenset(),
    )
    assert len(violations) == 1
    assert "class replacement" in violations[0]


def test_module_class_replacement_allowlist_is_honored(tmp_path):
    legacy = _write(
        tmp_path / "api.py",
        "import sys\n"
        "sys.modules[__name__].__class__ = type('_M', (), {})\n",
    )
    assert architecture.module_facade_mutation_violations(
        [legacy], allowed=frozenset({legacy.resolve()}),
    ) == []


def test_lib_init_ratchet_grows_and_shrinks_loudly(tmp_path):
    init = _write(tmp_path / "__init__.py", "import os\nA = os.environ.get('X')\n")
    assert architecture.lib_init_ratchet_violations(
        init_path=init, max_lines=2, max_env_reads=1,
    ) == []
    grew = architecture.lib_init_ratchet_violations(
        init_path=init, max_lines=1, max_env_reads=1,
    )
    assert grew and "grew" in grew[0]
    shrank = architecture.lib_init_ratchet_violations(
        init_path=init, max_lines=5, max_env_reads=3,
    )
    assert shrank and "SHRANK" in shrank[0]


def test_real_tree_has_no_import_direction_violations():
    """Anti-collapse tripwire: the gates must see the real tree and pass."""
    sources = architecture.authored_sources()
    assert any(
        path.is_relative_to(ROOT / "lib" / "storage_sidecar")
        for path in sources
    ), "gate is not reaching the sidecar tree"
    assert architecture.import_direction_violations(sources) == []
    assert architecture.module_facade_mutation_violations(sources) == []


def test_lib_toplevel_ratchet_counts_only_loose_modules():
    assert architecture.lib_toplevel_ratchet_violations(maximum=10**9) != []
    assert architecture.lib_toplevel_ratchet_violations(maximum=0) != []
    assert architecture.lib_toplevel_ratchet_violations() == []
