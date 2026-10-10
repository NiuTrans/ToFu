"""Condition-based retry evidence for the existing durable delivery queue.

Entry points: input_fingerprint, retry_evidence, conflicted_paths. Inputs are
immutable Git objects; receipts remain owned by delivery.py and never authorize
model execution. Schema: contracts/workspace_delivery_retry_v1.schema.json.
"""
from __future__ import annotations

import hashlib
import json

MAX_CONFLICT_PATHS = 64
MAX_CONFLICT_PATH_BYTES = 4096


def input_fingerprint(*, baseline, checkpoint, branch, source_tree, command, gate_options):
    payload = dict(baseline=baseline, checkpoint=checkpoint, branch=branch,
                   sourceTree=source_tree, command=command, gateOptions=gate_options)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def retry_evidence(condition, fingerprint, baseline, checkpoint, source, paths=()):
    evidence = dict(condition=condition, fingerprint=fingerprint,
                    baselineSha=baseline, checkpointSha=checkpoint, sourceSha=source)
    if condition == 'conflict_inputs_changed':
        selected, encoded_bytes = [], 2
        for path in paths:
            size = len(json.dumps(path, ensure_ascii=False).encode('utf-8')) + 1
            if len(selected) >= MAX_CONFLICT_PATHS or encoded_bytes + size > MAX_CONFLICT_PATH_BYTES:
                continue
            selected.append(path)
            encoded_bytes += size
        evidence.update(conflictPaths=selected, conflictPathCount=len(paths),
                        conflictPathsTruncated=len(selected) < len(paths),
                        resolution='owned_repair_checkpoint_required')
    return evidence


def conflicted_paths(unmerged_index):
    # Git -z preserves literal tabs/newlines in names. The first tab separates
    # stage metadata; paths are identifiers, never shell programs or file reads.
    return sorted({entry.split('\t', 1)[1] for entry in unmerged_index.split('\0')
                   if '\t' in entry})


def prepare_conflict_repair(row, receipt):
    """Build a scoped proposal from immutable registered repair bytes.

    Called outside writer/repository locks; the caller rechecks registration,
    terminal intent and receipt before admitting the proposal. A model may
    supply source edits through ordinary authorized tools, never canonical
    writes or unverified arbitrary patch publication.
    """
    import contextlib
    from pathlib import Path
    import tempfile
    from lib import integration_control as ic

    retry = receipt.get('retry') or {}
    checkpoint = row.get('checkpoint_sha')
    previous_repair = receipt.get('repair') or {}
    previous_checkpoint = previous_repair.get('registeredCheckpointSha') or retry.get('checkpointSha')
    if ((retry.get('condition') != 'conflict_inputs_changed' and not previous_repair)
            or not checkpoint or checkpoint == previous_checkpoint):
        return previous_repair or None
    baseline = (row.get('origin') or {}).get('sourceSnapshot')
    source = retry.get('sourceSha') or previous_repair['baselineSha']
    root = Path(row['project_root'])
    paths = [path for path in ic._git(root, ['diff', '--name-only', '-z', baseline,
                                           previous_checkpoint], check=True).stdout.split('\0') if path]
    repair_paths = [path for path in ic._git(root, ['diff', '--name-only', '-z',
                                                  previous_checkpoint, checkpoint],
                                            check=True).stdout.split('\0') if path]
    if set(repair_paths) - set(paths):
        raise ic.IntegrationError('Conflict repair changed paths outside the original task delta; create new owned work')
    patch = ic._git(root, ['--literal-pathspecs', 'diff', '--binary', '--full-index', '--no-ext-diff',
                          '--no-textconv', source, checkpoint, '--', *paths], check=True).stdout
    with tempfile.TemporaryDirectory(prefix='delivery-repair-', dir=str(ic._workspace_root())) as directory:
        patch_path = Path(directory) / 'repair.patch'
        patch_path.write_text(patch, encoding='utf-8')
        scratch = Path(directory) / 'proposal'
        ic._git(root, ['worktree', 'add', '--detach', str(scratch), source], timeout=90, check=True)
        try:
            if patch:
                ic._git(scratch, ['apply', '--whitespace=nowarn', str(patch_path)], check=True)
            target = ic._alternate_index_checkpoint(scratch, source, row['task_id'])
        finally:
            # Only this call's temporary proposal is reclaimable, never the
            # registered source or the durable conflict input/checkpoint.
            with contextlib.suppress(Exception):
                ic._remove_controlled_worktree(root, scratch)
    return dict(originalBaselineSha=baseline, registeredCheckpointSha=checkpoint,
                baselineSha=source, checkpointSha=target)


def pin_receipt(root, ref, target, persist):
    """Replace one evidence anchor with rollback on failed durable admission.

    Caller owns repository serialization. CAS restoration cannot overwrite a
    concurrent external ref move; failure remains visible to the caller.
    """
    from lib import integration_control as ic

    previous = ic._rev(root, ref)
    ic._git(root, ['update-ref', ref, target, previous], check=True)
    try:
        return persist()
    except BaseException:
        restore = (['update-ref', ref, previous, target] if previous
                   else ['update-ref', '-d', ref, target])
        ic._git(root, restore, check=True)
        raise
