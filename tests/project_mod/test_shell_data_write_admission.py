"""Explicit shell data writes coordinate their targets, not an unrelated cwd."""
from contextlib import contextmanager
import shlex
import subprocess
import sys
import threading

import pytest

from lib.project_mod import run_command as commands, tools, workspace_lease as leases
from lib.project_mod.command_analysis import command_segment_literal_targets
from lib.project_mod.command_heredoc import command_admission_view

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'source').write_text('source contents\n')
    (root / 'other').write_text('preserve\n')
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    return root


@contextmanager
def held(path, file=False):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            api = leases.workspace_file_write_lease if file else leases.workspace_write_lease
            with api(path):
                ready.set()
                assert release.wait(15), 'holder release was not signalled'
        except BaseException as error:
            errors.append(error)
            ready.set()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(5)
        assert not errors
        yield
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors


def run(repo, route, source):
    if route == 'runner':
        return commands.tool_run_command(str(repo), source)
    return tools.execute_tool('run_command', {'command': source}, str(repo))


def fingerprint_command(repo, scratch, cargo_home):
    q = shlex.quote
    return (
        f'export CARGO_HOME={q(str(cargo_home))} PATH="$CARGO_HOME/bin:$PATH"\n'
        f'mkdir -p {q(str(scratch / "fp2/src"))}\n'
        f'cat > {q(str(scratch / "fp2/Cargo.toml"))} <<\'EOF\'\n'
        '[package]\nname = "fp2"\nversion = "0.0.0"\nedition = "2021"\n'
        '[dependencies]\nsha2 = "0.10"\ntoml = "1.1"\nEOF\n'
        f'{q(sys.executable)} - <<\'PYEOF\'\n'
        'from pathlib import Path\n'
        'src = Path("source").read_text()\n'
        f'Path({str(scratch / "fp2/src/main.rs")!r}).write_text(src)\n'
        'print("patched")\nPYEOF\n'
        f'cd {q(str(scratch / "fp2"))} && CARGO_MANIFEST_DIR={q(str(repo))} '
        f'cargo run --release --quiet 2>{q(str(scratch / "err"))} '
        f'>{q(str(scratch / "out"))}; echo "exit=$?"\n'
        f'grep \'^FINGERPRINT\' {q(str(scratch / "out"))}; '
        f'echo "RS keys: $(grep -c \'^K=\' {q(str(scratch / "out"))})"; '
        f'grep -i \'error\\|panic\' {q(str(scratch / "err"))} | head -3\n'
        f'grep \'^K=\' {q(str(scratch / "out"))} | sed \'s/^K=//\' | sort '
        f'>{q(str(scratch / "keys"))}\n'
        'echo "=== diff ==="; '
        f'diff {q(str(scratch / "expected"))} {q(str(scratch / "keys"))} | head -25\n'
    )


@pytest.mark.parametrize('header, hidden', [
    ("cat > r$n <<'DATA'", True),
    ('python3 - <<\'DATA\' > "$OUT"', True),
    ('cat > "$(mktemp)" <<\'DATA\'', False),
    ("cat > `mktemp` <<'DATA'", False),
    ("cat > ${OUT:-x} <<'DATA'", False),
    ("$RUNNER - <<'DATA'", False),
])
def test_admission_view_accepts_plain_parameters_in_a_heredoc_header(header, hidden):
    """A parameter expansion executes nothing and cannot add a redirect operator;
    substitutions and a program named by an expansion keep the body visible."""
    source = header + '\nrm -rf payload\nDATA\n'
    view = command_admission_view(source)
    assert len(view) == len(source)
    assert ('rm -rf payload' not in view) is hidden


@pytest.mark.parametrize('source, opened', [
    ('echo row > /scratch/r$n.json', ('/scratch',)),
    ('echo row >/scratch/r$n.json', ('/scratch',)),
    ('echo row >> /scratch/r$n.json 2> /scratch/e$n.log', ('/scratch',)),
    ('rm -rf /scratch/$X > /scratch/r$n.json', ()),
    ('rm -rf /scratch/$X', ()),
])
def test_shell_outputs_only_keeps_paths_produced_solely_by_redirects(source, opened):
    assert command_segment_literal_targets(source, shell_outputs_only=True) == opened
    assert set(opened) <= set(command_segment_literal_targets(source))

