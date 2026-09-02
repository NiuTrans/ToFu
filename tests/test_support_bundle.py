"""Privacy, bounds, and offline contracts for ``serverctl support-bundle``."""

from __future__ import annotations

import json
import stat

import pytest

from serverctl_pkg.support_bundle import (
    MAX_TOTAL_LOG_BYTES,
    REDACTED,
    build_support_bundle,
    sanitize_text,
    write_support_bundle,
)


pytestmark = pytest.mark.unit


def _doctor(project, worker_log):
    return {
        'projectPath': str(project),
        'ready': False,
        'healthy': False,
        'managerStatus': {
            'observed': 'degraded',
            'workerLog': str(worker_log),
            'managerLog': str(project / 'logs' / 'server-manager.log'),
            'bootstrapToken': 'tofu_admin_supersecret123',
            'awsSecretAccessKey': 'opaque-structured-secret',
        },
        'findings': [{
            'code': 'worker_degraded',
            'severity': 'error',
            'message': 'request failed Authorization: Bearer secret-bearer-value',
        }],
    }


def test_bundle_is_bounded_and_redacts_structured_config_and_logs(tmp_path):
    project = tmp_path / 'tofu'
    logs = project / 'logs'
    logs.mkdir(parents=True)
    (project / 'VERSION').write_text('9.9.9\n', encoding='utf-8')
    (project / '.env').write_text(
        'PORT=16000\n'
        'LLM_MODEL=test-model\n'
        'LLM_API_KEY=sk-env-secret123456\n'
        'CUSTOM_PASSWORD=hunter2\n',
        encoding='utf-8')
    (project / '.tofu_env.json').write_text(json.dumps({
        'backend': 'uv',
        'python': str(project / '.venv' / 'bin' / 'python'),
        'env_prefix': str(project / '.venv'),
        'env_name': 'tofu',
        'ignored_secret': 'must-not-be-copied',
    }), encoding='utf-8')
    worker_log = logs / 'server-console.log'
    worker_log.write_text(
        'old line\n'
        'request token=raw-token-secret\n'
        'provider api_keys=opaque-inline-one,opaque-inline-two failed\n'
        'Authorization: Bearer bearer-secret-value\n',
        encoding='utf-8')
    (logs / 'server-manager.log').write_text(
        'url=https://user:pass@example.test/?api_key=query-secret\n',
        encoding='utf-8')
    (logs / 'incident.jsonl').write_text(
        '{"fingerprint":"fp-storage","occurrence_delta":42}\n',
        encoding='utf-8')
    installer_log = logs / 'install-20260823_120000-4321.log'
    installer_log.write_text(
        'step one\nLLM_API_KEYS=opaque-one,opaque-two\ninstall done\n',
        encoding='utf-8')
    (logs / 'cgroup_pressure.log').write_text(
        'memory pressure 91%\n', encoding='utf-8')
    (logs / 'tofu_faulthandler_123.log').write_text(
        'thread stack frame\n', encoding='utf-8')

    bundle = build_support_bundle(project, _doctor(project, worker_log), lines=2)
    encoded = json.dumps(bundle, ensure_ascii=False)

    for secret in (
            'sk-env-secret123456', 'hunter2', 'raw-token-secret',
            'bearer-secret-value', 'tofu_admin_supersecret123', 'query-secret',
            'https://user:pass@', 'must-not-be-copied', 'opaque-one',
            'opaque-two'):
        assert secret not in encoded
    assert 'opaque-structured-secret' not in encoded
    assert 'opaque-inline-one' not in encoded
    assert 'opaque-inline-two' not in encoded
    assert REDACTED in encoded
    assert bundle['schema'] == 'tofu.support-bundle/v1'
    assert bundle['privacy']['credentialsRedacted'] is True
    assert bundle['privacy']['redactionIsBestEffort'] is True
    assert bundle['privacy']['conversationStorageRead'] is False
    assert bundle['privacy']['externalNetworkRequestsMade'] is False
    assert bundle['privacy']['loopbackDiagnosticsMayRequestLocalServices'] is True
    assert bundle['privacy']['hostMetadataMayIdentifyMachineOrUser'] is True
    assert bundle['privacy']['logTailsMayContainUserContent'] is True
    assert bundle['privacy']['reviewBeforeSharing'] is True
    assert bundle['config']['environment']['operationalValues'] == {
        'LLM_MODEL': 'test-model', 'PORT': '16000'}
    assert bundle['config']['environment']['sensitiveKeysConfigured'] == [
        'CUSTOM_PASSWORD', 'LLM_API_KEY']
    assert bundle['config']['interpreterMarker']['backend'] == 'uv'
    assert bundle['logs']['worker']['tailLines'] == 2
    assert 'old line' not in bundle['logs']['worker']['content']
    assert bundle['logs']['installer']['available'] is True
    assert bundle['logs']['installer']['path'] == str(installer_log)
    assert 'install done' in bundle['logs']['installer']['content']
    assert 'memory pressure 91%' in bundle['logs']['resourcePressure']['content']
    assert 'thread stack frame' in bundle['logs']['faulthandler']['content']
    assert 'fp-storage' in bundle['logs']['incidents']['content']
    assert bundle['limits']['maxLogFiles'] == 12
    assert bundle['limits']['maxTotalLogBytes'] == MAX_TOTAL_LOG_BYTES


