"""Compile trusted native application defaults through existing policy owners.

compile_native_application_profile consumes the host Budget and issued launch
policy. compose_native_application pairs an explicitly pinned archive with the
existing frozen application owner. Both are lazy: no I/O, executor, store, probe,
task or model loop. Route preparation selects model encoding/output fields,
principals, credentials and operation IDs; the existing app lifespan owns close.
"""
from lib.agent_core.native_content_contract import MODEL_COUNTER_SCHEMA, TERMINAL_PERSISTENCE_SCHEMA
from lib.tasks_pkg.native_http_policy import compile_native_http_policy
from lib.tasks_pkg.native_resources import compile_native_resources


def compile_native_application_profile(*, transport, launch_policy, http_overrides=None):
    """Return detached production defaults for explicit application composition.

    Byte bounds follow issued host capacity. Stack size follows the supported
    JSON depth, not task concurrency; the scoped worker exits after each count.
    One persistence retry is allowed only after the host explicitly requests it;
    neither a timeout nor ambiguous delivery grants permission to repeat work.
    Callers may apply trusted overrides before NativeRootConfiguration freezes
    them. Request data never selects these policies.
    """
    profile = compile_native_resources({}, transport=transport, launch_policy=launch_policy)
    counter_rules = MODEL_COUNTER_SCHEMA['properties']
    profile.update(
        policy=compile_native_http_policy(http_overrides),
        model_control={'ack_ms': None, 'pricing': True},
        model_counter={
            'input_bytes': profile['request_bytes'],
            'text_bytes': profile['request_bytes'],
            'tool_json_bytes': profile['request_bytes'],
            'depth': counter_rules['depth']['default'],
            'worker_stack_bytes': counter_rules['worker_stack_bytes']['default'],
        },
        terminal_persistence={
            'ack_ms': None,
            'attempts': TERMINAL_PERSISTENCE_SCHEMA['properties']['attempts']['default'],
            'frame_bytes': min(launch_policy['frame_maximum'], transport.frame_bytes),
        },
    )
    return profile


def compose_native_application(*, archive, sha256, working_directory, launch_policy,
                               shutdown_seconds, configuration=None, **root_options):
    """Return existing create_production_app arguments for a trusted release.

    The caller supplies an authenticated release pin and already-issued launch
    policy. NativeArchiveImage retains lazy hash/manifest/capability admission;
    this helper never reads an unpinned installed executable or chooses storage.
    Explicit profiles and ROOT/Flow/checkpoint options retain their own validators.
    """
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.tasks_pkg.native_application import NativeRootConfiguration
    from lib.tasks_pkg.native_resources import compile_native_transport_budget
    from lib.version import __version__

    budget = compile_native_transport_budget(launch_policy=launch_policy, shutdown_seconds=shutdown_seconds)
    profile = (compile_native_application_profile(transport=budget, launch_policy=launch_policy)
               if configuration is None else configuration)
    root = NativeRootConfiguration(configuration=profile, working_directory=working_directory, **root_options)
    image = NativeArchiveImage(archive=archive, sha256=sha256, application_version=__version__, budget=budget)
    return {'native_image': image, 'native_root': root}
