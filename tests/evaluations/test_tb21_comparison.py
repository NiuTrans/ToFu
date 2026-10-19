"""Paired evidence must not turn missing, changed or selected trials into wins."""
import json
from pathlib import Path

import pytest

from evaluations.swebench import comparison as subject
from evaluations.swebench.artifacts import atomic_write_json, create_run_dir
from evaluations.swebench.constants import BENCHMARKS, terminal_bench_21_task_digests
from evaluations.paths import storage_record

pytestmark = pytest.mark.unit


def _prepared(host_tmp_path, role, *, attempts=1, full=False):
    root = create_run_dir(host_tmp_path / 'runs', role)
    definition = BENCHMARKS['terminal-bench-2.1']
    tasks = list(terminal_bench_21_task_digests())
    if not full:
        tasks = tasks[:2]
    agent = subject.OPENCODE_AGENT if role == 'baseline' else subject.TOFU_KIMI_AGENT
    config = {
        'job_name': role, 'n_attempts': attempts, 'n_concurrent_trials': 1,
        'timeout_multiplier': 1.0, 'retry': {'max_retries': 0},
        'agents': [{'name': agent, 'n_concurrent': 1,
                    'kwargs': {'timeout_sec': 3600}, 'model_name': 'glm-5.3-m17'}],
        'datasets': [{'name': 'terminal-bench/terminal-bench-2-1',
                      'ref': definition.dataset_source_revision,
                      **({} if full else {'task_names': tasks})}],
    }
    manifest = {
        'kind': 'harbor-agent-evaluation', 'run_id': role, 'status': 'prepared',
        'runtime_storage': storage_record(root),
        'agent': agent, 'benchmark': definition.key, 'dataset': definition.dataset,
        'dataset_source_revision': definition.dataset_source_revision,
        'benchmark_source_commit': definition.source_commit,
        'models': ['glm-5.3-m17'], 'provider_face': 'fixture', 'provider_slot_id': 'm17',
        'backend': 'rootless-qemu', 'sandbox_identity': {'digest': 'pinned'},
        'harbor_source_commit': 'a' * 40, 'harbor_binary_sha256': 'b' * 64,
        'project_revision': 'c' * 40, 'project_dirty': False,
        'attempts_per_task': attempts, 'expected_trials': len(tasks) * attempts,
        'expected_tasks': len(tasks), 'job_config': str(root / 'job-config.json'),
        **({'opencode_runtime': {'contextTokens': 200000, 'outputTokens': 32768}}
           if role == 'baseline' else {'tofu_kimi_runtime': {
               'contextWindowTokens': 200000, 'runtimeConfig': {'maxTokens': 32768}}}),
    }
    atomic_write_json(root / 'manifest.json', manifest)
    atomic_write_json(root / 'job-config.json', config)
    return root


def _complete(root, reward=1, *, skip=(), mutate=None):
    _, manifest, config, job = subject._load_run(root)
    lock = terminal_bench_21_task_digests()
    for name in subject._tasks(config):
        if name in skip:
            continue
        for trial in range(config['n_attempts']):
            path = job / f'{name.split("/")[-1]}__{trial}'
            path.mkdir(parents=True)
            result = {
                'task_name': name, 'task_id': {'ref': lock[name]},
                'config': {'task': {'ref': lock[name]}, 'agent': {'model_name': 'glm-5.3-m17'}},
                'verifier_result': {'rewards': {'reward': reward}},
                'verifier': {'started_at': '2026-09-01T00:00:00',
                             'finished_at': '2026-09-01T00:00:01'},
            }
            if mutate:
                mutate(result)
            atomic_write_json(path / 'result.json', result)
    manifest['status'] = 'succeeded'
    atomic_write_json(root / 'manifest.json', manifest)


