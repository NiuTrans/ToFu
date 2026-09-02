"""Executable contract for relocatable system-supervisord deployment."""

from __future__ import annotations

import configparser
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


pwd = pytest.importorskip(
    "pwd", reason="system-supervisord deployment is supported only on POSIX hosts")
pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_DIRECTORY = ROOT / "deploy" / "supervisor"
TEMPLATE = SUPERVISOR_DIRECTORY / "tofu.conf.template"
RENDERER = SUPERVISOR_DIRECTORY / "render_config.py"
INSTALLER = SUPERVISOR_DIRECTORY / "install.sh"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("tofu_supervisor_renderer", RENDERER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse(text: str) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read_string(text)
    assert parser.has_section("program:tofu")
    return parser["program:tofu"]


@pytest.fixture()
def rendered_host(tmp_path: Path):
    project = tmp_path / "relocated Tofu 豆腐"
    project.mkdir()
    (project / "server.py").write_text("# fixture\n", encoding="utf-8")
    (project / "logs").mkdir()
    home = tmp_path / "home, with spaces"
    home.mkdir()
    browser = home / "tofu-browser-libs"
    (browser / "lib").mkdir(parents=True)
    fonts = browser / "etc" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "fonts.conf").write_text("<fontconfig/>\n", encoding="utf-8")
    user_name = pwd.getpwuid(os.getuid()).pw_name

    module = _load_renderer()
    text = module.render_config(
        project_root=project,
        python_executable=Path(sys.executable),
        user_name=user_name,
        home_directory=home,
        port=15432,
        browser_libraries_directory=browser,
    )
    return {
        "text": text,
        "program": _parse(text),
        "project": project,
        "home": home,
        "browser": browser,
        "user": user_name,
    }


def test_repository_keeps_only_a_host_neutral_template():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert not (SUPERVISOR_DIRECTORY / "tofu.conf").exists()
    assert source.count("@@TOFU_COMMAND@@") == 1
    assert source.count("@@TOFU_PROJECT_DIR@@") == 1
    assert source.count("@@TOFU_RUN_USER@@") == 1
    assert source.count("@@TOFU_ENVIRONMENT@@") == 1
    assert source.count("@@TOFU_LOG_PATH@@") == 1
    for machine_prefix in ("/mnt/", "/home/", "/Users/", ":\\"):
        assert machine_prefix not in source


def test_rendered_command_and_paths_follow_the_target_host(rendered_host):
    program = rendered_host["program"]
    assert shlex.split(program["command"]) == [sys.executable, "server.py"]
    assert program["directory"] == str(rendered_host["project"])
    assert program["user"] == rendered_host["user"]
    assert program["stdout_logfile"] == str(
        rendered_host["project"] / "logs" / "supervisor_tofu.log")


def test_percent_paths_survive_supervisor_expansion_passes(tmp_path: Path):
    project = tmp_path / "Tofu 100%"
    project.mkdir()
    (project / "server.py").touch()
    python_executable = project / "python%"
    python_executable.symlink_to(sys.executable)
    home = tmp_path / "home 50%"
    home.mkdir()
    module = _load_renderer()
    text = module.render_config(
        project_root=project,
        python_executable=python_executable,
        user_name=pwd.getpwuid(os.getuid()).pw_name,
        home_directory=home,
        port=15000,
    )
    program = _parse(text)

    # Supervisor expands ordinary program fields once and logfile fields twice.
    assert program["directory"] % {} == str(project)
    assert shlex.split(program["command"] % {}) == [
        str(python_executable), "server.py"]
    assert program["stdout_logfile"] % {} % {} == str(
        project / "logs" / "supervisor_tofu.log")
    environment = program["environment"] % {}
    assert f'HOME="{home}"' in environment


def test_environment_paths_preserve_quotes_and_backslashes(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "server.py").touch()
    home = tmp_path / 'home, double" quote and back\\slash'
    home.mkdir()
    module = _load_renderer()
    text = module.render_config(
        project_root=project,
        python_executable=Path(sys.executable),
        user_name=pwd.getpwuid(os.getuid()).pw_name,
        home_directory=home,
        port=15000,
    )

    environment = _parse(text)["environment"]
    assert f"HOME='{home}'" in environment
    assert "back\\\\slash" not in environment


