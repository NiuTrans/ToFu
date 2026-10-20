"""Production transport, signal bridge and lifespan for native application tests.

Only launch policy is supplied by this fixture. Storage boot, model routing,
HTTP authentication, execution, settlement and shutdown keep production owners.
The caller owns the private data directory, port, process and archive pin.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sys


def main(values):
    directory = Path(values['directory']).resolve()
    assert Path(os.environ['TOFU_DATA_DIR']).resolve() == directory
    backend = values.get('storage_backend', 'tofudb')
    assert os.environ['TOFU_PERSONAL_STORAGE_BACKEND'] == ('tofudb' if backend == 'tofudb' else 'sqlite')
    from lib.hypercorn_runtime import build_hypercorn_config, serve
    from lib.server.server_shutdown import (
        http_keep_alive_timeout_seconds, request_graceful_shutdown,
        wait_for_shutdown_trigger,
    )
    from lib.tasks_pkg.native_production_profile import compile_native_application_profile, compose_native_application
    from lib.tasks_pkg.native_resources import compile_native_transport_budget
    from lib.resource_control.runtime import launch_policy
    from scripts.build_native_runtime import verify
    from server import create_production_app

    archive = Path(values['archive'])
    verify(archive, run=False)
    policy = launch_policy(os.environ)
    budget = compile_native_transport_budget(launch_policy=policy, shutdown_seconds=10)
    configuration = compile_native_application_profile(transport=budget, launch_policy=policy)
    checkpoint_factory = None
    if values.get('file_checkpoint'):
        from tests.support.native_production_checkpoint import file_checkpoint_factory
        configuration.update(checkpoint_shared_account=True, checkpoint_frame_bytes=budget.frame_bytes)
        checkpoint_factory = file_checkpoint_factory(values)
    flow_options = {}
    if values.get('goal_checkpoint'):
        from lib.goal_runs.native_recovery import NativeGoalRecoveryConfiguration
        from tests.support.native_production_goal import actor_checkpoint_factory
        if backend != 'tofudb':
            from lib.storage_authority_sql.native_checkpoint_config import NativeCheckpointConfiguration
            scope = NativeCheckpointConfiguration.from_environment()
            assert scope is not None
            values['goal_checkpoint_bounds'] = values.get('goal_checkpoint_bounds') or dict(header_bytes=scope.document_bytes,
                snapshot_bytes=scope.document_bytes, identity_bytes=scope.identity_bytes,
                evidence_bytes=scope.document_bytes)
        flow_profile = dict(configuration, checkpoint_actor_account=True,
            checkpoint_shared_account=True, checkpoint_frame_bytes=budget.frame_bytes)
        flow_options = dict(flow_configuration=flow_profile, flow_archive=True,
            flow_checkpoint_factory=actor_checkpoint_factory(values, budget),
            goal_recovery=NativeGoalRecoveryConfiguration(**(values.get('goal_checkpoint_bounds') or {})))
    native = compose_native_application(archive=archive,
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        working_directory=directory / 'project', launch_policy=policy, shutdown_seconds=10,
        configuration=configuration if values.get('file_checkpoint') else None,
        checkpoint_factory=checkpoint_factory, **flow_options)
    image = native['native_image']
    from dataclasses import asdict
    Path(values['catalog_ready']).with_suffix('.native-profile.json').write_text(json.dumps({
        'launch_policy': policy, 'transport': asdict(image.budget), 'configuration': json.loads(native['native_root']._profile),
        'image_admission_started': image.admission_started,
    }, indent=2))
    if backend == 'postgres':
        # PostgreSQL writes remain an explicit private test authority. The
        # shipped distributed preview stays read-only; no deployment override.
        from lib.storage import StorageRuntime, StorageSupervisor
        from lib.storage.service import install_runtime_for_test
        install_runtime_for_test(StorageRuntime(StorageSupervisor(project_root=directory, backend=backend)))
    app = create_production_app(**native)

    @app.before_serving
    async def observed_startup():
        # Wait for the real post-ready catalogue owner before test route CAS.
        # Its concurrent revision legitimately invalidates an issued route.
        await asyncio.wait_for(app.extensions['tofu_production_startup_gate'].wait(), timeout=90)
        pending = [task for task in asyncio.all_tasks()
                   if task.get_name() == 'tofu-post-ready-catalog-sync']
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=30)
        from lib.storage.service import get_storage_client
        storage = get_storage_client()
        storage_health = storage.health()
        Path(values['catalog_ready']).with_suffix('.storage.json').write_text(json.dumps({
            'transport': 'embedded' if storage.endpoint[0] == 'embedded' else 'daemon',
            'durability': storage_health.get('walDurability'),
            'backend': storage_health.get('backend'), 'protocol': storage_health.get('protocol'),
        }))
        Path(values['catalog_ready']).write_text('ready')

    @app.after_serving
    async def observed_shutdown():
        host = app.extensions['tofu_native_host']
        Path(values['closed']).write_text(json.dumps({
            'nativeClients': len(host._clients), 'imageClosed': image._closed,
        }))

    config = build_hypercorn_config('127.0.0.1', values['port'],
        keep_alive_timeout=http_keep_alive_timeout_seconds())
    config.accesslog = None
    config.errorlog = '-'

    async def run():
        # Use the same signal-to-push/drain bridge as server.py. A private
        # Hypercorn stop event would leave the production watchdog unaware.
        stop = app.extensions['tofu_shutdown_requested']
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, request_graceful_shutdown, stop)

        async def shutdown():
            await wait_for_shutdown_trigger(stop)

        await serve(app, config, shutdown_trigger=shutdown)

    asyncio.run(run())


if __name__ == '__main__':
    main(json.loads(Path(sys.argv[1]).read_text()))