def test_pair_rejects_different_frozen_runtime_resources(host_tmp_path):
    b, c = [_prepared(host_tmp_path, r) for r in ('baseline', 'candidate')]
    manifest = json.loads((c / 'manifest.json').read_text())
    manifest['runtime_resource_environment'] = {'TOFU_RESOURCE_PROFILE': 'throughput'}
    atomic_write_json(c / 'manifest.json', manifest)
    issues = subject._compatibility(subject._load_run(b), subject._load_run(c))
    assert 'shared_config:runtime_resource_environment' in issues


def _report(host_tmp_path, baseline, candidate, monkeypatch, **kwargs):
    monkeypatch.setattr(subject, 'audit_run', lambda *a, **k: {'checks': []})
    return subject.compare_runs(baseline, candidate, output_root=host_tmp_path / 'reports',
                                run_id='report', **kwargs)[0]


def test_historical_incomplete_pair_never_becomes_final(host_tmp_path, monkeypatch):
    b, c = [_prepared(host_tmp_path, r) for r in ('baseline', 'candidate')]
    _complete(b, 0)
    _complete(c, 1, skip=list(terminal_bench_21_task_digests())[1:2])
    report = _report(host_tmp_path, b, c, monkeypatch)
    assert 'diagnosticPaired' not in report
    assert report['fullDomain']['taskCount'] == 89
    assert report['fullDomain']['currentDeltaPercentagePoints'] == pytest.approx(100 / 89)
    assert report['fullDomain']['taskClusterBootstrap95CI'] is None
    full = report['arms']['candidate']['fullDomain']
    assert full['requiredTrials'] == 89
    assert full['passed'] == 1 and full['missing'] == 88
    assert full['successRatePercent'] == pytest.approx(100 / 89)
    assert len(full['tasks']) == 89
    summary = subject.render_report(report)
    assert '共同有效题' not in summary and '诊断子集' not in summary
    assert '1.12%' in summary
    assert report['decision'] == 'diagnostic_only'
    assert report['finalDeltaPercentagePoints'] is None
    assert report['arms']['candidate']['completeTasks'] == 1
    assert 'pair_not_preregistered' in report['blockers']
    assert report['arms']['candidate']['evidenceIndexSha256']


@pytest.mark.parametrize('reward', [True, False, float('nan'), float('inf'), '1', None, 0.5])
def test_only_finite_binary_numeric_rewards_count(host_tmp_path, reward):
    b = _prepared(host_tmp_path, 'baseline')
    _complete(b, reward)
    obs = subject._observe(subject._load_run(b))
    assert obs['completeTasks'] == 0
    assert obs['numericRewards'] == 0


@pytest.mark.parametrize('error,scored', [('AgentTimeoutError', True), ('RuntimeError', False)])
def test_numeric_timeout_is_scored_other_execution_errors_are_unresolved(host_tmp_path, error, scored):
    b = _prepared(host_tmp_path, 'baseline')
    _complete(b, 0, mutate=lambda r: r.update(exception_info={'exception_type': error}))
    obs = subject._observe(subject._load_run(b))
    assert (obs['completeTasks'] == 2) is scored


def test_registration_freezes_config_and_cannot_adopt_history(host_tmp_path):
    b, c = [_prepared(host_tmp_path, r) for r in ('baseline', 'candidate')]
    path = subject.register_comparison(b, c, output_root=host_tmp_path / 'protocols', run_id='pair')
    _, m, cfg, _ = subject._load_run(c)
    assert subject.validate_registration(c, m, cfg)['targetPercentagePoints'] == 10
    assert path.stat().st_mode & 0o077 == 0
    cfg['agents'][0]['kwargs']['timeout_sec'] = 7200
    with pytest.raises(ValueError, match='identity drifted'):
        subject.validate_registration(c, m, cfg)
    with pytest.raises(ValueError, match='already belongs'):
        subject.register_comparison(b, c, output_root=host_tmp_path / 'protocols', run_id='another')
    doc = json.loads(path.read_text())
    doc['targetPercentagePoints'] = 1
    atomic_write_json(path, doc)
    with pytest.raises(ValueError, match='protocol digest drifted'):
        subject.validate_registration(c, m, cfg)


