"""DeepSeek Harness Minimal-compatible tools over a Harbor environment.

The model-facing names, descriptions, parameters, output cap, persistent Bash
state, and editor semantics mirror DeepSeek Harness Minimal. Execution remains
inside the disposable rootless-QEMU task container.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import shlex
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.environments.base import BaseEnvironment


MAX_OUTPUT_CHARS = 16_000
MAX_EDIT_FILE_BYTES = 32 * 1024 * 1024
TRUNCATED_MESSAGE = (
    "<response clipped><NOTE>To save on context only part of this file has been "
    "shown to you. You should retry this tool after you have searched inside the "
    "file with `grep -n` in order to find the line numbers of what you are looking "
    "for.</NOTE>"
)
SHELL_RESET_MESSAGE = (
    "The persistent bash shell was reset; the next bash call starts from the "
    "workspace with a fresh current directory and environment."
)

BASH_DESCRIPTION = """Run commands in a bash shell
* When invoking this tool, the contents of the "command" parameter does NOT need to be XML-escaped.
* You don't have access to the internet via this tool.
* You do have access to a mirror of common linux and python packages via apt and pip.
* State is persistent across command calls and discussions with the user.
* To inspect a particular line range of a file, e.g. lines 10-25, try 'sed -n 10,25p /path/to/the/file'.
* Please avoid commands that may produce a very large amount of output.
* Please run long lived commands in the background, e.g. 'sleep 10 &' or start a server in the background."""

EDITOR_DESCRIPTION = """Custom editing tool for viewing, creating and editing files
* State is persistent across command calls and discussions with the user
* If `path` is a file, `view` displays the result of applying `cat -n`. If `path` is a directory, `view` lists non-hidden files and directories up to 2 levels deep
* The `create` command cannot be used if the specified `path` already exists as a file
* If a `command` generates a long output, it will be truncated and marked with `<response clipped>`