def test_environment_path_with_both_quote_styles_fails_closed(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "server.py").touch()
    home = tmp_path / "home with 'single' and \"double\""
    home.mkdir()
    module = _load_renderer()

    with pytest.raises(module.ConfigRenderError, match="both quote styles"):
        module.render_config(
            project_root=project,
            python_executable=Path(sys.executable),
            user_name=pwd.getpwuid(os.getuid()).pw_name,
            home_directory=home,
            port=15000,
        )


def test_rendered_lifecycle_is_bounded_and_graceful(rendered_host):
    program = rendered_host["program"]
    assert program["autostart"].lower() == "true"
    assert program["autorestart"].lower() == "true"
    assert int(program["startsecs"]) >= 10
    assert int(program["startretries"]) >= 1
    assert program["stopsignal"].upper() == "TERM"
    assert int(program["stopwaitsecs"]) >= 15
    assert program["stopasgroup"].lower() == "true"
    assert program["killasgroup"].lower() == "true"
    assert program["umask"] == "077"
    assert program["stdout_logfile_maxbytes"] == "16MB"
    assert int(program["stdout_logfile_backups"]) == 3


def test_rendered_environment_keeps_runtime_and_browser_contract(rendered_host):
    environment = rendered_host["program"]["environment"]
    expected = (
        'PORT="15432"',
        'BIND_HOST="0.0.0.0"',
        f'HOME="{rendered_host["home"]}"',
        'PYTHONUTF8="1"',
        'TOFU_SERVER_WORKER="1"',
        'TOFU_MANAGED_BY="root-supervisord"',
        f'CHROMIUM_EXTRA_LIB_DIRS="{rendered_host["browser"] / "lib"}"',
        f'FONTCONFIG_PATH="{rendered_host["browser"] / "etc" / "fonts"}"',
        f'FONTCONFIG_FILE="{rendered_host["browser"] / "etc" / "fonts" / "fonts.conf"}"',
    )
    for item in expected:
        assert item in environment
    assert "LANG=" not in environment
    assert "LC_ALL=" not in environment


def test_missing_optional_browser_bundle_is_not_advertised(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "server.py").touch()
    home = tmp_path / "home"
    home.mkdir()
    module = _load_renderer()
    text = module.render_config(
        project_root=project,
        python_executable=Path(sys.executable),
        user_name=pwd.getpwuid(os.getuid()).pw_name,
        home_directory=home,
        port=15000,
        browser_libraries_directory=home / "missing-browser-bundle",
    )
    environment = _parse(text)["environment"]
    assert "CHROMIUM_EXTRA_LIB_DIRS" not in environment
    assert "FONTCONFIG_PATH" not in environment
    assert "FONTCONFIG_FILE" not in environment


