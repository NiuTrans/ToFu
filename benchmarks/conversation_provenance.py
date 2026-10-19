"""Source/artifact receipts for storage benchmarks, not a new build identity.

Capture reuses the storage certification source inventory and native engine
fingerprint/build-info owners. Qualification is pure: callers supply a fresh
source snapshot, so inspecting an old report never executes its artifact path.
A stable frozen build is not necessarily a build of the current checkout.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath
import re

from scripts.certify_storage_backends import ROOT, fingerprint, verification_sources
from serverctl_pkg.native_engine import build_identity, source_fingerprint

FORMAT = 'tofu.benchmark-provenance/v1'
# The existing certification inventory owns application/SDK/contract coverage.
# These measurement/identity entrypoints are additional inputs, not another
# implementation of the application's source discovery or native fingerprint.
BENCHMARK_SOURCES = (
    'benchmarks/conversation_acceptance.py',
    'benchmarks/conversation_authority.py',
    'benchmarks/conversation_compare.py',
    'benchmarks/conversation_concurrency.py',
    'benchmarks/conversation_concurrency_contract.json',
    'benchmarks/conversation_provenance.py',
    'benchmarks/storage_write_pipeline.py',
    'scripts/tofudb_bench.py',
    'scripts/tofudb_charter_assess.py',
    'serverctl_pkg/native_engine.py',

    'tofu_workers/__init__.py',
    'tofu_workers/executor.py',
    'runtime_guards.py',
    'bootstrap_pkg/__init__.py',
    'bootstrap_pkg/resource_policy_generated.py',
)
REQUIRED_SOURCES = BENCHMARK_SOURCES + (
    'scripts/certify_storage_backends.py',
    'scripts/tofudb_perf_compare.py',
    'lib/turn_lifecycle.py',
    'lib/agent_core/worker_executor.py',
    'lib/storage/v2_client.py',
    'lib/storage/v2_embedded_client.py',
    'lib/storage/v2_semantics.py',
    'tofu_db/embedded.py',
    'tofu_db/generated_contract.py',
    'tofu_db/wire.py',
    'lib/tasks_pkg/event_delivery.py',
    'lib/tasks_pkg/event_delivery_profile.py',
    'lib/tasks_pkg/event_delivery_profile_metrics.py',
    'lib/storage/generated_task_event_delivery_profile.py',
    'contracts/task_event_delivery_v1.json',
    'contracts/storage_v2.json',
    'contracts/storage_performance_charter_v1.json',
    'contracts/conversation_sync_v3.yaml',
)


def is_sha256(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def capture_sources() -> dict:
    """Fresh inventory, including uncommitted sources and generated SDK inputs."""
    files = verification_sources()
    files.update({name: fingerprint(ROOT / name) for name in REQUIRED_SOURCES})
    return {'native_source_sha256': source_fingerprint(ROOT), 'files': files}


def capture_declared_artifact(binary: Path | None, *, embedded: bool = False) -> dict | None:
    """Freeze the requested bytes before the existing engine pins its copies."""
    if binary is None:
        return None
    result = {'binary_sha256': fingerprint(binary)}
    if embedded:
        result['shared_library_sha256'] = fingerprint(binary.parent / 'libtofu_db.so')
    return result


def capture_artifact(binary: Path, *, embedded_build_identity: dict | None = None) -> dict:
    """Observe the pinned executable and, when used, the actual loaded library.

    Only build-info runs here; no authority is opened. The embedded identity
    must come from the existing loaded runtime, never a guessed sibling build.
    """
    result = capture_declared_artifact(binary, embedded=embedded_build_identity is not None)
    result['build_identity'] = build_identity(binary)
    if embedded_build_identity is not None:
        result['embedded_build_identity'] = deepcopy(embedded_build_identity)
    return result


def _valid_sources(value) -> bool:
    if not isinstance(value, dict) or not is_sha256(value.get('native_source_sha256')):
        return False
    files = value.get('files')
    if not isinstance(files, dict) or not set(REQUIRED_SOURCES).issubset(files):
        return False
    return all(isinstance(name, str) and name and not PurePosixPath(name).is_absolute()
               and '..' not in PurePosixPath(name).parts
               and PurePosixPath(name).as_posix() == name and is_sha256(digest)
               for name, digest in files.items())


def _valid_build(value) -> bool:
    return (isinstance(value, dict) and value.get('format') == 'tofu-db-build.v1'
            and is_sha256(value.get('source_sha256')))


def qualify_pipeline_sources(pipeline, *, sources) -> dict:
    """Bind the pipeline owner's existing frozen manifest, including overrides.

    A pinned historical coordinator can be useful diagnostic evidence without
    being a measurement of the current coordinator. Never infer sameness from
    the pipeline's optimistic latency verdict or an unchanged-sources flag alone.
    """
    frozen_failures = []
    current_failures = []
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    manifest = pipeline.get('source_manifest_sha256')
    aliases = {'coordinator': 'lib/tasks_pkg/event_delivery.py',
               'benchmark': 'benchmarks/storage_write_pipeline.py'}
    if pipeline.get('contract') != 'tofu.storage-write-pipeline/v1':
        frozen_failures.append('delivery pipeline provenance missing or unsupported')
    if pipeline.get('sources_unchanged') is not True:
        frozen_failures.append('delivery pipeline sources changed or stability observation missing')
    valid_manifest = (isinstance(manifest, dict) and set(aliases).issubset(manifest)
                      and all(isinstance(name, str) and is_sha256(value)
                              for name, value in manifest.items()))
    if not valid_manifest:
        frozen_failures.append('delivery pipeline source manifest missing or malformed')
    else:
        for field, name in (('delivery_source_sha256', 'coordinator'),
                            ('benchmark_source_sha256', 'benchmark')):
            if pipeline.get(field) != manifest[name]:
                frozen_failures.append(f'delivery pipeline {field} contradicts source manifest')
        files = sources.get('files', {}) if isinstance(sources, dict) else {}
        if not isinstance(files, dict) or any(files.get(aliases.get(name, name)) != digest
                                              for name, digest in manifest.items()):
            current_failures.append('delivery pipeline sources differ from current checkout inventory')
    failures = frozen_failures + current_failures
    return {'current_source_qualified': not failures,
            'frozen_build_comparable': not frozen_failures,
            'failures': failures, 'frozen_failures': frozen_failures}


def qualify_provenance(receipt, *, backend, binary_sha256=None, authority=None,
                       current_sources) -> dict:
    """Fail closed on absent, contradictory or changed measurement provenance.

    ``current_sources`` is an independently captured checkout snapshot, not a
    report field. Frozen comparability validates stable self-consistent receipt
    evidence only; it grants neither current qualification nor timing admission.
    """
    frozen_failures = []
    current_failures = []
    receipt = receipt if isinstance(receipt, dict) else {}
    authority = authority if isinstance(authority, dict) else {}
    if receipt.get('format') != FORMAT:
        frozen_failures.append('provenance receipt missing or unsupported')
    if backend not in ('tofudb', 'sqlite', 'rawsqlite', 'legacy'):
        frozen_failures.append('provenance backend missing or unsupported')
    if authority.get('backend') != backend:
        frozen_failures.append('provenance authority backend contradicts report')
    before, after = receipt.get('sources_before'), receipt.get('sources_after')
    if not _valid_sources(before) or not _valid_sources(after):
        frozen_failures.append('provenance source inventory missing or malformed')
    elif before != after:
        frozen_failures.append('provenance sources changed during measurement')
    if not _valid_sources(current_sources):
        current_failures.append('current checkout source inventory unavailable or malformed')
    elif before != current_sources or after != current_sources:
        current_failures.append('provenance does not match the complete current checkout source inventory')

    for field in ('declared_artifact', 'artifact_before', 'artifact_after'):
        if field not in receipt:
            frozen_failures.append(f'provenance {field} observation missing')
    declared = receipt.get('declared_artifact')
    initial, final = receipt.get('artifact_before'), receipt.get('artifact_after')
    if backend != 'tofudb':
        if any(value is not None for value in (declared, initial, final)):
            frozen_failures.append('non-native authority cannot claim a native artifact')
    else:
        embedded = authority.get('client_mode') == 'embedded'
        if authority.get('client_mode') not in ('application', 'wire', 'embedded'):
            frozen_failures.append('native client mode missing or malformed')
        if not isinstance(declared, dict) or not is_sha256(declared.get('binary_sha256')):
            frozen_failures.append('declared native artifact missing or malformed')
            declared = {}
        if not is_sha256(binary_sha256) or binary_sha256 != declared.get('binary_sha256'):
            frozen_failures.append('report binary SHA256 does not match declared artifact')

        if 'binary_sha256' in authority and authority['binary_sha256'] != binary_sha256:
            frozen_failures.append('authority binary SHA256 contradicts declared artifact')
        if not embedded and any(key in value for value in (declared, authority)
                                for key in ('shared_library_sha256', 'embedded_build_identity')):
            frozen_failures.append('non-embedded authority has contradictory library evidence')
        for label, artifact in (('before', initial), ('after', final)):
            if (not isinstance(artifact, dict) or not is_sha256(artifact.get('binary_sha256'))
                    or not _valid_build(artifact.get('build_identity'))):
                frozen_failures.append(f'native artifact {label} missing or malformed')
                continue
            if artifact['binary_sha256'] != declared.get('binary_sha256'):
                frozen_failures.append(f'native artifact {label} differs from declared binary')
            if artifact['build_identity'] != authority.get('build_identity'):
                frozen_failures.append(f'native artifact {label} build identity contradicts authority')
            if embedded:
                library = artifact.get('shared_library_sha256')
                identity = artifact.get('embedded_build_identity')
                if (not is_sha256(library) or library != declared.get('shared_library_sha256')
                        or library != authority.get('shared_library_sha256')):
                    frozen_failures.append(f'embedded artifact {label} does not match declared library')
                if (not isinstance(identity, dict) or not is_sha256(identity.get('source_sha256'))
                        or identity != authority.get('embedded_build_identity')
                        or identity.get('source_sha256') != artifact['build_identity']['source_sha256']):
                    frozen_failures.append(f'embedded artifact {label} build identity differs from executable')
            elif any(key in artifact for key in ('shared_library_sha256', 'embedded_build_identity')):
                frozen_failures.append('non-embedded authority has contradictory library evidence')
        if initial != final:
            frozen_failures.append('native artifact changed during measurement')
        source = authority.get('checkout_source_sha256')
        identity = authority.get('build_identity')
        if (not is_sha256(source) or not _valid_build(identity)
                or not isinstance(before, dict) or source != before.get('native_source_sha256')):
            frozen_failures.append('authority native source identity missing or contradictory')
        else:
            matches = identity['source_sha256'] == source
            if authority.get('source_matches_checkout') is not matches:
                frozen_failures.append('source_matches_checkout contradicts native build/checkout hashes')
            if not matches:
                current_failures.append('native build source differs from checkout')
    failures = frozen_failures + current_failures
    return {'current_source_qualified': not failures,
            'frozen_build_comparable': not frozen_failures,
            'failures': failures, 'frozen_failures': frozen_failures}
