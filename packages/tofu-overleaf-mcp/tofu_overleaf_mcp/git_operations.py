"""Bounded Git request admission, private IPC and disposable workspace ownership.

The MCP server routes every Git-backed tool through invoke. One request owns its
worker until the process tree is reaped and its workspace removed. Upstream's
historical cache is never opened, reset, published or reclaimed by this owner.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time

from jsonschema import Draft202012Validator

from .git_process import ProcessTree
from .git_resources import probe_budget
from .git_workspace import create_workspace, record_worker, retire_workspace
from .writes import CONTROL, WriteError, contract, definition

logger = logging.getLogger(__name__)


def git_tools():
    return {*contract()['x-mcp-write-tools'], *contract()['x-mcp-git-read-tools']}


def workspace_usage(root, maximum_files):
    """Never follow source symlinks or enumerate an unbounded directory list."""
    size = count = 0
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > maximum_files:
                    raise WriteError('git_workspace_file_limit')
                try:
                    info = entry.stat(follow_symlinks=False)
                    size += info.st_size
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                except FileNotFoundError:
                    pass  # Atomic Git index/lock replacement during observation.
    return size, count


def write_request(path, value, maximum):
    size = 0
    with path.open('xb') as stream:
        for chunk in json.JSONEncoder(ensure_ascii=False, separators=(',', ':')).iterencode(value):
            data = chunk.encode()
            size += len(data)
            if size > maximum:
                raise WriteError('git_request_limit')
            stream.write(data)


def validate_arguments(arguments):
    """The pinned Git tools have flat inputs plus the optional receipt control."""
    if not isinstance(arguments, dict) or len(arguments) > 32:
        raise WriteError('git_arguments_invalid')
    project = arguments.get('project_id')
    if not isinstance(project, str) or re.fullmatch('[0-9a-f]{24}', project) is None:
        raise WriteError('write_project_invalid')
    maximum = contract()['x-resource-limits']['overleaf_file_bytes']
    for key, value in arguments.items():
        if not isinstance(key, str) or len(key) > 128:
            raise WriteError('git_arguments_invalid')
        if key == CONTROL:
            Draft202012Validator(definition('OverleafWriteControlV1')).validate(value)
        elif isinstance(value, str):
            if len(value) > maximum or len(value.encode()) > maximum:
                raise WriteError('write_file_limit')
        elif value is not None and type(value) not in (int, bool):
            raise WriteError('git_arguments_invalid')


class GitOperations:
    def __init__(self, *, budget=None, command=None, temporary_root=None, seconds=None):
        self.budget = probe_budget() if budget is None else budget
        limits = contract()['x-resource-limits']
        if (type(self.budget.memory_bytes) is not int or not 128 * 1024**2 <= self.budget.memory_bytes <= limits['overleaf_git_memory_bytes']
                or type(self.budget.workspace_bytes) is not int or not 0 <= self.budget.workspace_bytes <= limits['overleaf_git_workspace_bytes']):
            raise WriteError('git_budget_invalid')
        self.command = tuple(command or (sys.executable, '-m', 'tofu_overleaf_mcp.git_worker'))
        self.temporary_root = temporary_root
        self.seconds = contract()['x-resource-limits']['overleaf_write_seconds'] if seconds is None else seconds
        if not 0 < self.seconds <= contract()['x-resource-limits']['overleaf_write_seconds']:
            raise WriteError('git_budget_invalid')
        self.active = None
        self.sessions = 0
        self.last_usage = None

    @asynccontextmanager
    async def session(self):
        self.sessions += 1
        try:
            yield self
        finally:
            self.sessions -= 1
            if self.sessions == 0:
                await self.aclose()

    async def aclose(self):
        active = self.active
        if active is not None and active is not asyncio.current_task():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)

    async def invoke(self, name, arguments):
        if self.active is not None:
            raise WriteError('git_busy')
        if name not in git_tools():
            raise WriteError('git_tool_invalid')
        validate_arguments(arguments)
        self.active = asyncio.current_task()
        limits = contract()['x-resource-limits']
        started = time.monotonic()
        root = process = tree = None
        usage = {'budget': self.budget.report(), 'peak_memory_bytes': 0,
                 'peak_workspace_bytes': 0, 'peak_processes': 0}
        try:
            root = create_workspace(self.temporary_root)
            write_request(root / 'request.json', {'name': name, 'arguments': arguments,
                'memory_bytes': self.budget.memory_bytes, 'seconds': self.seconds,
                'workspace_bytes': self.budget.workspace_bytes}, limits['overleaf_git_request_bytes'])
            if workspace_usage(root, limits['overleaf_git_workspace_files'])[0] > self.budget.workspace_bytes:
                raise WriteError('git_workspace_limit')
            process = subprocess.Popen([*self.command, str(root)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt')
            tree = ProcessTree(process, self.budget.memory_bytes)
            record_worker(root, process.pid)
            (root / 'admitted').touch()
            while process.poll() is None:
                memory, processes = tree.sample()
                size, _ = workspace_usage(root, limits['overleaf_git_workspace_files'])
                usage['peak_memory_bytes'] = max(usage['peak_memory_bytes'], memory)
                usage['peak_workspace_bytes'] = max(usage['peak_workspace_bytes'], size)
                usage['peak_processes'] = max(usage['peak_processes'], processes)
                if memory > self.budget.memory_bytes:
                    raise WriteError('git_memory_limit')
                if processes > limits['overleaf_git_processes']:
                    raise WriteError('git_process_limit')
                if size > self.budget.workspace_bytes:
                    raise WriteError('git_workspace_limit')
                if time.monotonic() - started >= self.seconds:
                    raise TimeoutError('git_timeout')
                await asyncio.sleep(0.1)
            if process.returncode != 0:
                raise WriteError('git_worker_failed')
            size, _ = workspace_usage(root, limits['overleaf_git_workspace_files'])
            if size > self.budget.workspace_bytes:
                raise WriteError('git_workspace_limit')
            report = root / 'result.json'
            with report.open('rb') as stream:
                data = stream.read(limits['overleaf_git_response_bytes'] + 1)
            if len(data) > limits['overleaf_git_response_bytes']:
                raise WriteError('git_response_limit')
            result = json.loads(data)
            if result.get('ok') is not True:
                raise WriteError(result.get('error', 'git_operation_unconfirmed'))
            value = result['result']
            if name in contract()['x-mcp-write-tools']:
                Draft202012Validator(definition('OverleafWriteReceiptV1')).validate(value)
            elif not isinstance(value, str):
                raise WriteError('git_result_invalid')
            return value
        finally:
            try:
                if tree is not None:
                    tree.close()
                elif process is not None:
                    process.kill()
                    process.wait(timeout=5)
            finally:
                try:
                    if root is not None:
                        try:
                            retire_workspace(root)
                            shutil.rmtree(root)
                        except OSError:
                            # An acknowledged write remains acknowledged even
                            # if crash-reclaimable cache cleanup needs retrying.
                            usage['cleanup_pending'] = True
                            logger.warning('Git workspace cleanup deferred')
                finally:
                    usage['elapsed_seconds'] = time.monotonic() - started
                    self.last_usage = usage
                    self.active = None