def test_unrepresentable_path_fails_without_overwriting_output(tmp_path: Path):
    project = tmp_path / "unsafe;project"
    project.mkdir()
    (project / "server.py").touch()
    home = tmp_path / "home"
    home.mkdir()
    output = tmp_path / "tofu.conf"
    output.write_text("previous-config\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--project-root", str(project),
            "--python", sys.executable,
            "--user", pwd.getpwuid(os.getuid()).pw_name,
            "--home", str(home),
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "cannot encode safely" in result.stderr
    assert output.read_text(encoding="utf-8") == "previous-config\n"


def test_renderer_publishes_one_complete_atomic_file(tmp_path: Path, rendered_host):
    output = tmp_path / "rendered" / "tofu.conf"
    module = _load_renderer()
    module.write_atomic(output, rendered_host["text"])
    assert output.read_text(encoding="utf-8") == rendered_host["text"]
    assert not list(output.parent.glob(f".{output.name}.*"))


def test_installer_help_and_dry_run_are_non_mutating_interfaces(tmp_path: Path):
    help_result = subprocess.run(
        ["bash", str(INSTALLER), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert help_result.returncode == 0
    assert "--dry-run" in help_result.stdout
    assert list(tmp_path.iterdir()) == []

    dry_run = subprocess.run(
        [
            "bash", str(INSTALLER), "--dry-run",
            "--python", sys.executable,
            "--user", pwd.getpwuid(os.getuid()).pw_name,
            "--home", str(Path.home()),
            "--port", "15433",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TOFU_BROWSER_LIBS_DIR": str(tmp_path / "missing")},
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert _parse(dry_run.stdout)["directory"] == str(ROOT)
    assert 'PORT="15433"' in dry_run.stdout
    assert list(tmp_path.iterdir()) == []


def _copy_minimal_supervisor_checkout(destination: Path) -> Path:
    project = destination / "moved checkout with spaces"
    supervisor_directory = project / "deploy" / "supervisor"
    supervisor_directory.mkdir(parents=True)
    for name in ("install.sh", "render_config.py", "tofu.conf.template"):
        shutil.copy2(SUPERVISOR_DIRECTORY / name, supervisor_directory / name)
    (project / "server.py").write_text("# relocated fixture\n", encoding="utf-8")
    return project


def test_installer_resolves_a_relocated_checkout_from_its_own_path(tmp_path: Path):
    project = _copy_minimal_supervisor_checkout(tmp_path)
    home = tmp_path / "service home"
    home.mkdir()
    result = subprocess.run(
        [
            "bash", str(project / "deploy" / "supervisor" / "install.sh"),
            "--dry-run",
            "--python", sys.executable,
            "--user", pwd.getpwuid(os.getuid()).pw_name,
            "--home", str(home),
            "--port", "15434",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    program = _parse(result.stdout)
    assert program["directory"] == str(project)
    assert shlex.split(program["command"]) == [sys.executable, "server.py"]
    assert program["stdout_logfile"] == str(project / "logs" / "supervisor_tofu.log")


def test_relocated_checkout_with_stale_marker_fails_closed(tmp_path: Path):
    project = _copy_minimal_supervisor_checkout(tmp_path)
    stale_python = tmp_path / "old-host" / "missing-python"
    (project / ".tofu_env.json").write_text(
        json.dumps({"python": str(stale_python)}) + "\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [
            "bash", str(project / "deploy" / "supervisor" / "install.sh"),
            "--dry-run",
            "--user", pwd.getpwuid(os.getuid()).pw_name,
            "--home", str(home),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "points to a missing interpreter" in result.stderr
    assert "rerun install.sh after moving this checkout" in result.stderr


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _system_install_fixture(
    tmp_path: Path,
    *,
    health_exit_code: int = 0,
    listener_pid: int | None = None,
    proven_project_listener: bool = False,
    reported_uid: int = 0,
    reread_fail_once: bool = False,
):
    if sys.platform != "linux":
        pytest.skip("system-supervisord installation is Linux-only")
    project = _copy_minimal_supervisor_checkout(tmp_path)
    (project / "healthcheck.py").write_text(
        f"raise SystemExit({health_exit_code})\n", encoding="utf-8")
    (project / "serverctl.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "action = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "log = os.environ.get('FAKE_SERVERCTL_LOG')\n"
        "if log:\n"
        "    with open(log, 'a', encoding='utf-8') as stream:\n"
        "        stream.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "listener = os.environ.get('FAKE_LISTENER_STATE')\n"
        "if listener and action == 'stop':\n"
        "    Path(listener).unlink(missing_ok=True)\n"
        "elif listener and action == 'start':\n"
        "    Path(listener).touch()\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    home = tmp_path / "service-home"
    home.mkdir()
    config_directory = tmp_path / "supervisor-conf.d"
    config_directory.mkdir()
    state_file = tmp_path / "supervisor-running"
    supervisor_log = tmp_path / "supervisorctl.log"
    reread_failed_marker = tmp_path / "supervisor-reread-failed"
    serverctl_log = tmp_path / "serverctl.log"
    listener_state = tmp_path / "manual-listener"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    current_user = pwd.getpwuid(os.getuid()).pw_name
    target_user = current_user if current_user != "root" else "nobody"

    _write_executable(
        fake_bin / "id",
        "#!/usr/bin/env bash\n"
        'case "${1:-}" in\n'
        f'  -u) echo {reported_uid} ;;\n'
        f'  -un) echo {shlex.quote(target_user if reported_uid == 0 else "fake-caller")} ;;\n'
        '  *) exec /usr/bin/id "$@" ;;\n'
        "esac\n",
    )
    _write_executable(
        fake_bin / "sudo",
        "#!/usr/bin/env bash\n"
        '[ "${1:-}" = "-n" ] && shift\n'
        'if [ "${1:-}" = "-u" ]; then shift 2; fi\n'
        '[ "${1:-}" = "--" ] && shift\n'
        'exec "$@"\n',
    )
    _write_executable(
        fake_bin / "runuser",
        "#!/usr/bin/env bash\n"
        "echo 'runuser must not be selected by a non-root installer' >&2\n"
        "exit 99\n",
    )
    _write_executable(
        fake_bin / "supervisorctl",
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$FAKE_SUPERVISOR_LOG"\n'
        'case "${1:-}" in\n'
        '  status)\n'
        '    if [ -f "$FAKE_SUPERVISOR_STATE" ]; then\n'
        '      echo "tofu RUNNING pid 4242, uptime 0:00:20"\n'
        '    else\n'
        '      echo "tofu: ERROR (no such process)" >&2\n'
        '      exit 3\n'
        '    fi ;;\n'
        '  update|start) touch "$FAKE_SUPERVISOR_STATE" ;;\n'
        '  stop) rm -f "$FAKE_SUPERVISOR_STATE" ;;\n'
        '  reread)\n'
        '    if [ "$FAKE_REREAD_FAIL_ONCE" = "1" ] '
        '&& [ ! -f "$FAKE_REREAD_FAILED_MARKER" ]; then\n'
        '      touch "$FAKE_REREAD_FAILED_MARKER"\n'
        '      exit 1\n'
        '    fi ;;\n'
        '  *) exit 2 ;;\n'
        "esac\n",
    )
    listener_line = ""
    if listener_pid is not None:
        listener_line = (
            "LISTEN 0 128 127.0.0.1:15435 0.0.0.0:* "
            f'users:(("python",pid={listener_pid},fd=3))')
    if proven_project_listener:
        assert listener_pid is not None
        listener_state.touch()
        (project / "server_manager.py").write_text(
            "import os\n"
            "def read_lock_status(_project):\n"
            "    return {\n"
            "        'running': True,\n"
            "        'pid': int(os.environ['FAKE_PROJECT_LISTENER_PID']),\n"
            "        'projectMatches': True,\n"
            "        'externalOwner': None,\n"
            "    }\n",
            encoding="utf-8",
        )
    ss_source = "#!/usr/bin/env bash\n"
    if listener_line:
        if proven_project_listener:
            ss_source += '[ -f "$FAKE_LISTENER_STATE" ] || exit 0\n'
        ss_source += f"printf '%s\\n' {shlex.quote(listener_line)}\n"
    else:
        ss_source += "exit 0\n"
    _write_executable(fake_bin / "ss", ss_source)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_SUPERVISOR_STATE": str(state_file),
        "FAKE_SUPERVISOR_LOG": str(supervisor_log),
        "FAKE_REREAD_FAIL_ONCE": "1" if reread_fail_once else "0",
        "FAKE_REREAD_FAILED_MARKER": str(reread_failed_marker),
        "FAKE_SERVERCTL_LOG": str(serverctl_log),
        "FAKE_LISTENER_STATE": str(listener_state),
        "FAKE_PROJECT_LISTENER_PID": str(listener_pid or ""),
        "TOFU_BROWSER_LIBS_DIR": str(tmp_path / "missing-browser-bundle"),
    }
    command = [
        "bash", str(project / "deploy" / "supervisor" / "install.sh"),
        "--python", sys.executable,
        "--user", target_user,
        "--home", str(home),
        "--port", "15435",
        "--config-dir", str(config_directory),
    ]
    return project, config_directory, command, environment


def test_system_install_applies_rendered_config_and_requires_runtime_health(
        tmp_path: Path):
    project, config_directory, command, environment = _system_install_fixture(tmp_path)
    result = subprocess.run(
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    installed = (config_directory / "tofu.conf").read_text(encoding="utf-8")
    assert _parse(installed)["directory"] == str(project)
    assert "runtime-healthy" in result.stdout


def test_system_install_supports_non_root_passwordless_sudo(tmp_path: Path):
    project, config_directory, command, environment = _system_install_fixture(
        tmp_path, reported_uid=1000)
    result = subprocess.run(
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (config_directory / "tofu.conf").is_file()
    assert "runuser must not be selected" not in result.stderr


def test_live_update_approval_resolves_module_from_relocated_project(
        tmp_path: Path):
    project, config_directory, command, environment = _system_install_fixture(tmp_path)
    approval_package = project / "lib"
    approval_package.mkdir()
    (approval_package / "__init__.py").touch()
    (approval_package / "lifecycle_approval.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "expected = Path(os.environ['EXPECTED_APPROVAL_CWD'])\n"
        "raise SystemExit(0 if Path.cwd() == expected else 91)\n",
        encoding="utf-8",
    )
    config = config_directory / "tofu.conf"
    config.write_text("previous-config\n", encoding="utf-8")
    Path(environment["FAKE_SUPERVISOR_STATE"]).touch()
    environment["EXPECTED_APPROVAL_CWD"] = str(project)

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _parse(config.read_text(encoding="utf-8"))["directory"] == str(project)


def test_system_install_health_failure_restores_previous_config(tmp_path: Path):
    project, config_directory, command, environment = _system_install_fixture(
        tmp_path, health_exit_code=1)
    config = config_directory / "tofu.conf"
    config.write_text("previous-config\n", encoding="utf-8")
    result = subprocess.run(
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert config.read_text(encoding="utf-8") == "previous-config\n"
    assert "previous lifecycle configuration was restored" in result.stderr
    assert not Path(environment["FAKE_SUPERVISOR_STATE"]).exists()
    assert "stop tofu" in Path(environment["FAKE_SUPERVISOR_LOG"]).read_text()


def test_system_install_parse_failure_restores_previous_config(tmp_path: Path):
    project, config_directory, command, environment = _system_install_fixture(
        tmp_path, reread_fail_once=True)
    config = config_directory / "tofu.conf"
    config.write_text("previous-config\n", encoding="utf-8")

    result = subprocess.run(
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 1
    assert config.read_text(encoding="utf-8") == "previous-config\n"
    assert "supervisorctl rejected the rendered config" in result.stderr
    assert "previous lifecycle configuration was restored" in result.stderr


def test_idempotent_install_does_not_wait_on_supervised_listener(tmp_path: Path):
    project, config_directory, command, environment = _system_install_fixture(tmp_path)
    first = subprocess.run(
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert first.returncode == 0, first.stdout + first.stderr

    fake_ss = Path(environment["PATH"].split(os.pathsep, 1)[0]) / "ss"
    _write_executable(
        fake_ss,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' 'LISTEN 0 128 127.0.0.1:15435 0.0.0.0:* "
        'users:(("python",pid=4242,fd=3))\'\n',
    )
    second = subprocess.run(
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert second.returncode == 0, second.stdout + second.stderr
    assert "runtime-healthy" in second.stdout


def test_system_install_handoff_requires_project_lock_identity(tmp_path: Path):
    listener_pid = 777_777
    project, config_directory, command, environment = _system_install_fixture(
        tmp_path,
        listener_pid=listener_pid,
        proven_project_listener=True,
    )
    result = subprocess.run(
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "proven project-local worker" in result.stdout
    calls = Path(environment["FAKE_SERVERCTL_LOG"]).read_text(encoding="utf-8")
    assert "stop --source supervisor-install" in calls
    assert not Path(environment["FAKE_LISTENER_STATE"]).exists()
    assert (config_directory / "tofu.conf").is_file()


def test_system_install_never_stops_an_unknown_port_owner(tmp_path: Path):
    unrelated_process = subprocess.Popen(["sleep", "30"])
    try:
        listener_pid = unrelated_process.pid
        project, config_directory, command, environment = _system_install_fixture(
            tmp_path, listener_pid=listener_pid)
        config = config_directory / "tofu.conf"
        config.write_text("previous-config\n", encoding="utf-8")
        result = subprocess.run(
            command,
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 1
        assert f"refusing to stop unknown listener pid {listener_pid}" in result.stderr
        assert config.read_text(encoding="utf-8") == "previous-config\n"
        assert unrelated_process.poll() is None
    finally:
        unrelated_process.terminate()
        unrelated_process.wait(timeout=5)


@pytest.mark.parametrize("argument", ["--unknown", "--port", "--port=0", "--port=70000"])
def test_installer_rejects_invalid_cli_before_host_changes(tmp_path: Path, argument: str):
    result = subprocess.run(
        ["bash", str(INSTALLER), argument],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert list(tmp_path.iterdir()) == []