def test_post_outcome_registration_is_rejected(host_tmp_path):
    b, c = [_prepared(host_tmp_path, r) for r in ('baseline', 'candidate')]
    _complete(b)
    with pytest.raises(ValueError, match='unstarted'):
        subject.register_comparison(b, c, output_root=host_tmp_path / 'protocols', run_id='pair')


def test_changed_budget_and_destructive_retries_are_rejected(host_tmp_path):
    b, c = [_prepared(host_tmp_path, r) for r in ('baseline', 'candidate')]
    cfg = json.loads((c / 'job-config.json').read_text())
    cfg['retry']['max_retries'] = 2
    atomic_write_json(c / 'job-config.json', cfg)
    with pytest.raises(ValueError, match='destructive_internal_retry'):
        subject.register_comparison(b, c, output_root=host_tmp_path / 'protocols', run_id='pair')


def test_unequal_outer_harbor_deadline_cannot_hide_behind_equal_agent_budget(host_tmp_path):
    b, c = [_prepared(host_tmp_path, r) for r in ('baseline', 'candidate')]
    cfg = json.loads((c / 'job-config.json').read_text())
    cfg['agents'][0]['override_timeout_sec'] = 21600
    atomic_write_json(c / 'job-config.json', cfg)
    with pytest.raises(ValueError, match='harbor_agent_timeout_override_sec'):
        subject.register_comparison(b, c, output_root=host_tmp_path / 'protocols', run_id='pair')


def test_duplicate_successes_are_not_best_of_selection(host_tmp_path, monkeypatch):
    b, c = [_prepared(host_tmp_path, r) for r in ('baseline', 'candidate')]
    _complete(b, 0)
    _complete(c, 1)
    _, _, _, job = subject._load_run(c)
    result = next(job.glob('*/result.json'))
    atomic_write_json(job / 'duplicate' / 'result.json', json.loads(result.read_text()))
    report = _report(host_tmp_path, b, c, monkeypatch)
    assert report['arms']['candidate']['completeTasks'] == 1
    assert report['arms']['candidate']['surplus']
    full = report['arms']['candidate']['fullDomain']
    assert full['passed'] == 1 and full['unscored'] == 1
    assert full['missing'] == 87
    assert 'candidate:surplus_trials' in report['blockers']


def test_full_pair_requires_audits_and_uses_task_means(host_tmp_path, monkeypatch):
    b, c = [_prepared(host_tmp_path, r, attempts=5, full=True) for r in ('baseline', 'candidate')]
    subject.register_comparison(b, c, output_root=host_tmp_path / 'protocols', run_id='pair')
    _complete(b, 0)
    _complete(c, 1)
    report = _report(host_tmp_path, b, c, monkeypatch)
    assert report['decision'] == 'target_confirmed'
    assert report['fullDomain']['taskCount'] == 89
    assert report['fullDomain']['taskClusterBootstrap95CI'] == [100, 100]
    assert report['finalDeltaPercentagePoints'] == 100
    assert report['officialLeaderboardResult'] is False
    monkeypatch.setattr(subject, 'audit_run', lambda *a, **k: {'checks': [{'name': 'route', 'ok': False}]})
    report, _ = subject.compare_runs(b, c, output_root=host_tmp_path / 'reports', run_id='audit-fail')
    assert report['decision'] == 'diagnostic_only'
    assert 'candidate:audit_failed' in report['blockers']


def test_artifact_budget_and_symlink_fail_closed(host_tmp_path, monkeypatch):
    root = host_tmp_path / 'evidence'
    root.mkdir()
    p = root / 'large.json'
    p.write_bytes(b'12345')
    monkeypatch.setattr(subject, 'MAX_TRACE_BYTES', 4)
    with pytest.raises(ValueError, match='per-file bound'):
        subject.EvidenceIndex(root).add(p)
    p.unlink()
    p.symlink_to(host_tmp_path / 'outside')
    with pytest.raises(ValueError, match='symlink'):
        subject.EvidenceIndex(root).add(p)


