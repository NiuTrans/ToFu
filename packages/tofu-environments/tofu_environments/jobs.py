"""Batch compute workflow: scoped plans, at-most-once submission and readback.

Embeddings supply the existing CAS repository and an actor-bound JobProvider.
No transport, credential discovery, thread or network work occurs on import.
"""
from __future__ import annotations
import json
from typing import Protocol
from .contracts import Actor, EnvironmentError, canonical, seal

class JobProvider(Protocol):
    def validate_job(self, actor: Actor, spec: dict, *, operation_id: str) -> dict: ...
    def submit_job(self, actor: Actor, spec: dict, operation_id: str, admission: dict) -> dict: ...
    def job_status(self, actor: Actor, reference: dict, operation_id: str) -> dict: ...
    def reconcile_job(self, actor: Actor, record: dict) -> dict | None: ...
    def cancel_job(self, actor: Actor, reference: dict, operation_id: str) -> None: ...
    def read_job_output(self, actor: Actor, reference: dict, operation_id: str, name: str): ...


def validate_job_spec(spec):
    from pathlib import Path
    from jsonschema import Draft202012Validator
    schema = json.loads(Path(__file__).with_name('job.schema.json').read_text())
    try:
        Draft202012Validator(schema).validate(spec)
    except Exception as exc:
        raise EnvironmentError('invalid_job_spec', 'Job specification does not match API v1') from exc
    clean = json.loads(canonical(spec))
    resources = clean['resources']
    attempts = clean.get('recovery', {}).get('max_attempts', 1)
    if resources['gpu_count'] * clean['limits']['runtime_seconds'] * attempts > clean['limits']['gpu_seconds']:
        raise EnvironmentError('invalid_job_spec', 'Worst-case runtime across all admitted attempts exceeds total GPU-seconds budget')
    return clean


def validate_reference(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'id'}:
        raise EnvironmentError('provider_protocol', 'Typed job reference required', uncertain=True)
    if not all(isinstance(value[k], str) and 0 < len(value[k]) <= 256 for k in value):
        raise EnvironmentError('provider_protocol', 'Bounded job reference required', uncertain=True)
    return json.loads(canonical(value))


