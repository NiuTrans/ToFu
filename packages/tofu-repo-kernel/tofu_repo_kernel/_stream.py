"""Caller-owned sealed repository-stream executable; no parser or child pool.

StreamExecutable.start returns one child owned by the caller. The caller must
validate frames, enforce the overall deadline/cancellation and join every child
before closing this image. A grammar directory is an explicit trusted input.
None selects a pinned installed bundle, sealed lazily inside that owned child.
"""
import os
from pathlib import Path
import subprocess
import sys
from threading import Lock

from ._process_discovery import discovery_environment, seal_executable


class StreamExecutable:
    def __init__(self, path, expected_sha256, *, installed_grammars=None):
        path = Path(path).resolve()
        self.path = str(path)
        self._installed_grammars = installed_grammars
        self._lock = Lock()
        self._image = seal_executable(path, expected_sha256)

    def close(self):
        with self._lock:
            self._image.close()

    def __enter__(self):
        return self

    def __exit__(self, _kind, primary, _traceback):
        errors = []
        while True:
            try:
                self.close()
                break
            except KeyboardInterrupt as error:
                errors.append(error)
            except BaseException as error:
                errors.append(error)
                break
        if errors:
            if primary is None:
                raise errors[0]
            if hasattr(primary, 'add_note'):
                primary.add_note('native stream image close: ' + ', '.join(type(error).__name__ for error in errors))

    def start(self, root, grammar_directory, timeout_ns, limits, *, environment=None):
        root = os.path.abspath(os.fsdecode(root))
        grammar_arguments = []
        if grammar_directory is None:
            if self._installed_grammars is None:
                raise ValueError('native repository package has no installed grammar bundle')
            grammar_directory, grammar_digest, grammar_bytes = self._installed_grammars
            grammar_arguments = ['--grammar-bundle-v1', grammar_digest, str(grammar_bytes)]
        grammar_directory = os.path.abspath(os.fsdecode(grammar_directory))
        if '\0' in root or '\0' in grammar_directory:
            raise ValueError('native stream paths cannot contain NUL')
        if type(timeout_ns) is not int or not 0 <= timeout_ns <= (1 << 64) - 1:
            raise ValueError('native stream timeout must fit unsigned nanoseconds')
        limits = tuple(limits)
        if len(limits) != 4 or any(type(value) is not int or not 0 < value < sys.maxsize for value in limits):
            raise ValueError('native stream needs four positive bounded byte limits')
        return self._start([root, grammar_directory, str(timeout_ns),
                            *(str(value) for value in limits), '--discovery-index-v1', *grammar_arguments], environment)

    def start_grammar_check(self):
        """Return an owned certification child; callers still enforce/join exit."""
        if self._installed_grammars is None:
            raise ValueError('native repository package has no installed grammar bundle')
        directory, digest, size = self._installed_grammars
        return self._start(['--check-grammar-bundle-v1', str(directory), digest, str(size)], None)

    def start_rg(self, root, *, environment=None):
        root = os.path.abspath(os.fsdecode(root))
        if '\0' in root:
            raise ValueError('native discovery root cannot contain NUL')
        return self._start(['--list-rg', root], environment)

    def _start(self, arguments, environment):
        # Hold the original image through Popen's fd inheritance handoff. The
        # explicit task/runtime owner retains it until all children have joined.
        with self._lock:
            descriptor = self._image.fileno()
            command = [f'/proc/self/fd/{descriptor}', *arguments]
            return subprocess.Popen(command, pass_fds=(descriptor,), env=discovery_environment(environment),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=0)
