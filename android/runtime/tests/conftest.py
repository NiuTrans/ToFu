"""Options for the runtime image tests (android/runtime/README.md §8).

Unit tests run with no options. Tests over built artifacts need the output
directories of ``build_runtime.py build``:

    python -m pytest android/runtime/tests \
        --runtime-out <out>/arm64-v8a [--runtime-out-repeat <out2>/arm64-v8a] \
        [--runtime-host-payload <out>/x86_64]
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_addoption(parser):
    group = parser.getgroup('android-runtime')
    group.addoption('--runtime-out', type=Path,
                    help='<out>/arm64-v8a of a build: checks the contract layout')
    group.addoption('--runtime-out-repeat', type=Path,
                    help='a second build from the same inputs: checks byte identity')
    group.addoption('--runtime-host-payload', type=Path,
                    help='<out>/x86_64 of a build: boots the payload on this host')


@pytest.fixture(scope='session')
def runtime_out(request):
    path = request.config.getoption('--runtime-out')
    if path is None:
        pytest.skip('needs --runtime-out <out>/arm64-v8a')
    return path


@pytest.fixture(scope='session')
def runtime_out_repeat(request):
    path = request.config.getoption('--runtime-out-repeat')
    if path is None:
        pytest.skip('needs --runtime-out-repeat <second out>/arm64-v8a')
    return path


@pytest.fixture(scope='session')
def host_payload(request):
    path = request.config.getoption('--runtime-host-payload')
    if path is None:
        pytest.skip('needs --runtime-host-payload <out>/x86_64')
    return path
