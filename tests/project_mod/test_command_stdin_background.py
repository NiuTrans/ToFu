"""Only executed shell syntax can trigger unmanaged detach admission."""
import pytest

from lib.project_mod import run_command as commands

pytestmark = pytest.mark.unit


def test_literal_runtime_stdin_bitwise_operator_is_not_shell_background():
    source = "python3 - <<'PY'\nprint(2 & 1)\nPY\n"
    assert commands.command_background_refusal(source) is None


def test_real_detach_after_literal_stdin_remains_refused():
    source = "python3 - <<'PY'\nprint(2 & 1)\nPY\nsleep 20 &\n"
    assert 'background-unsupported' in commands.command_background_refusal(source)


def test_shell_stdin_detach_is_not_hidden_as_runtime_data():
    source = "sh <<'SH'\nsleep 20 &\nSH\n"
    assert 'background-unsupported' in commands.command_background_refusal(source)
