"""Ordinary tools defer schema loading; actual grants still fail closed."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit


def test_fresh_dispatch_import_defers_validator_without_bypassing_validation(tmp_path):
    probe = textwrap.dedent('''
        import builtins
        import sys

        assert 'jsonschema' not in sys.modules
        original = builtins.__import__
        blocked = True
        attempts = []
        def guarded(name, *args, **kwargs):
            if name == 'jsonschema' or name.startswith('jsonschema.'):
                attempts.append(name)
                if blocked:
                    raise ImportError('validator deliberately unavailable')
            return original(name, *args, **kwargs)
        builtins.__import__ = guarded
        try:
            from lib.project_dispatch.scope import admit_tool, validate_profile
            from lib.project_dispatch import ProjectDispatchError
            assert admit_tool({'id': 'ordinary-task', 'config': {}}, 'repo_map', {}) is None
            assert attempts == [] and 'jsonschema' not in sys.modules
            profile = dict(mode='consult', tools=[], readPaths=[], writePaths=[], maxToolCalls=0)
            try:
                validate_profile({'profile': profile})
            except ImportError as error:
                assert str(error) == 'validator deliberately unavailable'
            else:
                raise AssertionError('missing validator admitted a profile')
            assert attempts
            blocked = False
            assert validate_profile({'profile': profile}) == profile
            assert 'jsonschema' in sys.modules
            for invalid in [dict(profile, maxToolCalls=True), dict(profile, unknownAuthority=True)]:
                try:
                    validate_profile({'profile': invalid})
                except ProjectDispatchError:
                    pass
                else:
                    raise AssertionError('malformed profile admitted after lazy loading')
        finally:
            builtins.__import__ = original
    ''')
    executable = os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable)
    result = subprocess.run([executable, '-c', probe],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ, TOFU_DATA_DIR=str(tmp_path)),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