class JobService:
    def __init__(self, repository, resolve_provider):
        self.repository, self.resolve_provider = repository, resolve_provider

    def plan(self, actor, provider_id, spec, request_id):
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
            raise EnvironmentError('invalid_request', 'Bounded request identifier required')
        if not isinstance(provider_id, str) or not 0 < len(provider_id) <= 64:
            raise EnvironmentError('invalid_provider', 'Bounded provider identifier required')
        clean = validate_job_spec(spec)
        provider = self.resolve_provider(actor, provider_id)
        operation_id = 'job-' + seal([actor.scope, request_id])[:32]
        plan = {'scope': actor.scope, 'project_root': actor.project_root, 'provider': provider_id,
                'spec': clean, 'admission': provider.validate_job(actor, clean, operation_id=operation_id)}
        digest = seal(plan)
        record = {'operation_id': operation_id, 'plan': plan, 'plan_hash': digest,
                  'phase': 'planned', 'reference': None, 'error': ''}
        old = self.repository.get(actor, operation_id)
        if old:
            if old['value']['plan_hash'] != digest:
                raise EnvironmentError('request_conflict', 'Request already names another job plan')
            return self._view(old['value'])
        try:
            self.repository.put(actor, operation_id, record, 0)
        except EnvironmentError as exc:
            if exc.code != 'conflict': raise
            old = self.repository.get(actor, operation_id)
            if not old or old['value']['plan_hash'] != digest: raise
            record = old['value']
        return self._view(record)

    def _load(self, actor, operation_id):
        row = self.repository.get(actor, operation_id)
        if not row:
            raise EnvironmentError('not_found', 'No job in this owner scope')
        record = json.loads(canonical(row['value']))
        plan = record['plan']
        if not operation_id.startswith('job-') or plan['scope'] != actor.scope or plan['project_root'] != actor.project_root:
            raise EnvironmentError('scope_changed', 'Job belongs to another project scope')
        if seal(plan) != record['plan_hash']:
            raise EnvironmentError('plan_corrupt', 'Job plan seal does not match')
        return row['version'], record, self.resolve_provider(actor, plan['provider'])

    def submit(self, actor, operation_id, plan_hash, authorize):
        version, record, provider = self._load(actor, operation_id)
        if plan_hash != record['plan_hash'] or not authorize(actor, plan_hash):
            raise EnvironmentError('approval_required', 'Exact job plan authorization required')
        if record['phase'] != 'planned':
            return self._view(record)
        plan = record['plan']
        if provider.validate_job(actor, plan['spec'], operation_id=operation_id) != plan['admission']:
            raise EnvironmentError('provider_changed', 'Job admission changed; replan required')
        record['phase'] = 'submitting'
        claimed = self.repository.put(actor, operation_id, record, version)
        try:
            record['reference'] = validate_reference(provider.submit_job(actor, plan['spec'], operation_id, plan['admission']))
            record['phase'] = 'queued'
        except Exception as exc:
            record['phase'] = 'failed' if isinstance(exc, EnvironmentError) and not exc.uncertain else 'uncertain'
            record['error'] = getattr(exc, 'code', 'external_outcome_unknown')
        self.repository.put(actor, operation_id, record, claimed['version'])
        return self._view(record)

    def cancel(self, actor, operation_id, plan_hash, authorize):
        version, record, provider = self._load(actor, operation_id)
        if plan_hash != record['plan_hash'] or not authorize(actor, plan_hash):
            raise EnvironmentError('approval_required', 'Exact job cancellation authorization required')
        if record['phase'] not in {'queued', 'running', 'failed'}:
            return self._view(record)
        if record['phase'] == 'failed' and record['reference'] is None:
            # A definitively rejected submission has no owned external resource
            # to stop. Preserve its failure instead of inventing cancellation.
            return self._view(record)
        previous = record['phase']
        if previous == 'failed':
            record['execution_failure_before_cancellation'] = {
                'phase': previous, 'exit_code': record.get('exit_code'), 'error': record.get('error')}
        record['phase'] = 'cancelling'
        claimed = self.repository.put(actor, operation_id, record, version)
        try:
            provider.cancel_job(actor, record['reference'], operation_id)
        except Exception as exc:
            record['phase'] = previous if isinstance(exc, EnvironmentError) and not exc.uncertain else 'cancel_uncertain'
            record['error'] = getattr(exc, 'code', 'cancel_outcome_unknown')
        self.repository.put(actor, operation_id, record, claimed['version'])
        return self._view(record)

    def status(self, actor, operation_id):
        version, record, provider = self._load(actor, operation_id)
        if record['phase'] in {'submitting', 'uncertain'}:
            reference = provider.reconcile_job(actor, json.loads(canonical(record)))
            if reference is None:
                return self._view(record)
            record['reference'] = validate_reference(reference)
            record['phase'] = 'queued'
        if record['phase'] in {'queued', 'running', 'cancelling', 'cancel_uncertain'}:
            snapshot = json.loads(canonical(provider.job_status(actor, record['reference'], operation_id)))
            if set(snapshot) != {'state', 'exit_code'} or snapshot['state'] not in {'queued', 'running', 'succeeded', 'failed', 'cancelled'}:
                raise EnvironmentError('provider_protocol', 'Invalid job status')
            exit_code = snapshot['exit_code']
            if exit_code is not None and type(exit_code) is not int:
                raise EnvironmentError('provider_protocol', 'Exit code must be an integer or null')
            if snapshot['state'] == 'succeeded' and exit_code != 0:
                raise EnvironmentError('provider_protocol', 'Success requires proven zero exit code')
            if record['phase'] in {'cancelling', 'cancel_uncertain'} and snapshot['state'] in {'queued', 'running'}:
                return self._view(record)
            record['phase'] = snapshot['state']
            record['exit_code'] = exit_code
            self.repository.put(actor, operation_id, record, version)
        return self._view(record)

    def collect(self, actor, operation_id, destination):
        """Read owned outputs into a private new directory; seal durable receipts.

        Provider returns bounded byte chunks from the exact owned job. Destination
        is a trusted caller path, never interpreted as a remote job path.
        Local partial files are removed on failure; durable state is untouched.
        """
        import hashlib
        import os
        import shutil
        import tempfile
        from pathlib import Path, PurePosixPath
        version, record, provider = self._load(actor, operation_id)
        if record['phase'] != 'succeeded':
            raise EnvironmentError('job_not_succeeded', 'Collect requires proven successful job exit')
        root = Path(destination)
        if not root.is_dir() or root.is_symlink():
            raise EnvironmentError('invalid_destination', 'Trusted real destination directory required')
        staging = Path(tempfile.mkdtemp(prefix='compute-result-', dir=root))
        limit = record['plan']['spec']['limits']['result_bytes']
        total = 0
        receipts = []
        try:
            for name in record['plan']['spec']['outputs']:
                remote = PurePosixPath(name)
                if remote.is_absolute() or '..' in remote.parts or not remote.parts or '\\' in name:
                    raise EnvironmentError('invalid_output', 'Relative output path required')
                target = staging.joinpath(*remote.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with target.open('xb') as output:
                    for chunk in provider.read_job_output(actor, record['reference'], operation_id, name):
                        if not isinstance(chunk, bytes) or len(chunk) > 1048576:
                            raise EnvironmentError('provider_protocol', 'Output chunks must be bytes of at most 1 MiB')
                        total += len(chunk)
                        if total > limit:
                            raise EnvironmentError('result_limit', 'Collected output exceeds approved result-byte bound')
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                receipts.append({'name': name, 'bytes': size, 'sha256': digest.hexdigest()})
            previous = record.get('outputs')
            if previous is not None and previous != receipts:
                raise EnvironmentError('output_changed', 'Terminal job outputs differ from sealed receipts')
            record['outputs'] = receipts
            self.repository.put(actor, operation_id, record, version)
        except BaseException:
            shutil.rmtree(staging)
            raise
        return {'operation_id': operation_id, 'reference': record['reference'],
                'directory': str(staging), 'outputs': receipts, 'total_bytes': total,
                'durable': self.repository.durable}

    def _view(self, record):
        return {**json.loads(canonical(record)), 'durable': self.repository.durable}