Notes for using the `str_replace` command:
* The `old_str` parameter should match EXACTLY one or more consecutive lines from the original file. Be mindful of whitespaces!
* If the `old_str` parameter is not unique in the file, the replacement will not be performed. Make sure to include enough context in `old_str` to make it unique
* The `new_str` parameter should contain the edited lines that should replace the `old_str`"""

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": BASH_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The bash command to run. Relative path is preferred in "
                        "the command."
                    ),
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

STR_REPLACE_EDITOR_TOOL = {
    "type": "function",
    "function": {
        "name": "str_replace_editor",
        "description": EDITOR_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace", "insert"],
                    "description": (
                        "The commands to run. Allowed options are: `view`, "
                        "`create`, `str_replace`, `insert`."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute path to file or directory, e.g. `/repo/file.py` "
                        "or `/repo`."
                    ),
                },
                "file_text": {
                    "type": "string",
                    "description": (
                        "Required parameter of `create` command, with the content "
                        "of the file to be created."
                    ),
                },
                "insert_line": {
                    "type": "integer",
                    "description": (
                        "Required parameter of `insert` command. The `new_str` will "
                        "be inserted AFTER the line `insert_line` of `path`."
                    ),
                },
                "new_str": {
                    "type": "string",
                    "description": (
                        "Optional parameter of `str_replace` command containing "
                        "the new string (if not given, no string will be added). "
                        "Required parameter of `insert` command containing the "
                        "string to insert."
                    ),
                },
                "old_str": {
                    "type": "string",
                    "description": (
                        "Required parameter of `str_replace` command containing "
                        "the string in `path` to replace."
                    ),
                },
                "view_range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Optional parameter of `view` command when `path` points "
                        "to a file. If none is given, the full file is shown. If "
                        "provided, the file will be shown in the indicated line "
                        "number range, e.g. [11, 12] will show lines 11 and 12. "
                        "Indexing at 1 to start. Setting `[start_line, -1]` shows "
                        "all lines from `start_line` to the end of the file."
                    ),
                },
            },
            "required": ["command", "path"],
            "additionalProperties": False,
        },
    },
}

MINIMAL_TOOLS = [BASH_TOOL, STR_REPLACE_EDITOR_TOOL]


def _maybe_truncate(content: str) -> str:
    return content if len(content) <= MAX_OUTPUT_CHARS else content[:MAX_OUTPUT_CHARS] + TRUNCATED_MESSAGE


def _ansi_c_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return "$'" + escaped + "'"


class PersistentBash:
    """One owner-scoped persistent Bash session inside the task container."""

    def __init__(
        self,
        environment: BaseEnvironment,
        *,
        timeout_sec: int = 300,
        poll_interval_sec: float = 0.25,
    ) -> None:
        self._environment = environment
        self._timeout_sec = max(1, min(1800, int(timeout_sec)))
        self._poll_interval_sec = max(0.05, float(poll_interval_sec))
        self._token = secrets.token_hex(16)
        self._root = PurePosixPath(f"/tmp/deepseek-minimal-bash-{self._token}")
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def root(self) -> PurePosixPath:
        return self._root

    async def _start(self) -> None:
        fifo = self._root / "input.fifo"
        output = self._root / "output.log"
        shell_pid = self._root / "shell.pid"
        keeper_pid = self._root / "keeper.pid"
        command = (
            "set -eu; "
            f"rm -rf {shlex.quote(str(self._root))}; "
            f"mkdir -m 700 {shlex.quote(str(self._root))}; "
            f"mkfifo -m 600 {shlex.quote(str(fifo))}; "
            f": > {shlex.quote(str(output))}; "
            "( setsid /bin/bash --noprofile --norc "
            f"< {shlex.quote(str(fifo))} >> {shlex.quote(str(output))} 2>&1 & "
            f"echo $! > {shlex.quote(str(shell_pid))} ); "
            "( setsid tail -f /dev/null "
            f"> {shlex.quote(str(fifo))} 2>/dev/null & "
            f"echo $! > {shlex.quote(str(keeper_pid))} ); "
            "sleep 0.1; "
            f"kill -0 \"$(cat {shlex.quote(str(shell_pid))})\""
        )
        result = await self._environment.exec(command, timeout_sec=10)
        if result.return_code != 0:
            detail = (result.stderr or result.stdout or "no output")[-2000:]
            raise RuntimeError(f"could not start persistent bash: {detail}")
        self._started = True

    async def _status(self, end_marker: str) -> int | None:
        output = self._root / "output.log"
        result = await self._environment.exec(
            "grep -F "
            + shlex.quote(end_marker)
            + " "
            + shlex.quote(str(output))
            + " | tail -n 1",
            timeout_sec=10,
        )
        if result.return_code != 0 or not result.stdout:
            return None
        tail = result.stdout.strip().rsplit(end_marker, 1)[-1]
        try:
            return int(tail)
        except ValueError:
            return None

    async def _shell_alive(self) -> bool:
        shell_pid = self._root / "shell.pid"
        result = await self._environment.exec(
            f"test -s {shlex.quote(str(shell_pid))} && "
            f"kill -0 \"$(cat {shlex.quote(str(shell_pid))})\"",
            timeout_sec=10,
        )
        return result.return_code == 0

    async def _capture(self, start_marker: str, end_marker: str) -> str:
        output = self._root / "output.log"
        # UUID markers cannot occur in model-provided output. awk stops at the
        # completion marker and head bounds the QGA transfer itself.
        command = (
            "awk -v start="
            + shlex.quote(start_marker)
            + " -v end="
            + shlex.quote(end_marker)
            + " '$0 == start {seen=1; next} seen && index($0,end)==1 {exit} "
            + "seen {print}' "
            + shlex.quote(str(output))
            + f" | head -c {MAX_OUTPUT_CHARS + 1}"
        )
        result = await self._environment.exec(command, timeout_sec=20)
        rendered = result.stdout or ""
        return _maybe_truncate(rendered[:-1] if rendered.endswith("\n") else rendered)

    async def _reset(self) -> None:
        if not self._started:
            return
        shell_pid = self._root / "shell.pid"
        keeper_pid = self._root / "keeper.pid"
        command = (
            "for path in "
            f"{shlex.quote(str(shell_pid))} {shlex.quote(str(keeper_pid))}; do "
            'if test -s "$path"; then '
            'pid=$(cat "$path"); '
            'case "$pid" in ""|*[!0-9]*) continue ;; esac; '
            # Both processes are session/process-group leaders. Targeting the
            # complete group is essential: killing only Bash leaves a timed-out
            # compiler, package manager, or training job alive in the guest.
            'kill -TERM -- "-$pid" 2>/dev/null || true; '
            "fi; "
            "done; "
            "sleep 0.2; "
            "for path in "
            f"{shlex.quote(str(shell_pid))} {shlex.quote(str(keeper_pid))}; do "
            'if test -s "$path"; then '
            'pid=$(cat "$path"); '
            'case "$pid" in ""|*[!0-9]*) continue ;; esac; '
            'kill -KILL -- "-$pid" 2>/dev/null || true; '
            "fi; "
            "done; "
            f"rm -rf {shlex.quote(str(self._root))}"
        )
        await self._environment.exec(command, timeout_sec=10)
        self._started = False

    async def run(self, command: str) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Parameter `command` is required for command: bash")
        if len(command) > 256 * 1024:
            raise ValueError("bash command exceeds the 256 KiB safety limit")
        async with self._lock:
            if not self._started:
                await self._start()
            nonce = secrets.token_hex(16)
            start_marker = f"__DSH_PERSISTENT_BASH_START_{nonce}__"
            end_marker = f"__DSH_PERSISTENT_BASH_END_{nonce}:"
            command_file = self._root / f"command-{nonce}.sh"
            wrapper = (
                f"printf '%s\\n' {_ansi_c_quote(start_marker)}; "
                f"eval -- {_ansi_c_quote(command)}; "
                "__dsh_persistent_bash_status=$?; "
                f"printf '%s%s\\n' {_ansi_c_quote(end_marker)} "
                '"$__dsh_persistent_bash_status"\n'
            )
            with tempfile.TemporaryDirectory(prefix="deepseek-minimal-command-") as temp:
                local = Path(temp) / "command.sh"
                local.write_text(wrapper, encoding="utf-8")
                local.chmod(0o600)
                await self._environment.upload_file(local, str(command_file))
            output = self._root / "output.log"
            fifo = self._root / "input.fifo"
            submitted = await self._environment.exec(
                f": > {shlex.quote(str(output))}; "
                f"printf '%s\\n' {shlex.quote('source ' + str(command_file))} "
                f"> {shlex.quote(str(fifo))}",
                timeout_sec=10,
            )
            if submitted.return_code != 0:
                await self._reset()
                detail = submitted.stderr or submitted.stdout or "no output"
                raise RuntimeError(f"persistent bash command submission failed: {detail}")
            deadline = time.monotonic() + self._timeout_sec
            while time.monotonic() < deadline:
                status = await self._status(end_marker)
                if status is not None:
                    rendered = await self._capture(start_marker, end_marker)
                    await self._environment.exec(
                        f"rm -f {shlex.quote(str(command_file))}", timeout_sec=10
                    )
                    if status != 0:
                        marker = f"[exit code: {status}]"
                        rendered = marker if not rendered else f"{rendered}\n{marker}"
                    return rendered
                if not await self._shell_alive():
                    rendered = await self._capture(start_marker, end_marker)
                    await self._reset()
                    parts = [rendered, "[shell exited]", SHELL_RESET_MESSAGE]
                    return "\n".join(part for part in parts if part)
                await asyncio.sleep(self._poll_interval_sec)
            rendered = await self._capture(start_marker, end_marker)
            await self._reset()
            parts = [
                rendered,
                f"[command timed out after {self._timeout_sec} seconds]",
                SHELL_RESET_MESSAGE,
            ]
            return "\n".join(part for part in parts if part)

    async def close(self) -> None:
        async with self._lock:
            await self._reset()


class StrReplaceEditor:
    """Official Minimal editor semantics over Harbor file transfer methods."""

    def __init__(self, environment: BaseEnvironment) -> None:
        self._environment = environment

    @staticmethod
    def _path(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("path must be a non-empty string")
        path = PurePosixPath(value)
        if not path.is_absolute():
            raise ValueError(
                f"The path {value} is not an absolute path, it should start with `/`. "
                f"Maybe you meant /{value}?"
            )
        return str(path)

    async def _kind(self, path: str) -> tuple[str, int | None, int | None]:
        quoted = shlex.quote(path)
        result = await self._environment.exec(
            f"if test -d {quoted}; then printf 'directory'; "
            f"elif test -f {quoted}; then printf 'file|'; stat -c '%s|%a' {quoted}; "
            f"elif test -e {quoted}; then printf 'other'; else printf 'absent'; fi",
            timeout_sec=20,
        )
        if result.return_code != 0:
            raise RuntimeError(result.stderr or result.stdout or "could not stat path")
        fields = (result.stdout or "absent").strip().split("|")
        if fields[0] != "file":
            return fields[0], None, None
        return "file", int(fields[1]), int(fields[2], 8)

    async def _read_file(self, path: str) -> tuple[str, int]:
        kind, size, mode = await self._kind(path)
        if kind == "absent":
            raise ValueError(f"The path {path} does not exist. Please provide a valid path.")
        if kind != "file" or size is None or mode is None:
            raise ValueError(f'cannot view "{path}": not a regular file')
        if size > MAX_EDIT_FILE_BYTES:
            raise ValueError(f"The file {path} exceeds the 32 MiB editor safety limit.")
        with tempfile.TemporaryDirectory(prefix="deepseek-minimal-editor-") as temp:
            local = Path(temp) / "file"
            await self._environment.download_file(path, local)
            try:
                content = local.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"The file {path} is not valid UTF-8 text.") from exc
        return content, mode

    async def _replace_file(self, path: str, content: str, mode: int) -> None:
        token = secrets.token_hex(12)
        target = PurePosixPath(path)
        remote_temp = str(target.parent / f".{target.name}.dsh-{token}.partial")
        with tempfile.TemporaryDirectory(prefix="deepseek-minimal-editor-") as temp:
            local = Path(temp) / "file"
            local.write_text(content, encoding="utf-8")
            local.chmod(mode)
            await self._environment.upload_file(local, remote_temp)
        result = await self._environment.exec(
            f"chmod {mode:o} {shlex.quote(remote_temp)} && "
            f"mv -f {shlex.quote(remote_temp)} {shlex.quote(path)}",
            timeout_sec=30,
        )
        if result.return_code != 0:
            await self._environment.exec(
                f"rm -f {shlex.quote(remote_temp)}", timeout_sec=10
            )
            raise RuntimeError(result.stderr or result.stdout or "file update failed")

    async def _view_directory(self, path: str) -> str:
        quoted = shlex.quote(path)
        command = (
            f"find {quoted} -mindepth 0 -maxdepth 2 "
            "\\( -name '.*' -o -name node_modules -o -name __pycache__ \\) -prune -o "
            "-printf '%y\\t%p\\n' | LC_ALL=C sort"
        )
        result = await self._environment.exec(command, timeout_sec=30)
        if result.return_code != 0:
            raise RuntimeError(result.stderr or result.stdout or "directory view failed")
        listing = _maybe_truncate(result.stdout or "")
        return (
            "Here're the files and directories up to 2 levels deep in "
            f"{path}, excluding hidden items, node_modules, and Python cache "
            f"directories:\n{listing}\n"
        )

    async def _view(self, path: str, view_range: Any) -> str:
        kind, _size, _mode = await self._kind(path)
        if kind == "absent":
            raise ValueError(f"The path {path} does not exist. Please provide a valid path.")
        if kind == "directory":
            if view_range is not None:
                raise ValueError(
                    "The `view_range` parameter is not allowed when `path` points "
                    "to a directory."
                )
            return await self._view_directory(path)
        content, _mode = await self._read_file(path)
        all_lines = content.split("\n")
        lines = all_lines
        initial = 1
        final: int | None = None
        prompt = (
            f"Here's the content of {path} with line numbers (which has a total "
            f"of {len(all_lines)} lines)"
        )
        if view_range is not None:
            if (
                not isinstance(view_range, list)
                or len(view_range) != 2
                or any(not isinstance(value, int) or isinstance(value, bool) for value in view_range)
            ):
                raise ValueError("Invalid `view_range`. It should be a list of two integers.")
            initial, final = view_range
            if initial < 1 or initial > len(all_lines):
                raise ValueError(
                    f"Invalid `view_range`: {view_range}. Its first element `{initial}` "
                    "should be within the range of lines of the file: "
                    f"[1, {len(all_lines)}]"
                )
            if final > len(all_lines):
                raise ValueError(
                    f"Invalid `view_range`: {view_range}. Its second element `{final}` "
                    "should be smaller than the number of lines in the file: "
                    f"`{len(all_lines)}`"
                )
            if final != -1 and final < initial:
                raise ValueError(
                    f"Invalid `view_range`: {view_range}. Its second element `{final}` "
                    f"should be larger or equal than its first `{initial}`"
                )
            lines = all_lines[initial - 1 :] if final == -1 else all_lines[initial - 1 : final]
            prompt += f" with view_range=[{initial}, {final}]"
        numbered = "\n".join(
            f"{initial + index:6d}  {line}" for index, line in enumerate(lines)
        )
        return _maybe_truncate(f"{prompt}:\n{numbered}\n")

    async def _create(self, path: str, file_text: Any) -> str:
        if not isinstance(file_text, str):
            raise ValueError("Parameter `file_text` is required for command: create")
        kind, _size, _mode = await self._kind(path)
        if kind != "absent":
            raise ValueError(
                f"File already exists at: {path}. Cannot overwrite files using "
                "command `create`."
            )
        target = PurePosixPath(path)
        token = secrets.token_hex(12)
        remote_temp = str(target.parent / f".{target.name}.dsh-{token}.partial")
        with tempfile.TemporaryDirectory(prefix="deepseek-minimal-editor-") as temp:
            local = Path(temp) / "file"
            local.write_text(file_text, encoding="utf-8")
            local.chmod(0o644)
            await self._environment.upload_file(local, remote_temp)
        result = await self._environment.exec(
            f"ln {shlex.quote(remote_temp)} {shlex.quote(path)} && "
            f"rm -f {shlex.quote(remote_temp)}",
            timeout_sec=30,
        )
        if result.return_code != 0:
            await self._environment.exec(
                f"rm -f {shlex.quote(remote_temp)}", timeout_sec=10
            )
            raise ValueError(
                f"File already exists at: {path}. Cannot overwrite files using "
                "command `create`."
            )
        return f"New file created successfully at: {path}"

    async def _str_replace(self, path: str, old_str: Any, new_str: Any) -> str:
        if not isinstance(old_str, str):
            raise ValueError("Parameter `old_str` is required for command: str_replace")
        if not old_str:
            raise ValueError("Parameter `old_str` is empty for command: str_replace")
        if new_str is None:
            new_str = ""
        if not isinstance(new_str, str):
            raise ValueError("Parameter `new_str` must be a string")
        before, mode = await self._read_file(path)
        offsets = []
        cursor = 0
        while True:
            offset = before.find(old_str, cursor)
            if offset < 0:
                break
            offsets.append(offset)
            cursor = offset + len(old_str)
        if not offsets:
            raise ValueError(
                f"No replacement was performed, old_str `{old_str}` did not "
                f"appear verbatim in {path}."
            )
        if len(offsets) > 1:
            lines = [before.count("\n", 0, offset) + 1 for offset in offsets]
            raise ValueError(
                f"No replacement was performed. Multiple occurrences of old_str "
                f"`{old_str}` in lines [{', '.join(map(str, lines))}]. Please "
                "ensure it is unique"
            )
        offset = offsets[0]
        after = before[:offset] + new_str + before[offset + len(old_str) :]
        await self._replace_file(path, after, mode)
        return f"The file {path} has been edited successfully."

    async def _insert(self, path: str, insert_line: Any, new_str: Any) -> str:
        if not isinstance(insert_line, int) or isinstance(insert_line, bool):
            raise ValueError("Parameter `insert_line` is required for command: insert")
        if not isinstance(new_str, str):
            raise ValueError("Parameter `new_str` is required for command: insert")
        before, mode = await self._read_file(path)
        lines = before.split("\n")
        if insert_line < 0 or insert_line > len(lines):
            raise ValueError(
                f"Invalid `insert_line` parameter: {insert_line}. It should be "
                f"within the range of lines of the file: [0, {len(lines)}]"
            )
        after = "\n".join(
            [*lines[:insert_line], *new_str.split("\n"), *lines[insert_line:]]
        )
        await self._replace_file(path, after, mode)
        return f"The file {path} has been edited successfully."

    async def run(self, arguments: dict[str, Any]) -> str:
        command = arguments.get("command")
        path = self._path(arguments.get("path"))
        if command == "view":
            return await self._view(path, arguments.get("view_range"))
        if command == "create":
            return await self._create(path, arguments.get("file_text"))
        if command == "str_replace":
            return await self._str_replace(
                path, arguments.get("old_str"), arguments.get("new_str")
            )
        if command == "insert":
            return await self._insert(
                path, arguments.get("insert_line"), arguments.get("new_str")
            )
        raise ValueError(
            "Parameter `command` must be one of: view, create, str_replace, insert"
        )


def tool_schema_digest() -> str:
    """Stable audit fingerprint for the exact model-facing Minimal surface."""

    rendered = json.dumps(MINIMAL_TOOLS, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode()).hexdigest()