def test_paired_bootstrap_is_seeded_and_clusters_by_task():
    a = subject._bootstrap([-1, 0, 1], 123)
    assert a == subject._bootstrap([-1, 0, 1], 123)
    assert a[0] < 0 < a[1]
    assert subject._bootstrap([1], 123) is None


def test_full_domain_keeps_partial_repeats_and_infrastructure_failures():
    tasks = sorted(terminal_bench_21_task_digests())
    by_task = {tasks[0]: [
        {'eligible': True, 'reward': 1, 'resultPath': 'pass.json'},
        {'eligible': True, 'reward': 0, 'resultPath': 'fail.json'},
        {'eligible': False, 'reward': None, 'resultPath': 'infra.json'},
    ]}
    full = subject._full_domain(by_task, tasks, 5)
    assert full['requiredTrials'] == 445
    assert full['passed'] == full['scoredFailed'] == full['unscored'] == 1
    assert full['missing'] == 442
    assert full['successRatePercent'] == pytest.approx(100 / 445)
    assert full['scoredCoveragePercent'] == pytest.approx(200 / 445)
    assert not full['allTrialsScored']
    assert sum(full[k] for k in ('passed', 'scoredFailed', 'unscored', 'missing')) == 445


def test_verify_report_detects_original_artifact_changes(host_tmp_path, monkeypatch):
    b, c = [_prepared(host_tmp_path, r) for r in ('baseline', 'candidate')]
    _complete(b)
    _complete(c)
    _report(host_tmp_path, b, c, monkeypatch)
    path = host_tmp_path / 'reports/report/report.json'
    assert subject.verify_report(path)['ok']
    cfg = b / 'job-config.json'
    cfg.write_text(cfg.read_text() + ' ')
    verified = subject.verify_report(path)
    assert not verified['ok']
    assert {'arm': 'baseline', 'path': 'job-config.json', 'cause': 'changed'} in verified['failures']


def test_trace_does_not_blame_model_after_tool_call_finish(host_tmp_path):
    b = _prepared(host_tmp_path, 'baseline')
    _complete(b, 0)
    _, _, _, job = subject._load_run(b)
    trial = next(job.iterdir())
    path = trial / 'agent/tofu-kimi-evidence/events.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'event': {'type': 'model_request_complete',
        'finishReason': 'tool_calls', 'model': 'glm-5.3-m17', 'durationMs': 1832}}) + '\n'
        + json.dumps({'observedAtUnixNs': 535_000_000_000, 'event': {'type': 'round_usage',
        'usage': {'_dispatch': {'stream_completed_at_unix_ns': 1_000_000_000}}}}) + '\n')
    observed = subject._observe(subject._load_run(b))
    row = next(row for row in observed['trials'] if row['trial'] == trial.name)
    assert row['trace']['toolCallWithoutObservedClientHandoff']
    assert row['trace']['maxObservedDeliveryLagMs'] == 534_000
    assert row['trace']['modelRoundsObservedOver120SecondsLate'] == 1
    assert row['trace']['completedModelRequestMs'] == 1832
    assert row['diagnosis'] == 'verifier_failed'


