#!/usr/bin/env python3
"""Render one target-host supervisord program from the host-neutral template.

Responsibility: validate deployment identity and paths, encode supervisord
command/environment values, and publish one complete config atomically.
Entrypoint: ``main``. Dependencies: Python standard library and
``tofu.conf.template`` only; importing this module has no side effects.
"""

from __future__ import annotations

import argparse
import configparser
import ipaddress
import os
from pathlib import Path
import pwd
import shlex
import sys
import tempfile


TEMPLATE_PATH = Path(__file__).with_name("tofu.conf.template")
PLACEHOLDERS = {
    "@@TOFU_COMMAND@@",
    "@@TOFU_PROJECT_DIR@@",
    "@@TOFU_RUN_USER@@",
    "@@TOFU_ENVIRONMENT@@",
    "@@TOFU_LOG_PATH@@",
}


class ConfigRenderError(ValueError):
    """The requested host values cannot be represented safely."""


def _absolute_path(value: str, *, label: str) -> Path:
    if any(character in value for character in ("\0", "\r", "\n", ";")):
        raise ConfigRenderError(
            f"{label} contains a character supervisord cannot encode safely")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigRenderError(f"{label} must be an absolute path: {value!r}")
    return path


def _supervisor_percent_escape(value: str, *, expansion_passes: int = 1) -> str:
    """Protect literal percent signs across Supervisor's expansion passes."""
    if expansion_passes < 1:
        raise ValueError("expansion_passes must be positive")
    # Most program options are expanded once. Supervisor expands logfile
    # values twice (saneget, then per-process logfile expansion), including in
    # current releases, so each literal percent needs 2**passes copies.
    return value.replace("%", "%" * (2 ** expansion_passes))


def _environment_value(value: str, *, label: str) -> str:
    if any(character in value for character in ("\0", "\r", "\n", ";")):
        raise ConfigRenderError(
            f"{label} contains a character supervisord cannot encode safely")
    escaped = _supervisor_percent_escape(value)
    # Supervisor's environment parser uses non-POSIX shlex: backslashes are
    # literal, and it has no portable escape for a delimiter occurring inside
    # its own quoted value. Choose the other quote style or fail closed when a
    # value contains both styles.
    if '"' not in escaped:
        return f'"{escaped}"'
    if "'" not in escaped:
        return f"'{escaped}'"
    raise ConfigRenderError(
        f"{label} contains both quote styles and supervisord cannot encode it safely")


def _command_argument(value: str, *, label: str) -> str:
    if any(character in value for character in ("\0", "\r", "\n", ";")):
        # Supervisor truncates command= at a semicolon even inside quotes.
        raise ConfigRenderError(
            f"{label} contains a character supervisord command= cannot encode safely")
    return _supervisor_percent_escape(shlex.quote(value))


def _require_target_user(user_name: str) -> None:
    if not user_name or any(character in user_name for character in "\0\r\n;,%"):
        raise ConfigRenderError(f"invalid target user: {user_name!r}")
    try:
        pwd.getpwnam(user_name)
    except KeyError as exc:
        raise ConfigRenderError(
            f"target user does not exist on this host: {user_name!r}") from exc


