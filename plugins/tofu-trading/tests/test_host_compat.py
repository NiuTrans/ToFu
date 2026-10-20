"""Current-host integration boundary for the sidecar-native plugin."""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

import pytest

from lib.storage.manifest import validate_manifest
from tofu_trading.storage_manifest import MANIFEST
import tofu_trading.web as web


pytestmark = pytest.mark.unit


def test_storage_manifest_is_accepted_by_current_host():
    normalized = validate_manifest(MANIFEST)
    assert normalized["namespace"] == "tofu.trading"
    assert normalized["version"] == 1
    assert len(normalized["operations"]) == 40


def test_runtime_source_has_no_retired_database_imports():
    package = Path(__file__).parents[1] / "tofu_trading"
    offenders = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "lib.database" in text:
            offenders.append(path.relative_to(package).as_posix())
    assert offenders == []


def test_trading_provider_protocol_is_owned_by_the_plugin():
    package = Path(__file__).parents[1] / "tofu_trading"
    offenders = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "lib.protocols import TradingDataProvider" in text:
            offenders.append(path.relative_to(package).as_posix())
    assert offenders == []


def test_background_threads_have_operational_names():
    package = Path(__file__).parents[1] / "tofu_trading"
    unnamed = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            is_thread = (
                isinstance(function, ast.Name) and function.id == "Thread"
            ) or (
                isinstance(function, ast.Attribute) and function.attr == "Thread"
            )
            if is_thread and not any(item.arg == "name" for item in node.keywords):
                unnamed.append(f"{path.relative_to(package)}:{node.lineno}")
    assert unnamed == []


def _carries_error_value(value, name):
    """Recognize the original error in an explicit returned outcome carrier."""
    if name is None:
        return False
    if isinstance(value, ast.Name):
        return value.id == name
    if isinstance(value, (ast.Tuple, ast.List)):
        return any(_carries_error_value(item, name) for item in value.elts)
    if isinstance(value, ast.Dict):
        return any(_carries_error_value(item, name) for item in value.values)
    return False


def _silent_broad_handlers(source, path):
    """Return locations missing a visible failure observation/propagation port."""
    log_methods = {
        "debug", "info", "warning", "error", "exception", "critical", "log"
    }
    silent = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        broad = node.type is None or (
            isinstance(node.type, ast.Name)
            and node.type.id in {"Exception", "BaseException"}
        )
        if not broad:
            continue
        handler = ast.Module(body=node.body, type_ignores=[])
        logged = any(
            isinstance(call.func, ast.Attribute)
            and call.func.attr in log_methods
            for call in ast.walk(handler)
            if isinstance(call, ast.Call)
        )
        reraises = any(
            isinstance(item, ast.Raise) and item.exc is None
            for item in ast.walk(handler)
        )
        # A Future is an explicit error port, not a swallowed exception.
        # The generated kernel also delegates initializer failure to its
        # one failed-Future owner, verified by the shared lifecycle suite.
        forwarded = any(
            isinstance(call.func, ast.Attribute)
            and (call.func.attr == "set_exception" or (
                path.name == "_worker_executor.py"
                and call.func.attr == "_fail_initializer"))
            for call in ast.walk(handler)
            if isinstance(call, ast.Call)
        )
        forwarded = forwarded or any(
            isinstance(item, ast.Return) and _carries_error_value(item.value, node.name)
            for item in ast.walk(handler)
        )
        if not logged and not reraises and not forwarded:
            silent.append(node.lineno)
    return silent


@pytest.mark.parametrize('statement,visible', [
    ('return error', True),
    ('return (), error', True),
    ("return {'failure': error}", True),
    ('return None', False),
    ('return False', False),
    ('return str(error)', False),
    ("return {'failure': None}", False),
    ('return lambda: error', False),
    ('raise', True),
    ('future.set_exception(error)', True),
    ('logger.exception("failed")', True),
])
def test_broad_exception_guard_recognizes_explicit_error_outcomes(statement, visible):
    source = f'def run():\n    try:\n        work()\n    except Exception as error:\n        {statement}\n'
    assert bool(_silent_broad_handlers(source, Path('fixture.py'))) is not visible


def test_broad_exception_handlers_log_reraise_or_forward_failure():
    package = Path(__file__).parents[1] / "tofu_trading"
    silent = []
    for path in package.rglob("*.py"):
        silent.extend(f'{path.relative_to(package)}:{line}' for line in
                      _silent_broad_handlers(path.read_text(encoding='utf-8'), path))
    assert silent == []


def test_worker_import_failure_happens_after_verified_storage(monkeypatch):
    brain = ModuleType("tofu_trading.web.handlers.trading_brain")
    brain.init_brain = lambda: None
    intel = ModuleType("tofu_trading.web.handlers.trading_intel")
    autopilot = ModuleType("tofu_trading.web.handlers.trading_autopilot")
    calls = []
    intel.start_intel_worker = lambda _app: calls.append("intel")

    def fail_to_import_worker():
        raise RuntimeError("autopilot import failed")

    autopilot.__getattr__ = lambda _name: fail_to_import_worker()
    monkeypatch.setitem(__import__("sys").modules, brain.__name__, brain)
    monkeypatch.setitem(__import__("sys").modules, intel.__name__, intel)
    monkeypatch.setitem(__import__("sys").modules, autopilot.__name__, autopilot)
    monkeypatch.setattr(
        "tofu_trading.storage.prepare_storage",
        lambda: {"migration": "legacy-v1", "total_rows": 0},
    )

    with pytest.raises(RuntimeError, match="autopilot import failed"):
        web.start_workers(object())
    assert calls == []