def test_prepared_registered_run_uses_existing_launcher_and_freeze_gate(host_tmp_path, monkeypatch):
    from evaluations.swebench import harbor_runner as runner
    from evaluations.swebench import preflight, audit
    b, c = [_prepared(host_tmp_path, r) for r in ('baseline', 'candidate')]
    for root in (b, c):
        _, manifest, cfg, _ = subject._load_run(root)
        manifest['harbor_binary'] = 'harbor-fixture'
        cfg['environment'] = {'import_path': 'rootless_vm.harbor_environment:RootlessQemuEnvironment', 'kwargs': {}}
        atomic_write_json(root / 'manifest.json', manifest)
        atomic_write_json(root / 'job-config.json', cfg)
    subject.register_comparison(b, c, output_root=host_tmp_path / 'protocols', run_id='pair')
    from types import SimpleNamespace
    monkeypatch.setattr(runner.RootlessQemuSettings, 'from_environment_kwargs',
                        lambda *a: SimpleNamespace(preflight_storage=lambda *a: {}))
    for name in ('_validated_resume_codex_runtime', '_validated_resume_tofu_runtime', '_validated_resume_opencode_runtime'):
        monkeypatch.setattr(runner, name, lambda **kw: None)
    monkeypatch.setattr(preflight, 'harbor_checks', lambda **kw: [])
    monkeypatch.setattr(runner, 'resolve_executable', lambda name: 'harbor-fixture')
    monkeypatch.setattr(runner, '_executable_identity', lambda name: ('harbor-fixture', 'b' * 64))
    monkeypatch.setattr(runner, 'prepare_runtime_environment', lambda *a: {})
    commands = []
    monkeypatch.setattr(runner, 'run_streaming', lambda command, **kw: commands.append(command) or 0)
    monkeypatch.setattr(audit, 'audit_run', lambda *a, **kw: {'ok': True, 'checks': []})
    assert runner.resume_harbor_run(b) == 0
    assert commands == [['harbor-fixture', 'run', '--config', str(b / 'job-config.json'), '--yes']]
    assert json.loads((b / 'manifest.json').read_text())['status'] == 'succeeded'
    with pytest.raises(ValueError, match='already terminal'):
        runner.resume_harbor_run(b)
    _, _, cfg, _ = subject._load_run(c)
    cfg['agents'][0]['kwargs']['timeout_sec'] = 4000
    atomic_write_json(c / 'job-config.json', cfg)
    with pytest.raises(ValueError, match='identity drifted'):
        runner.resume_harbor_run(c)
    assert len(commands) == 1


@pytest.mark.parametrize('value', [None, True, 65536])
def test_unknown_or_unequal_output_budget_blocks_registration(host_tmp_path, value):
    b, c = [_prepared(host_tmp_path, r) for r in ('baseline', 'candidate')]
    m = json.loads((c / 'manifest.json').read_text())
    m['tofu_kimi_runtime']['runtimeConfig']['maxTokens'] = value
    atomic_write_json(c / 'manifest.json', m)
    with pytest.raises(ValueError, match='max_output_tokens'):
        subject.register_comparison(b, c, output_root=host_tmp_path / 'protocols', run_id='pair')


@pytest.mark.parametrize('decision,expected', [('diagnostic_only', 1), ('point_target_met', 1), ('target_confirmed', 0)])
def test_cli_target_gate_requires_confirmed_evidence(host_tmp_path, monkeypatch, capsys, decision, expected):
    from evaluations.swebench.cli import main
    report = {'decision': decision, 'fullDomain': {'taskCount': 89},
              'finalDeltaPercentagePoints': 10, 'blockers': []}
    monkeypatch.setattr(subject, 'compare_runs', lambda *a, **kw: (report, host_tmp_path))
    assert main(['compare', '--baseline', 'b', '--candidate', 'c', '--output-root',
                 str(host_tmp_path), '--run-id', 'test', '--require-target']) == expected
    assert json.loads(capsys.readouterr().out)['decision'] == decision


def test_retry_allowance_is_frozen_before_archive_mutation(host_tmp_path):
    from evaluations.swebench.infrastructure_retry import infrastructure_retry_cycle
    baseline, candidate = [_prepared(host_tmp_path, role) for role in ('baseline', 'candidate')]
    path = subject.register_comparison(baseline, candidate, output_root=host_tmp_path/'protocols',
        run_id='bounded-retry', infrastructure_retry_limit=2)
    protocol = json.loads(path.read_text())
    assert protocol['maxInfrastructureRetries'] == 2
    with pytest.raises(ValueError, match='frozen protocol'):
        infrastructure_retry_cycle(candidate, attempt_index=1, max_infrastructure_retries=3,
                                   resume=lambda *args: pytest.fail('must not dispatch'))
    assert not (candidate/'infrastructure-attempts').exists()
