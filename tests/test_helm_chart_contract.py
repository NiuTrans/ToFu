"""Executable specification for the distributed Helm release boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.check_helm_render import validate_rendered_release


pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]
_CHART = _ROOT / 'deploy' / 'helm' / 'tofu'


def _read(relative_path: str) -> str:
    return (_CHART / relative_path).read_text(encoding='utf-8')


def test_chart_defaults_keep_horizontal_scaling_preview_closed():
    values = yaml.safe_load(_read('values.yaml'))
    assert values['roles']['api']['replicas'] == 1
    assert values['roles']['worker']['replicas'] == 1
    assert values['roles']['scheduler']['replicas'] == 1
    assert values['autoscaling']['api']['enabled'] is False
    assert values['autoscaling']['worker']['enabled'] is False
    assert values['podDisruptionBudget']['worker']['enabled'] is False


def test_chart_schema_rejects_horizontal_scaling_during_preview():
    schema = json.loads(_read('values.schema.json'))
    role = schema['definitions']['previewRole']['properties']
    autoscaling = schema['definitions']['previewAutoscaling']['properties']
    assert role['replicas'] == {
        'type': 'integer',
        'minimum': 1,
        'maximum': 1,
    }
    assert autoscaling['enabled'] == {'const': False}
    for role_name in ('api', 'worker', 'scheduler'):
        assert schema['properties']['roles']['properties'][role_name] == {
            '$ref': '#/definitions/previewRole',
        }
    for role_name in ('api', 'worker'):
        assert schema['properties']['autoscaling']['properties'][role_name] == {
            '$ref': '#/definitions/previewAutoscaling',
        }


def test_extra_env_cannot_override_chart_or_secret_authority():
    schema = json.loads(_read('values.schema.json'))
    protected = set(
        schema['definitions']['extraEnvVar']['properties']['name']['not']['enum'])
    expected = {
        'MALLOC_ARENA_MAX',
        'TOFU_AUTH_MODE',
        'TOFU_DB_BACKEND',
        'TOFU_DEPLOYMENT_MODE',
        'TOFU_DISTRIBUTED_PREVIEW_MODE',
        'TOFU_POSTGRES_DSN',
        'TOFU_POSTGRES_DSN_FILE',
        'TOFU_PROCESS_ROLE',
        'TOFU_REDIS_URL',
        'TOFU_REDIS_URL_FILE',
        'TOFU_REPLICA_ID',
        'TOFU_REPLICA_RING',
        'TOFU_REQUIRE_PG',
        'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE',
        'TOFU_STORAGE_CONNECTION_FILE',
        'TOFU_STORAGE_MODE',
        'TOFU_STORAGE_PARENT_PID',
        'TOFU_STORAGE_PROJECT_ROOT',
        'TOFU_STORAGE_TEST_BACKEND',
        'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE',
        'TOFU_STORAGE_TOKEN',
    }
    assert protected == expected

    helper = _read('templates/_helpers.tpl')
    deployment = _read('templates/deployments.yaml')
    assert 'define "tofu.validateExtraEnv"' in helper
    assert 'extraEnv contains duplicate name' in helper
    assert 'managed by the chart and cannot be overridden' in helper
    assert 'include "tofu.validateExtraEnv" $root' in deployment
    for name in expected:
        assert f'"{name}"' in helper


def _minimal_deployment(role: str, replicas: int = 1) -> dict[str, object]:
    return {
        'apiVersion': 'apps/v1',
        'kind': 'Deployment',
        'metadata': {'name': f'tofu-{role}'},
        'spec': {
            'replicas': replicas,
            'template': {
                'metadata': {
                    'labels': {'tofu.openai.com/process-role': role},
                },
                'spec': {},
            },
        },
    }


def test_render_contract_rejects_preview_scaling_in_default_release():
    deployments = [
        _minimal_deployment(role) for role in ('api', 'worker', 'scheduler')]
    default_errors = validate_rendered_release(deployments)
    assert not any('must default to one replica' in error
                   for error in default_errors)
    assert not any('HorizontalPodAutoscalers' in error
                   for error in default_errors)

    scaled_deployments = [
        _minimal_deployment('api', replicas=2),
        _minimal_deployment('worker'),
        _minimal_deployment('scheduler'),
    ]
    scaled_errors = validate_rendered_release(scaled_deployments)
    assert any('api must default to one replica' in error
               for error in scaled_errors)

    hpa_errors = validate_rendered_release([
        *deployments,
        {
            'apiVersion': 'autoscaling/v2',
            'kind': 'HorizontalPodAutoscaler',
            'metadata': {'name': 'tofu-api'},
            'spec': {},
        },
    ])
    assert any('HorizontalPodAutoscalers' in error for error in hpa_errors)


def test_render_contract_rejects_duplicate_environment_authorities():
    deployments = [
        _minimal_deployment(role) for role in ('api', 'worker', 'scheduler')]
    api_pod = deployments[0]['spec']['template']['spec']
    api_pod['containers'] = [
        {
            'name': 'api',
            'env': [
                {'name': 'TOFU_PROCESS_ROLE', 'value': 'api'},
                {'name': 'TOFU_PROCESS_ROLE', 'value': 'all'},
            ],
        },
        {'name': 'storage-sidecar'},
    ]

    errors = validate_rendered_release(deployments)
    assert any(
        'app contains duplicate environment names: TOFU_PROCESS_ROLE' in error
        for error in errors
    )


def test_render_contract_rejects_a_writable_container_root_filesystem():
    deployment = _minimal_deployment('api')
    pod_spec = deployment['spec']['template']['spec']
    pod_spec['containers'] = [{
        'name': 'api',
        'image': 'ghcr.io/rangehow/tofu-api@sha256:' + ('a' * 64),
        'securityContext': {
            'allowPrivilegeEscalation': False,
            'readOnlyRootFilesystem': False,
            'capabilities': {'drop': ['ALL']},
            'runAsNonRoot': True,
            'runAsUser': 10001,
            'runAsGroup': 10001,
            'seccompProfile': {'type': 'RuntimeDefault'},
        },
    }]

    writable_errors = validate_rendered_release([deployment])
    assert any(
        'container api security context is incomplete' in error
        for error in writable_errors
    )

    pod_spec['containers'][0]['securityContext'][
        'readOnlyRootFilesystem'] = True
    hardened_errors = validate_rendered_release([deployment])
    assert not any(
        'container api security context is incomplete' in error
        for error in hardened_errors
    )


def test_render_contract_rejects_cluster_wide_empty_namespace_selector():
    policy = {
        'apiVersion': 'networking.k8s.io/v1',
        'kind': 'NetworkPolicy',
        'metadata': {'name': 'tofu-api-ingress'},
        'spec': {
            'ingress': [{
                'from': [{
                    'namespaceSelector': {},
                    'podSelector': {},
                }],
            }],
        },
    }

    wide_errors = validate_rendered_release([policy])
    assert 'API ingress must not use an empty namespaceSelector' in wide_errors

    del policy['spec']['ingress'][0]['from'][0]['namespaceSelector']
    same_namespace_errors = validate_rendered_release([policy])
    assert (
        'API ingress must not use an empty namespaceSelector'
        not in same_namespace_errors
    )


def test_chart_requires_digest_pinned_images_and_external_secret():
    schema = json.loads(_read('values.schema.json'))
    assert schema['definitions']['image']['properties']['digest']['pattern'] == (
        '^$|^sha256:[a-f0-9]{64}$'
    )
    helpers = _read('templates/_helpers.tpl')
    assert 'required (printf "images.%s.digest is required"' in helpers
    assert 'secretName: {{ .Values.secrets.existingSecret | quote }}' in helpers
    assert 'defaultMode: 0400' in helpers
    assert 'kind: Secret' not in '\n'.join(
        path.read_text(encoding='utf-8')
        for path in (_CHART / 'templates').glob('*.yaml')
    )


def test_deployments_use_co_container_sidecar_and_fixed_role_contract():
    helpers = _read('templates/_helpers.tpl')
    deployments = _read('templates/deployments.yaml')
    combined = helpers + deployments
    assert 'TOFU_STORAGE_CONNECTION_FILE' in combined
    assert 'MALLOC_ARENA_MAX' in combined
    assert 'TOFU_DISTRIBUTED_PREVIEW_MODE' in combined
    assert 'value: read-only' in combined
    assert '/run/tofu-storage/connection.json' in combined
    assert 'medium: Memory' in combined
    assert 'name: storage-sidecar' in combined
    assert 'lib.storage.connection_probe' in combined
    readiness_command = (
        'command: ["python", "-m", "lib.storage.connection_probe"]')
    liveness_command = (
        'command: ["python", "-m", "lib.storage.connection_probe", '
        '"--liveness"]')
    assert helpers.count(readiness_command) == 2
    assert helpers.count(liveness_command) == 1
    assert 'fieldPath: metadata.uid' in combined
    assert 'automountServiceAccountToken: false' in deployments
    assert 'allowPrivilegeEscalation: false' in helpers
    assert 'readOnlyRootFilesystem: true' in helpers
    assert 'drop: ["ALL"]' in helpers
    assert 'runAsNonRoot: true' in helpers
    assert 'hostPath:' not in combined
    for removed in (
        'TOFU_DB_BACKEND', 'TOFU_REQUIRE_PG', 'TOFU_REPLICA_RING',
        'TOFU_STORAGE_MODE',
    ):
        assert f'- name: {removed}' not in combined


def test_release_contains_migration_maintenance_and_network_safety_objects():
    migration = _read('templates/migration-job.yaml')
    maintenance = _read('templates/maintenance-cronjob.yaml')
    network = _read('templates/networkpolicy.yaml')
    service = _read('templates/service.yaml')
    assert 'pre-install,pre-upgrade' in migration
    assert 'activeDeadlineSeconds:' in migration
    assert 'concurrencyPolicy: Forbid' in maintenance
    assert 'activeDeadlineSeconds:' in maintenance
    assert 'default-deny-ingress' in network
    assert 'tofu.openai.com/process-role: api' not in service
    assert '"role" "api"' in service
    assert 'sessionAffinity' not in service


def test_ci_lints_and_semantically_checks_the_rendered_chart():
    workflow = (_ROOT / '.github' / 'workflows' / 'ci.yml').read_text(
        encoding='utf-8')
    assert 'helm lint deploy/helm/tofu' in workflow
    assert 'helm template tofu deploy/helm/tofu' in workflow
    assert 'scripts/check_helm_render.py' in workflow
    assert 'Reject protected extraEnv overrides' in workflow
    assert 'Reject duplicate extraEnv names' in workflow
    assert 'Render one ordinary extraEnv setting' in workflow
    assert 'ee88b3c851ae6466a3de507f7be73fe94d54cbf2987cbaa3d1a3832ea331f2cd' in workflow