def test_log_collection_enforces_one_total_source_byte_budget(tmp_path):
    project = tmp_path / 'tofu'
    logs = project / 'logs'
    logs.mkdir(parents=True)
    payload = ('x' * 1023 + '\n') * 400
    for filename in ('server-console.log', 'server-manager.log', 'error.log',
                     'incident.jsonl', 'app.log', 'postgresql.log',
                     'storage-postgresql.log', 'cgroup_pressure.log',
                     'watchdog.log', 'faulthandler.log'):
        (logs / filename).write_text(payload, encoding='utf-8')

    bundle = build_support_bundle(
        project, _doctor(project, logs / 'server-console.log'), lines=1000)

    total_read = sum(
        int(item.get('sourceTailBytesRead') or 0)
        for item in bundle['logs'].values())
    assert total_read <= MAX_TOTAL_LOG_BYTES
    assert total_read > 0


def test_bundle_refuses_log_symlink_outside_known_directories(tmp_path):
    project = tmp_path / 'tofu'
    logs = project / 'logs'
    logs.mkdir(parents=True)
    outside = tmp_path / 'server-console.log'
    outside.write_text('token=must-not-be-read\n', encoding='utf-8')
    (logs / 'server-console.log').symlink_to(outside)

    bundle = build_support_bundle(
        project, _doctor(project, logs / 'server-console.log'), lines=20)

    assert bundle['logs']['worker']['available'] is False
    assert 'refused log path' in bundle['logs']['worker']['error']
    assert 'must-not-be-read' not in json.dumps(bundle)


def test_bundle_ignores_external_installer_symlink_and_keeps_valid_log(tmp_path):
    project = tmp_path / 'tofu'
    logs = project / 'logs'
    logs.mkdir(parents=True)
    worker = logs / 'server-console.log'
    worker.write_text('worker\n', encoding='utf-8')
    valid = logs / 'install-20260823_120000.log'
    valid.write_text('valid installer evidence\n', encoding='utf-8')
    outside = tmp_path / 'install-20260824_120000.log'
    outside.write_text('outside secret=must-not-be-read\n', encoding='utf-8')
    (logs / outside.name).symlink_to(outside)

    bundle = build_support_bundle(project, _doctor(project, worker), lines=20)

    assert bundle['logs']['installer']['available'] is True
    assert bundle['logs']['installer']['path'] == str(valid)
    assert 'valid installer evidence' in bundle['logs']['installer']['content']
    assert 'must-not-be-read' not in json.dumps(bundle)


def test_support_bundle_write_is_private_and_never_overwrites(tmp_path):
    target = tmp_path / 'support.json'
    written = write_support_bundle(target, {'schema': 'test'})
    assert written == target.resolve()
    assert json.loads(target.read_text(encoding='utf-8')) == {'schema': 'test'}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_support_bundle(target, {'schema': 'replacement'})
    assert json.loads(target.read_text(encoding='utf-8')) == {'schema': 'test'}


def test_bundle_can_omit_all_log_content(tmp_path):
    project = tmp_path / 'tofu'
    logs = project / 'logs'
    logs.mkdir(parents=True)
    worker = logs / 'server-console.log'
    worker.write_text('user supplied text\n', encoding='utf-8')

    bundle = build_support_bundle(
        project, _doctor(project, worker), include_logs=False)

    assert bundle['logs'] == {}
    assert bundle['privacy']['logTailsIncluded'] is False
    assert bundle['privacy']['logTailsMayContainUserContent'] is False
    assert 'user supplied text' not in json.dumps(bundle)


@pytest.mark.parametrize(
    'raw',
    [
        'Authorization=Basic abcdef123',
        'LLM_API_KEYS=sk-key-secret123',
        'LLM_API_KEYS=opaque-one,opaque-two',
        '{"client_secret":"hello-world"}',
        'https://user:password@example.test/?access_token=secret',
        'postgresql://user:database-password@db.example.test/tofu',
        'github_pat_abcdefghijklmnopqrstuvwx',
        'eyJabcdefghijkl.abcdefghijk.abcdefghijkl',
    ],
)
def test_text_sanitizer_removes_common_secret_shapes(raw):
    cleaned = sanitize_text(raw)
    assert REDACTED in cleaned
    assert raw != cleaned
