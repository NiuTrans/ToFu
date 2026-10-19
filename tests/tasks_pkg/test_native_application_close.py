"""Control failures reach lifespan cleanup without escaping a worker Task.

The subprocess keeps an intentionally broken event loop from interrupting the
test runner. No native model, repository scan or provider work is started.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

PROGRAM = r'''
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from lib.app.app_factory import create_base_app
from lib.app.app_lifecycle import add_shutdown_handler
from lib.tasks_pkg.native_application import NativeRootConfiguration, register_native_application
from lib.tasks_pkg.native_host import register_native_host
from tofu_agent import NativeRepositoryConfiguration
from tests.tasks_pkg.test_native_application import profile
from tests.tasks_pkg.test_native_host import image

failure_type = {'interrupt': KeyboardInterrupt, 'exit': SystemExit,
                'cancel': asyncio.CancelledError, 'ordinary': OSError}[sys.argv[1]]
failure = failure_type('repository cleanup fault')
events = []
app = create_base_app('native-close-control')
add_shutdown_handler(app, lambda: events.append('dependency'), name='dependency')
host = register_native_host(app, image=image())
configuration = NativeRootConfiguration(configuration=profile(), working_directory=Path.cwd(),
    native_repository=NativeRepositoryConfiguration(str(Path.cwd()), (8<<20, 8<<20, 32<<20, 16<<20)))
binding = register_native_application(app, configuration=configuration)
def close_image():
    events.append('image')
    if events.count('image') == 1:
        raise failure
binding.repository_runtime._runtime_image = SimpleNamespace(close=close_image)
record = {}
async def exercise():
    await app.startup()
    try:
        await app.shutdown()
    except BaseException as caught:
        record['lifespan_caught_original'] = caught is failure
    record['host_closed'] = host._closed
    record['dependency_closed'] = 'dependency' in events
    record['status'] = app.extensions['tofu_lifecycle']['status']
    # A failed native image remains owned and explicitly retryable.
    await binding.close()
    record['image_close_attempts'] = events.count('image')
try:
    asyncio.run(exercise())
except BaseException as escaped:
    record['escaped_loop'] = type(escaped).__name__
Path(sys.argv[2]).write_text(json.dumps(record))
'''


@pytest.mark.parametrize('failure', ['interrupt', 'exit', 'cancel', 'ordinary'])
def test_repository_cleanup_failure_preserves_remaining_owners_and_retry(tmp_path, failure):
    output = tmp_path / 'result.json'
    python = os.environ.get('TOFU_BENCHMARK_PYTHON') or sys.executable
    result = subprocess.run([python, '-c', PROGRAM, failure, str(output)],
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-3000:]
    assert json.loads(output.read_text()) == {
        'lifespan_caught_original': True,
        'host_closed': True,
        'dependency_closed': True,
        'status': 'stopped',
        'image_close_attempts': 2,
    }


def test_unstarted_repository_cleanup_preserves_ownership_and_explicit_retry(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from lib.app.app_factory import create_base_app
    from lib.tasks_pkg.native_application import NativeRootConfiguration, register_native_application
    from lib.tasks_pkg.native_host import register_native_host
    from tofu_agent import NativeRepositoryConfiguration
    from tests.tasks_pkg.test_native_application import profile
    from tests.tasks_pkg.test_native_host import image

    app = create_base_app('native-close-scheduling')
    host = register_native_host(app, image=image())
    configuration = NativeRootConfiguration(configuration=profile(), working_directory=tmp_path,
        native_repository=NativeRepositoryConfiguration(str(tmp_path), (8<<20, 8<<20, 32<<20, 16<<20)))
    binding = register_native_application(app, configuration=configuration)
    closed, unstarted = [], []
    owned_image = SimpleNamespace(close=lambda: closed.append(True))
    binding.repository_runtime._runtime_image = owned_image
    failure = RuntimeError('cleanup task creation refused')

    def refuse(operation):
        unstarted.append(operation)
        raise failure

    async def exercise():
        try:
            with monkeypatch.context() as patch:
                patch.setattr(asyncio, 'create_task', refuse)
                with pytest.raises(RuntimeError) as caught:
                    await binding.close()
                assert caught.value is failure
            assert binding.closed and binding.app is None
            assert binding.repository_runtime._runtime_image is owned_image
            assert not closed
            assert len(unstarted) == 1 and unstarted[0].cr_frame is None
            await binding.close()
            assert closed == [True]
        finally:
            await binding.close()
            await host.close()

    asyncio.run(exercise())
