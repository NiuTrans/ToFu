#!/usr/bin/env python3
"""Validate security and topology invariants in a rendered Tofu Helm release.

Entry point: ``python scripts/check_helm_render.py <helm-template.yaml>``.
Dependency: PyYAML from the repository's locked test toolchain.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path
import re
import sys
from typing import Any

import yaml


_DIGEST_IMAGE = re.compile(r'^[^@\s]+@sha256:[a-f0-9]{64}$')
_PROCESS_ROLES = frozenset({'api', 'worker', 'scheduler'})
_FORBIDDEN_ENVIRONMENT = frozenset({
    'TOFU_DB_BACKEND',
    'TOFU_POSTGRES_DSN',
    'TOFU_REDIS_URL',
    'TOFU_REPLICA_RING',
    'TOFU_REQUIRE_PG',
    'TOFU_STORAGE_MODE',
    'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE',
    'TOFU_STORAGE_PARENT_PID',
    'TOFU_STORAGE_PROJECT_ROOT',
    'TOFU_STORAGE_TEST_BACKEND',
    'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE',
    'TOFU_STORAGE_TOKEN',
})
_FIXED_ENVIRONMENT_VALUES = {
    'MALLOC_ARENA_MAX': '8',
    'TOFU_DEPLOYMENT_MODE': 'distributed',
    'TOFU_DISTRIBUTED_PREVIEW_MODE': 'read-only',
    'TOFU_POSTGRES_DSN_FILE': '/run/secrets/tofu/postgres-dsn',
    'TOFU_REDIS_URL_FILE': '/run/secrets/tofu/redis-url',
    'TOFU_STORAGE_CONNECTION_FILE': '/run/tofu-storage/connection.json',
}


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _environment(
    container: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], frozenset[str]]:
    """Return the environment plus names masked by a later duplicate.

    Kubernetes accepts duplicate ``env`` entries and container runtimes use the
    later value.  A plain dict is therefore unsafe for topology validation: it
    would silently validate the attacker's overriding value while forgetting
    that the chart rendered two authorities for one setting.
    """
    result: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for item in _list(container.get('env')):
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            continue
        if item['name'] in result:
            duplicates.add(item['name'])
        result[item['name']] = item
    return result, frozenset(duplicates)


def _container_security_is_rootless(container: dict[str, Any]) -> bool:
    security = _mapping(container.get('securityContext'))
    capabilities = _mapping(security.get('capabilities'))
    return (
        security.get('allowPrivilegeEscalation') is False
        and security.get('readOnlyRootFilesystem') is True
        and security.get('runAsNonRoot') is True
        and security.get('runAsUser') == 10001
        and security.get('runAsGroup') == 10001
        and capabilities.get('drop') == ['ALL']
        and _mapping(security.get('seccompProfile')).get('type')
        == 'RuntimeDefault'
        and security.get('privileged') is not True
    )


def _pod_specs(document: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    kind = document.get('kind')
    metadata = _mapping(document.get('metadata'))
    identity = f'{kind}/{metadata.get("name", "<unnamed>")}'
    spec = _mapping(document.get('spec'))
    if kind == 'Deployment':
        yield identity, _mapping(_mapping(spec.get('template')).get('spec'))
    elif kind == 'Job':
        yield identity, _mapping(_mapping(spec.get('template')).get('spec'))
    elif kind == 'CronJob':
        job = _mapping(_mapping(spec.get('jobTemplate')).get('spec'))
        yield identity, _mapping(_mapping(job.get('template')).get('spec'))


def _load_documents(path: Path) -> list[dict[str, Any]]:
    try:
        parsed = list(yaml.safe_load_all(path.read_text(encoding='utf-8')))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeError(f'cannot parse rendered manifest: {exc}') from exc
    documents = [item for item in parsed if isinstance(item, dict)]
    if len(documents) != len([item for item in parsed if item is not None]):
        raise RuntimeError('rendered manifest contains a non-object document')
    return documents


def validate_rendered_release(documents: list[dict[str, Any]]) -> list[str]:
    """Return every violated default distributed-release invariant."""
    errors: list[str] = []
    identities: set[tuple[str, str]] = set()
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        kind = document.get('kind')
        metadata = _mapping(document.get('metadata'))
        name = metadata.get('name')
        _require(isinstance(kind, str) and bool(kind), 'object missing kind', errors)
        _require(isinstance(name, str) and bool(name), f'{kind} missing name', errors)
        if isinstance(kind, str) and isinstance(name, str):
            identity = (kind, name)
            _require(identity not in identities, f'duplicate {kind}/{name}', errors)
            identities.add(identity)
            by_kind.setdefault(kind, []).append(document)

    _require(not by_kind.get('Secret'), 'chart must not render credential Secrets', errors)
    deployments: dict[str, dict[str, Any]] = {}
    for deployment in by_kind.get('Deployment', []):
        template = _mapping(_mapping(deployment.get('spec')).get('template'))
        labels = _mapping(_mapping(template.get('metadata')).get('labels'))
        role = labels.get('tofu.openai.com/process-role')
        _require(role in _PROCESS_ROLES, 'Deployment has invalid process role', errors)
        if isinstance(role, str):
            _require(role not in deployments, f'duplicate {role} Deployment', errors)
            deployments[role] = deployment
    _require(
        set(deployments) == _PROCESS_ROLES,
        'default release must render api, worker, and scheduler Deployments',
        errors,
    )

    for role, deployment in deployments.items():
        identity = f'Deployment/{_mapping(deployment.get("metadata")).get("name")}'
        deployment_spec = _mapping(deployment.get('spec'))
        _require(
            deployment_spec.get('replicas') == 1,
            f'{identity} must default to one replica until durable execution '
            'cutover',
            errors,
        )
        template = _mapping(deployment_spec.get('template'))
        pod_spec = _mapping(template.get('spec'))
        selector = _mapping(_mapping(deployment_spec.get('selector')).get('matchLabels'))
        labels = _mapping(_mapping(template.get('metadata')).get('labels'))
        _require(
            all(labels.get(key) == value for key, value in selector.items()),
            f'{identity} selector does not match its Pod labels',
            errors,
        )
        containers = {
            item.get('name'): item for item in _list(pod_spec.get('containers'))
            if isinstance(item, dict) and isinstance(item.get('name'), str)
        }
        _require(
            set(containers) == {role, 'storage-sidecar'},
            f'{identity} must contain exactly app and storage-sidecar containers',
            errors,
        )
        app = containers.get(role, {})
        sidecar = containers.get('storage-sidecar', {})
        app_environment, app_duplicates = _environment(app)
        sidecar_environment, sidecar_duplicates = _environment(sidecar)
        for label, environment, duplicates in (
            ('app', app_environment, app_duplicates),
            ('sidecar', sidecar_environment, sidecar_duplicates),
        ):
            _require(
                not duplicates,
                f'{identity} {label} contains duplicate environment names: '
                + ', '.join(sorted(duplicates)),
                errors,
            )
            _require(
                _mapping(environment.get('TOFU_DEPLOYMENT_MODE')).get('value')
                == 'distributed',
                f'{identity} {label} must select distributed mode',
                errors,
            )
            _require(
                _mapping(environment.get('TOFU_PROCESS_ROLE')).get('value') == role,
                f'{identity} {label} process role mismatch',
                errors,
            )
            replica_source = _mapping(
                _mapping(environment.get('TOFU_REPLICA_ID')).get('valueFrom'))
            _require(
                _mapping(replica_source.get('fieldRef')).get('fieldPath')
                == 'metadata.uid',
                f'{identity} {label} replica ID must use the immutable Pod UID',
                errors,
            )
            _require(
                not (_FORBIDDEN_ENVIRONMENT & set(environment)),
                f'{identity} {label} exposes a removed, plaintext-secret, or '
                'test-authority configuration key',
                errors,
            )
            for name, expected_value in _FIXED_ENVIRONMENT_VALUES.items():
                _require(
                    _mapping(environment.get(name)).get('value') == expected_value,
                    f'{identity} {label} must keep chart-owned {name}',
                    errors,
                )
        expected_probe_paths = {
            'startupProbe': '/health/startup',
            'readinessProbe': '/health/ready',
            'livenessProbe': '/health/live',
        }
        for probe_name, path in expected_probe_paths.items():
            probe = _mapping(app.get(probe_name))
            _require(
                _mapping(probe.get('httpGet')).get('path') == path,
                f'{identity} {probe_name} must use {path}',
                errors,
            )
        expected_sidecar_probe_commands = {
            'startupProbe': ['python', '-m', 'lib.storage.connection_probe'],
            'readinessProbe': ['python', '-m', 'lib.storage.connection_probe'],
            'livenessProbe': [
                'python', '-m', 'lib.storage.connection_probe', '--liveness'],
        }
        for probe_name, expected_command in expected_sidecar_probe_commands.items():
            command = _list(
                _mapping(_mapping(sidecar.get(probe_name)).get('exec')).get('command'))
            _require(
                command == expected_command,
                f'{identity} sidecar {probe_name} has the wrong dependency boundary',
                errors,
            )
        volumes = {
            item.get('name'): item for item in _list(pod_spec.get('volumes'))
            if isinstance(item, dict) and isinstance(item.get('name'), str)
        }
        connection_volume = _mapping(volumes.get('storage-connection'))
        _require(
            _mapping(connection_volume.get('emptyDir')).get('medium') == 'Memory',
            f'{identity} storage handoff must use a memory emptyDir',
            errors,
        )
        secret = _mapping(_mapping(volumes.get('external-services')).get('secret'))
        _require(
            secret.get('defaultMode') == 0o400,
            f'{identity} external secret files must be mode 0400',
            errors,
        )

    for document in documents:
        for identity, pod_spec in _pod_specs(document):
            pod_security = _mapping(pod_spec.get('securityContext'))
            _require(
                pod_spec.get('automountServiceAccountToken') is False,
                f'{identity} must disable service-account token mounting',
                errors,
            )
            _require(
                pod_spec.get('hostNetwork') is not True,
                f'{identity} must not use the host network',
                errors,
            )
            _require(
                pod_security.get('runAsNonRoot') is True
                and pod_security.get('runAsUser') == 10001
                and pod_security.get('runAsGroup') == 10001,
                f'{identity} Pod security context is not rootless',
                errors,
            )
            for volume in _list(pod_spec.get('volumes')):
                if isinstance(volume, dict):
                    _require(
                        'hostPath' not in volume,
                        f'{identity} must not use hostPath storage',
                        errors,
                    )
            for container in _list(pod_spec.get('containers')):
                if not isinstance(container, dict):
                    continue
                name = container.get('name', '<unnamed>')
                _require(
                    bool(_DIGEST_IMAGE.fullmatch(str(container.get('image', '')))),
                    f'{identity} container {name} image is not digest-pinned',
                    errors,
                )
                _require(
                    _container_security_is_rootless(container),
                    f'{identity} container {name} security context is incomplete',
                    errors,
                )

    services = by_kind.get('Service', [])
    _require(len(services) == 1, 'default release must render one API Service', errors)
    if len(services) == 1:
        service_spec = _mapping(services[0].get('spec'))
        selector = _mapping(service_spec.get('selector'))
        _require(
            selector.get('tofu.openai.com/process-role') == 'api',
            'Service must select only API Pods',
            errors,
        )
        _require(
            service_spec.get('sessionAffinity') in {None, 'None'},
            'Service must not require sticky sessions',
            errors,
        )

    jobs = by_kind.get('Job', [])
    _require(len(jobs) == 1, 'default release must render one migration Job', errors)
    if len(jobs) == 1:
        metadata = _mapping(jobs[0].get('metadata'))
        annotations = _mapping(metadata.get('annotations'))
        _require(
            annotations.get('helm.sh/hook') == 'pre-install,pre-upgrade',
            'migration Job must run before install and upgrade',
            errors,
        )
        _require(
            isinstance(_mapping(jobs[0].get('spec')).get('activeDeadlineSeconds'), int),
            'migration Job requires a hard deadline',
            errors,
        )

    cronjobs = by_kind.get('CronJob', [])
    _require(len(cronjobs) == 1, 'default release must render one maintenance CronJob', errors)
    if len(cronjobs) == 1:
        cron_spec = _mapping(cronjobs[0].get('spec'))
        job_spec = _mapping(_mapping(cron_spec.get('jobTemplate')).get('spec'))
        _require(
            cron_spec.get('concurrencyPolicy') == 'Forbid',
            'maintenance CronJob must forbid overlap',
            errors,
        )
        _require(
            isinstance(job_spec.get('activeDeadlineSeconds'), int),
            'maintenance CronJob requires a hard deadline',
            errors,
        )

    policies = by_kind.get('NetworkPolicy', [])
    _require(len(policies) >= 2, 'release requires default-deny and API ingress policies', errors)
    api_ingress_policies = [
        policy for policy in policies
        if str(_mapping(policy.get('metadata')).get('name', '')).endswith(
            '-api-ingress')
    ]
    _require(
        len(api_ingress_policies) == 1,
        'release requires exactly one API ingress policy',
        errors,
    )
    if len(api_ingress_policies) == 1:
        ingress_rules = _list(
            _mapping(api_ingress_policies[0].get('spec')).get('ingress'))
        peers = [
            peer
            for rule in ingress_rules
            for peer in _list(_mapping(rule).get('from'))
            if isinstance(peer, dict)
        ]
        _require(
            len(peers) == 1,
            'API ingress policy requires exactly one bounded peer',
            errors,
        )
        if len(peers) == 1:
            peer = peers[0]
            _require(
                isinstance(peer.get('podSelector'), dict),
                'API ingress peer requires a podSelector',
                errors,
            )
            if 'namespaceSelector' in peer:
                _require(
                    bool(_mapping(peer.get('namespaceSelector'))),
                    'API ingress must not use an empty namespaceSelector',
                    errors,
                )
    hpas = by_kind.get('HorizontalPodAutoscaler', [])
    _require(
        not hpas,
        'default release must not render HorizontalPodAutoscalers until '
        'durable execution cutover',
        errors,
    )
    return [error for error in errors if error]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    arguments = parser.parse_args(argv)
    try:
        errors = validate_rendered_release(_load_documents(arguments.manifest))
    except RuntimeError as exc:
        print(f'Helm render contract failed: {exc}', file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(f'Helm render contract failed: {error}', file=sys.stderr)
        return 1
    print('Helm render contract passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
