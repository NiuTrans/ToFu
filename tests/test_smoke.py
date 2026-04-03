"""Smoke tests — fast sanity checks consolidated from debug/ and healthcheck.py.

Covers:
  1. Import validation — all lib/ sub-packages import cleanly
  2. Cross-platform compat — platform detection, shell args, process introspection
  3. Python syntax — all .py files compile without errors
  4. Blueprint registration — all Flask blueprints load

These wrap logic from:
  - lib/tests/validate_imports.py
  - debug/test_cross_platform.py
  - healthcheck.py (sections 1 & 2)

Run:  pytest tests/test_smoke.py -m unit -v
"""
from __future__ import annotations

import importlib
import os
import platform
import py_compile
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ═══════════════════════════════════════════════════════════
#  1. Import Validation (from lib/tests/validate_imports.py)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestImportValidation:
    """Verify all lib/ sub-packages and modules import without error."""

    # Sub-packages with __init__.py façades
    _SUB_PACKAGES = [
        "lib",
        "lib.fetch",
        "lib.trading",
        "lib.trading_autopilot",
        "lib.trading_backtest_engine",
        "lib.trading_strategy_engine",
        "lib.llm_dispatch",
        "lib.project_mod",
        "lib.scheduler",
        "lib.swarm",
        "lib.tasks_pkg",
        "lib.tools",
    ]

    # Top-level modules
    _TOP_LEVEL_MODULES = [
        "lib.browser",
        "lib.conv_ref",
        "lib.database",
        "lib.embeddings",
        "lib.image_gen",
        "lib.llm_client",
        "lib.log",
        "lib.pdf_parser",
        "lib.pricing",
        "lib.protocols",
        "lib.rate_limiter",
        "lib.search",
        "lib.skills",
    ]

    @pytest.mark.parametrize("module_name", _SUB_PACKAGES + _TOP_LEVEL_MODULES)
    def test_import_module(self, module_name):
        """Each lib module imports without error."""
        mod = importlib.import_module(module_name)
        assert mod is not None

    def test_validate_imports_function(self):
        """The standalone validate_imports() returns True."""
        from lib.tests.validate_imports import validate_imports
        assert validate_imports() is True


# ═══════════════════════════════════════════════════════════
#  2. Cross-Platform Compat (from debug/test_cross_platform.py)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCrossPlatformCompat:
    """Verify lib.compat works on the current platform."""

    def test_platform_detection_exactly_one(self):
        from lib.compat import IS_LINUX, IS_MACOS, IS_WINDOWS
        flags = [IS_WINDOWS, IS_MACOS, IS_LINUX]
        assert sum(flags) == 1, f"Expected exactly 1 True, got {flags}"

    def test_platform_flag_matches_os(self):
        from lib.compat import IS_LINUX, IS_MACOS, IS_WINDOWS
        system = platform.system()
        if system == "Linux":
            assert IS_LINUX
        elif system == "Darwin":
            assert IS_MACOS
        elif system == "Windows":
            assert IS_WINDOWS

    def test_has_procfs_consistent(self):
        from lib.compat import HAS_PROCFS, IS_LINUX
        if IS_LINUX:
            assert HAS_PROCFS is True
        else:
            assert HAS_PROCFS is False

    def test_get_shell_args(self):
        from lib.compat import IS_WINDOWS, get_shell_args
        args = get_shell_args("echo hello")
        assert isinstance(args, list)
        assert len(args) == 3
        if IS_WINDOWS:
            assert args[0] == "cmd.exe"
            assert args[1] == "/c"
        else:
            assert args[0] == "/bin/sh"
            assert args[1] == "-c"

    def test_get_username(self):
        from lib.compat import get_username
        username = get_username()
        assert isinstance(username, str)
        assert len(username) > 0

    def test_get_temp_dir(self):
        from lib.compat import get_temp_dir
        tmp = get_temp_dir()
        assert os.path.isdir(tmp)

    def test_is_process_alive(self):
        from lib.compat import is_process_alive
        assert is_process_alive(os.getpid()) is True
        assert is_process_alive(99999999) is False


# ═══════════════════════════════════════════════════════════
#  3. Python Syntax Check (from healthcheck.py §1)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPythonSyntax:
    """Verify all .py files in the project compile without syntax errors."""

    _SKIP_DIRS = {
        ".git", "__pycache__", "node_modules", "debug", "analysis_scripts",
        "offline_pkgs", "logs", ".project_sessions", ".chatui", "uploads",
    }

    def _collect_py_files(self):
        py_files = []
        for root, dirs, files in os.walk(PROJECT_ROOT):
            dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS]
            for f in files:
                if f.endswith(".py"):
                    py_files.append(os.path.join(root, f))
        return py_files

    def test_all_py_files_compile(self):
        """Every .py file in the project compiles without SyntaxError."""
        py_files = self._collect_py_files()
        assert len(py_files) > 50, f"Expected 50+ .py files, found {len(py_files)}"

        errors = []
        for path in py_files:
            try:
                py_compile.compile(path, doraise=True)
            except py_compile.PyCompileError as e:
                errors.append(str(e))

        assert not errors, f"Syntax errors in {len(errors)} file(s):\n" + "\n".join(errors)


# ═══════════════════════════════════════════════════════════
#  4. Blueprint Registration (from healthcheck.py §2)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBlueprintRegistration:
    """Verify all Flask blueprints can be imported and registered."""

    def test_all_blueprints_import(self):
        from routes import ALL_BLUEPRINTS
        assert len(ALL_BLUEPRINTS) > 10, (
            f"Expected 10+ blueprints, got {len(ALL_BLUEPRINTS)}"
        )

    def test_blueprint_names_unique(self):
        from routes import ALL_BLUEPRINTS
        names = [bp.name for bp in ALL_BLUEPRINTS]
        assert len(names) == len(set(names)), (
            f"Duplicate blueprint names: {[n for n in names if names.count(n) > 1]}"
        )