def render_config(
    *,
    project_root: Path,
    python_executable: Path,
    user_name: str,
    home_directory: Path,
    port: int,
    bind_host: str = "0.0.0.0",
    browser_libraries_directory: Path | None = None,
    template_path: Path = TEMPLATE_PATH,
) -> str:
    """Return a complete validated program config for one target host."""
    project_root = _absolute_path(str(project_root), label="project root")
    python_executable = _absolute_path(
        str(python_executable), label="Python executable")
    home_directory = _absolute_path(str(home_directory), label="home directory")
    _require_target_user(user_name)

    if not (project_root / "server.py").is_file():
        raise ConfigRenderError(
            f"project root does not contain server.py: {project_root}")
    if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
        raise ConfigRenderError(
            f"Python executable is missing or not executable: {python_executable}")
    if not home_directory.is_dir():
        raise ConfigRenderError(f"home directory does not exist: {home_directory}")
    if not 1 <= port <= 65535:
        raise ConfigRenderError(f"port must be in 1..65535: {port}")
    try:
        ipaddress.ip_address(bind_host)
    except ValueError as exc:
        raise ConfigRenderError(
            f"bind host must be a literal IPv4 or IPv6 address: {bind_host!r}") from exc

    environment = [
        ("PORT", str(port)),
        ("BIND_HOST", bind_host),
        ("HOME", str(home_directory)),
        ("PYTHONUTF8", "1"),
        ("TOFU_SERVER_WORKER", "1"),
        ("TOFU_MANAGED_BY", "root-supervisord"),
    ]
    if browser_libraries_directory is not None:
        browser_libraries_directory = _absolute_path(
            str(browser_libraries_directory), label="browser libraries directory")
        library_directory = browser_libraries_directory / "lib"
        fonts_directory = browser_libraries_directory / "etc" / "fonts"
        fonts_configuration = fonts_directory / "fonts.conf"
        if library_directory.is_dir():
            environment.append(("CHROMIUM_EXTRA_LIB_DIRS", str(library_directory)))
        if fonts_configuration.is_file():
            environment.extend((
                ("FONTCONFIG_PATH", str(fonts_directory)),
                ("FONTCONFIG_FILE", str(fonts_configuration)),
            ))

    environment_text = ",".join(
        f"{name}={_environment_value(value, label=name)}"
        for name, value in environment
    )
    replacements = {
        "@@TOFU_COMMAND@@": " ".join((
            _command_argument(str(python_executable), label="Python executable"),
            _command_argument("server.py", label="server entrypoint"),
        )),
        "@@TOFU_PROJECT_DIR@@": _supervisor_percent_escape(str(project_root)),
        "@@TOFU_RUN_USER@@": user_name,
        "@@TOFU_ENVIRONMENT@@": environment_text,
        "@@TOFU_LOG_PATH@@": _supervisor_percent_escape(
            str(project_root / "logs" / "supervisor_tofu.log"),
            expansion_passes=2,
        ),
    }

    try:
        rendered = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigRenderError(f"cannot read template {template_path}: {exc}") from exc
    observed = {token for token in PLACEHOLDERS if token in rendered}
    if observed != PLACEHOLDERS:
        missing = sorted(PLACEHOLDERS - observed)
        raise ConfigRenderError(f"template is missing placeholders: {missing}")
    for token in PLACEHOLDERS:
        if rendered.count(token) != 1:
            raise ConfigRenderError(
                f"template placeholder must occur exactly once: {token}")
        rendered = rendered.replace(token, replacements[token])
    if "@@TOFU_" in rendered:
        raise ConfigRenderError("template contains an unresolved Tofu placeholder")

    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(rendered)
    except configparser.Error as exc:
        raise ConfigRenderError(f"rendered config is not valid INI: {exc}") from exc
    if not parser.has_section("program:tofu"):
        raise ConfigRenderError("rendered config is missing [program:tofu]")
    return rendered


def write_atomic(output_path: Path, content: str) -> None:
    """Publish content without exposing a partial config to another reader."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", dir=output_path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render Tofu's host-neutral supervisord program template.")
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path,
                        dest="python_executable")
    parser.add_argument("--user", required=True, dest="user_name")
    parser.add_argument("--home", required=True, type=Path,
                        dest="home_directory")
    parser.add_argument("--port", type=int, default=15000)
    parser.add_argument("--bind-host", default="0.0.0.0")
    parser.add_argument("--browser-libs-dir", type=Path, default=None)
    parser.add_argument(
        "--output", type=Path,
        help="atomic output path; omit to write the rendered config to stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        rendered = render_config(
            project_root=args.project_root,
            python_executable=args.python_executable,
            user_name=args.user_name,
            home_directory=args.home_directory,
            port=args.port,
            bind_host=args.bind_host,
            browser_libraries_directory=args.browser_libs_dir,
        )
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            write_atomic(args.output, rendered)
    except (ConfigRenderError, OSError) as exc:
        print(f"render_config.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
