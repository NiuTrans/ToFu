"""Database-independent, byte-bounded diagnostics for humans and language models.

Entry point: :func:`diagnose_logs` reads the compact ``incident.jsonl`` family,
falls back to a bounded tail of ``error.log`` for pre-migration evidence, and
returns one self-contained report under a caller-selected byte ceiling.  It
never imports the storage repository: diagnostics must remain available when
SQLite, PostgreSQL, or the storage sidecar is the incident.

The report is an index, not a replacement for evidence.  Fingerprints, source
locations, correlation ids and short redacted samples let a debugging agent
select a tiny raw slice only when deeper inspection is necessary.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from lib.log_aggregates import fingerprint_text, replay_log_lines
from lib.log_policy import (
    LOG_FILE_MODE, POLICY_BY_FILENAME, policy_manifest, total_log_budget_bytes,
)
from lib.log_redaction import bound_text, redact_text, sanitize_value


SCHEMA_VERSION = 1
_LEVEL_NUMBER = {
    'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50,
}
_SELECTOR_KEYS = ('request_id', 'conversation_id', 'task_id', 'trace_id')
_MAX_SCAN_BYTES = 64 * 1024 * 1024
_DEFAULT_SCAN_BYTES = 8 * 1024 * 1024
_MAX_SAFE_EPOCH = 253_402_300_799.0


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _timestamp(value: object) -> float:
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) and 0 <= parsed <= _MAX_SAFE_EPOCH else 0.0
    raw = str(value or '').strip()
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
        return parsed if math.isfinite(parsed) and 0 <= parsed <= _MAX_SAFE_EPOCH else 0.0
    except (ValueError, OverflowError, OSError):
        try:
            return datetime.strptime(raw[:19], '%Y-%m-%d %H:%M:%S').replace(
                tzinfo=timezone.utc).timestamp()
        except (ValueError, OverflowError, OSError):
            return 0.0


def _rotation_family(path: Path) -> list[Path]:
    """Return current then rotated files, newest first, without symlinks."""
    candidates = []
    try:
        for candidate in path.parent.iterdir():
            is_active = candidate.name == path.name
            is_numbered_backup = bool(re.fullmatch(
                re.escape(path.name) + r'\.\d+', candidate.name))
            if not is_active and not is_numbered_backup:
                continue
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                info = candidate.stat()
            except OSError:
                continue
            candidates.append((is_active, info.st_mtime,
                               candidate.name, candidate))
    except OSError:
        return []
    return [item[3] for item in sorted(
        candidates, key=lambda item: (item[0], item[1], item[2]), reverse=True)]


def _tail_lines(path: Path, max_bytes: int) -> tuple[list[str], int]:
    """Read complete UTF-8 lines from a bounded file tail."""
    try:
        with path.open('rb') as handle:
            size = handle.seek(0, os.SEEK_END)
            keep = min(size, max(0, int(max_bytes)))
            start = max(0, size - keep)
            starts_on_line_boundary = False
            if start:
                handle.seek(start - 1)
                starts_on_line_boundary = handle.read(1) == b'\n'
            handle.seek(start)
            data = handle.read(keep)
    except OSError:
        return [], 0
    if start and not starts_on_line_boundary:
        boundary = data.find(b'\n')
        data = data[boundary + 1:] if boundary >= 0 else b''
    return data.decode('utf-8', 'replace').splitlines(), len(data)


def _selector_match(entry: dict, selectors: dict[str, str]) -> bool:
    for key, expected in selectors.items():
        if expected and str(entry.get(key) or '') != expected:
            return False
    return True


def _new_bucket(entry: dict, timestamp: float) -> dict:
    return {
        'fingerprint': str(entry.get('fingerprint') or '')[:64],
        'level': str(entry.get('level') or 'WARNING')[:16],
        'logger': str(entry.get('logger') or 'unknown')[:256],
        'template': redact_text(entry.get('template') or '', max_chars=300),
        'exception': redact_text(entry.get('exception') or '', max_chars=160),
        'source': str(entry.get('source') or '')[:200],
        'event': str(entry.get('event') or '')[:128],
        'count': 0,
        'checkpoints': 0,
        'first_seen_epoch': timestamp,
        'last_seen_epoch': timestamp,
        'sample': redact_text(entry.get('sample') or '', max_chars=700),
        '_correlations': {key: set() for key in _SELECTOR_KEYS},
    }


def _add_entry(buckets: dict[str, dict], entry: dict, timestamp: float) -> None:
    fingerprint = str(entry.get('fingerprint') or '')
    if not fingerprint:
        fingerprint, template = fingerprint_text(
            str(entry.get('level') or 'WARNING'),
            str(entry.get('logger') or 'unknown'),
            str(entry.get('sample') or entry.get('template') or ''))
        entry = dict(entry, fingerprint=fingerprint, template=template)
    bucket = buckets.get(fingerprint)
    if bucket is None:
        bucket = _new_bucket(entry, timestamp)
        buckets[fingerprint] = bucket
    delta = _bounded_int(entry.get('occurrence_delta'), 1, 1, 1_000_000_000)
    bucket['count'] += delta
    bucket['checkpoints'] += 1
    bucket['first_seen_epoch'] = min(bucket['first_seen_epoch'], timestamp)
    if timestamp >= bucket['last_seen_epoch']:
        bucket['last_seen_epoch'] = timestamp
        bucket['sample'] = redact_text(entry.get('sample') or '', max_chars=700)
        for key in ('source', 'exception', 'event'):
            value = str(entry.get(key) or '')
            if value:
                bucket[key] = value[:200]
    if _LEVEL_NUMBER.get(str(entry.get('level') or ''), 0) > _LEVEL_NUMBER.get(
            bucket['level'], 0):
        bucket['level'] = str(entry.get('level'))
    for key in _SELECTOR_KEYS:
        value = str(entry.get(key) or '')[:128]
        if value and len(bucket['_correlations'][key]) < 8:
            bucket['_correlations'][key].add(value)


def _public_bucket(bucket: dict, now: float) -> dict:
    output = {key: value for key, value in bucket.items()
              if key != '_correlations' and value not in ('', None)}
    output['first_seen'] = datetime.fromtimestamp(
        bucket['first_seen_epoch'], timezone.utc).isoformat()
    output['last_seen'] = datetime.fromtimestamp(
        bucket['last_seen_epoch'], timezone.utc).isoformat()
    output['age_seconds'] = max(0, int(now - bucket['last_seen_epoch']))
    correlations = {
        key: sorted(values) for key, values in bucket['_correlations'].items()
        if values
    }
    if correlations:
        output['correlations'] = correlations
    output.pop('first_seen_epoch', None)
    output.pop('last_seen_epoch', None)
    return output


def _scan_incidents(log_dir: Path, *, cutoff: float, scan_bytes: int,
                    selectors: dict[str, str], requesting_user_id: str,
                    include_all_users: bool) -> tuple[list[dict], dict]:
    buckets: dict[str, dict] = {}
    timeline = []
    scanned = 0
    invalid = 0
    records = 0
    paths = []
    remaining = scan_bytes
    for path in _rotation_family(log_dir / 'incident.jsonl'):
        if remaining <= 0:
            break
        lines, used = _tail_lines(path, remaining)
        remaining -= used
        scanned += used
        paths.append(str(path))
        for line in lines:
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                invalid += 1
                continue
            if not isinstance(entry, dict):
                invalid += 1
                continue
            ts = _timestamp(entry.get('timestamp'))
            if ts < cutoff or not _selector_match(entry, selectors):
                continue
            entry_user = str(entry.get('user_id') or '')
            if (not include_all_users and requesting_user_id
                    and entry_user != requesting_user_id):
                continue
            _add_entry(buckets, entry, ts)
            records += 1
            timeline.append({
                'timestamp': entry.get('timestamp'),
                'level': entry.get('level'),
                'fingerprint': entry.get('fingerprint'),
                'event': entry.get('event') or '',
                'request_id': entry.get('request_id') or '',
                'sample': redact_text(entry.get('sample') or '', max_chars=240),
            })
            if len(timeline) > 200:
                del timeline[:len(timeline) - 200]
    return list(buckets.values()), {
        'source': 'incident_journal', 'paths': paths, 'bytes_scanned': scanned,
        'records': records, 'invalid_lines': invalid,
        'timeline': sorted(timeline, key=lambda row: str(row.get('timestamp') or ''),
                           reverse=True),
    }


def _scan_error_fallback(log_dir: Path, *, cutoff: float,
                         scan_bytes: int) -> tuple[list[dict], dict]:
    buckets: dict[str, dict] = {}
    scanned = 0
    paths = []
    remaining = scan_bytes
    records = 0
    for path in _rotation_family(log_dir / 'error.log'):
        if remaining <= 0:
            break
        lines, used = _tail_lines(path, remaining)
        remaining -= used
        scanned += used
        paths.append(str(path))
        for level, logger_name, text, ts_ms in replay_log_lines(lines):
            ts = ts_ms / 1000 if ts_ms else 0.0
            if ts < cutoff:
                continue
            safe_text = redact_text(text, max_chars=2_000)
            fingerprint, template = fingerprint_text(
                level, logger_name, safe_text)
            _add_entry(buckets, {
                'fingerprint': fingerprint, 'template': template,
                'level': level, 'logger': logger_name,
                'sample': safe_text, 'occurrence_delta': 1,
            }, ts)
            records += 1
    return list(buckets.values()), {
        'source': 'error_log_fallback', 'paths': paths,
        'bytes_scanned': scanned, 'records': records, 'invalid_lines': 0,
        'timeline': [],
    }


def _load_maintenance_report(data_dir: Path | None) -> dict:
    if data_dir is None:
        return {}
    path = data_dir / 'log-maintenance-last.json'
    try:
        with path.open(encoding='utf-8') as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    compact = {
        'timestamp': value.get('timestamp'),
        'dry_run': bool(value.get('dry_run')),
        'skipped': bool(value.get('skipped')),
        'before_bytes': value.get('before_bytes', 0),
        'after_bytes_estimate': value.get('after_bytes_estimate', 0),
        'reclaimed_bytes_estimate': value.get('reclaimed_bytes_estimate', 0),
        'budget_bytes': value.get('budget_bytes', 0),
        'over_budget_bytes': value.get('over_budget_bytes', 0),
        'rotated_count': len(value.get('rotated') or []),
        'compacted_count': len(value.get('compacted') or []),
        'removed_count': len(value.get('removed') or []),
        'permissions_hardened_count': len(
            value.get('permissions_hardened') or []),
        'unmanaged_count': len(value.get('unmanaged') or []),
        'errors': (value.get('errors') or [])[:10],
    }
    return sanitize_value(compact, field_name='maintenance', max_items=30,
                          max_string_chars=300)


def _log_inventory(log_dir: Path) -> dict:
    files = []
    total = 0
    unmanaged = []
    insecure_managed = []
    try:
        paths = list(log_dir.iterdir())
    except OSError:
        paths = []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
        except OSError:
            continue
        total += info.st_size
        family = ''
        if path.name in POLICY_BY_FILENAME:
            family = POLICY_BY_FILENAME[path.name].name
        else:
            for filename, policy in POLICY_BY_FILENAME.items():
                if '*' not in filename and path.name.startswith(filename + '.'):
                    family = policy.name
                    break
        if not family and re.fullmatch(r'tofu_faulthandler_\d+\.log', path.name):
            family = 'faulthandler_process'
        row = {
            'name': path.name, 'bytes': info.st_size,
            'modified': datetime.fromtimestamp(
                info.st_mtime, timezone.utc).isoformat(),
            'family': family or 'unmanaged',
        }
        files.append(row)
        if not family:
            unmanaged.append(row)
        elif stat.S_IMODE(info.st_mode) != LOG_FILE_MODE:
            insecure_managed.append({
                'name': path.name,
                'mode': format(stat.S_IMODE(info.st_mode), '04o'),
                'expected_mode': format(LOG_FILE_MODE, '04o'),
            })
    files.sort(key=lambda row: (-row['bytes'], row['name']))
    return {
        'total_bytes': total,
        'budget_bytes': total_log_budget_bytes(),
        'over_budget_bytes': max(0, total - total_log_budget_bytes()),
        'file_count': len(files),
        'largest_files': files[:16],
        'unmanaged_files': unmanaged[:24],
        'insecure_managed_count': len(insecure_managed),
        'insecure_managed_files': insecure_managed[:24],
    }


def _action_hints(items: list[dict], inventory: dict) -> list[str]:
    hints = []
    joined = ' '.join(
        '%s %s %s' % (item.get('logger', ''), item.get('template', ''),
                       item.get('exception', '')) for item in items[:10]).lower()
    if inventory.get('over_budget_bytes', 0) > 0:
        hints.append('Run log maintenance; the direct log directory is over budget.')
    if inventory.get('insecure_managed_count', 0) > 0:
        hints.append('Run log maintenance; managed evidence has non-private file modes.')
    if any(token in joined for token in ('storage', 'postgres', 'database', 'sqlite')):
        hints.append('Check storage-sidecar/PG health first; this report does not depend on either.')
    if any(token in joined for token in ('queue recovered', 'shed', 'logbudget')):
        hints.append('A flood-control signal fired; fix the leading fingerprint, not the shed notices.')
    if any(token in joined for token in ('oom', 'cgroup', 'sigkill', 'memory')):
        hints.append('Correlate the timestamp with cgroup_pressure.log and worker restart state.')
    if not hints and items:
        hints.append('Start with the highest-count/highest-level fingerprint and use its correlation ids for a narrow evidence lookup.')
    return hints[:4]


def _encoded_bytes(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                          default=str).encode('utf-8'))


def _fit_report(report: dict, max_bytes: int) -> dict:
    """Deterministically shrink optional detail until JSON fits the ceiling."""
    report['truncated'] = False
    for item in report.get('incidents', []):
        item['sample'] = bound_text(item.get('sample', ''), 500)
    # Reserve a small margin for ``output_bytes`` and its changing digit count.
    target_bytes = max(1024, max_bytes - 64)
    while _encoded_bytes(report) > target_bytes:
        report['truncated'] = True
        timeline = report.get('timeline') or []
        if timeline:
            timeline.pop()
            continue
        unmanaged = report.get('inventory', {}).get('unmanaged_files') or []
        if unmanaged:
            unmanaged.pop()
            continue
        insecure = report.get('inventory', {}).get(
            'insecure_managed_files') or []
        if insecure:
            insecure.pop()
            continue
        largest = report.get('inventory', {}).get('largest_files') or []
        if len(largest) > 5:
            largest.pop()
            continue
        maintenance = report.get('maintenance')
        if isinstance(maintenance, dict) and len(maintenance) > 2:
            report['maintenance'] = {
                'available': True,
                'over_budget_bytes': maintenance.get('over_budget_bytes', 0),
            }
            continue
        incidents = report.get('incidents') or []
        if any(len(item.get('sample', '')) > 240 for item in incidents):
            for item in incidents:
                item['sample'] = bound_text(item.get('sample', ''), 240)
            continue
        if len(incidents) > 3:
            incidents.pop()
            continue
        hints = report.get('action_hints') or []
        if len(hints) > 2:
            hints.pop()
            continue
        if len(incidents) > 1:
            incidents.pop()
            continue
        report = {
            'schema_version': SCHEMA_VERSION,
            'generated_at': report.get('generated_at'),
            'summary': report.get('summary', {}),
            'incidents': (report.get('incidents') or [])[:1],
            'truncated': True,
        }
        break
    summary = report.get('summary')
    if isinstance(summary, dict):
        summary['returned_fingerprints'] = len(report.get('incidents') or [])
    report['output_bytes'] = _encoded_bytes(report)
    # ``output_bytes`` itself changes the size by a few digits.
    report['output_bytes'] = _encoded_bytes(report)
    return report


def diagnose_logs(log_dir: str | os.PathLike[str], *,
                  data_dir: str | os.PathLike[str] | None = None,
                  window_hours: float = 24.0, max_items: int = 20,
                  max_output_bytes: int = 32 * 1024,
                  scan_bytes: int = _DEFAULT_SCAN_BYTES,
                  request_id: str = '', conversation_id: str = '',
                  task_id: str = '', trace_id: str = '',
                  requesting_user_id: str = '',
                  include_all_users: bool = False,
                  now: float | None = None) -> dict:
    """Build one redacted diagnostic report without contacting storage."""
    current = time.time() if now is None else float(now)
    if not math.isfinite(current) or not 0 <= current <= _MAX_SAFE_EPOCH:
        current = time.time()
    try:
        requested_hours = float(window_hours)
    except (TypeError, ValueError, OverflowError):
        requested_hours = 24.0
    if not math.isfinite(requested_hours):
        requested_hours = 24.0
    hours = max(0.05, min(24 * 30, requested_hours))
    item_limit = _bounded_int(max_items, 20, 1, 100)
    output_limit = _bounded_int(max_output_bytes, 32 * 1024, 4 * 1024,
                                256 * 1024)
    scan_limit = _bounded_int(scan_bytes, _DEFAULT_SCAN_BYTES,
                              64 * 1024, _MAX_SCAN_BYTES)
    selectors = {
        key: str(value or '').strip()[:128]
        for key, value in {
            'request_id': request_id, 'conversation_id': conversation_id,
            'task_id': task_id, 'trace_id': trace_id,
        }.items() if value
    }
    root = Path(log_dir).resolve()
    data_root = Path(data_dir).resolve() if data_dir is not None else None
    buckets, scan = _scan_incidents(
        root, cutoff=current - hours * 3600, scan_bytes=scan_limit,
        selectors=selectors, requesting_user_id=str(requesting_user_id or ''),
        include_all_users=bool(include_all_users))
    # A selector/user filter returning no matches must never fall back to an
    # unscoped legacy text log. Legacy fallback is only safe for an explicitly
    # all-user, unfiltered operator diagnosis.
    fallback_allowed = (
        not selectors and (not requesting_user_id or include_all_users))
    if not buckets and fallback_allowed:
        buckets, scan = _scan_error_fallback(
            root, cutoff=current - hours * 3600, scan_bytes=scan_limit)

    ordered = sorted(
        buckets,
        key=lambda row: (
            -_LEVEL_NUMBER.get(row.get('level', ''), 0),
            -int(row.get('count') or 0),
            -float(row.get('last_seen_epoch') or 0),
            row.get('fingerprint', ''),
        ),
    )
    incidents = [_public_bucket(row, current) for row in ordered[:item_limit]]
    inventory = _log_inventory(root)
    report = {
        'schema_version': SCHEMA_VERSION,
        'generated_at': datetime.fromtimestamp(current, timezone.utc).isoformat(),
        'scope': {
            'window_hours': hours,
            'selectors': selectors,
            'requesting_user_id': str(requesting_user_id or '')[:128],
            'include_all_users': bool(include_all_users),
            'max_output_bytes': output_limit,
        },
        'summary': {
            'source': scan['source'],
            'storage_independent': True,
            'bytes_scanned': scan['bytes_scanned'],
            'physical_checkpoints': scan['records'],
            'occurrences': sum(int(row.get('count') or 0) for row in ordered),
            'unique_fingerprints': len(ordered),
            'returned_fingerprints': len(incidents),
            'invalid_lines': scan['invalid_lines'],
        },
        'incidents': incidents,
        'timeline': scan['timeline'][:min(20, item_limit)],
        'inventory': inventory,
        'maintenance': _load_maintenance_report(data_root),
        'policy': {
            'schema_version': 1,
            'stream_count': len(policy_manifest()),
            'total_budget_bytes': total_log_budget_bytes(),
        },
        'action_hints': _action_hints(incidents, inventory),
        'evidence_paths': scan['paths'],
    }
    return _fit_report(report, output_limit)


def _cli_parser():
    """Build the offline operator CLI without affecting library imports."""
    import argparse

    from lib.runtime_paths import data_root, logs_root

    parser = argparse.ArgumentParser(
        description='Bounded, redacted, DB-independent Tofu log diagnosis')
    parser.add_argument('--log-dir', default=logs_root())
    parser.add_argument('--data-dir', default=data_root())
    parser.add_argument('--window-hours', type=float, default=24.0)
    parser.add_argument('--max-items', type=int, default=20)
    parser.add_argument('--max-bytes', type=int, default=32 * 1024)
    parser.add_argument('--scan-bytes', type=int, default=_DEFAULT_SCAN_BYTES)
    parser.add_argument('--request-id', default='')
    parser.add_argument('--conversation-id', default='')
    parser.add_argument('--task-id', default='')
    parser.add_argument('--trace-id', default='')
    parser.add_argument(
        '--user-id', default='',
        help='ownership filter for future multi-user deployments')
    parser.add_argument(
        '--maintenance', choices=('none', 'dry-run', 'apply'), default='none',
        help='optionally audit/apply the shared retention policy first')
    parser.add_argument('--pretty', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the supported offline CLI; mutation requires an explicit flag."""
    import sys

    from lib.log_retention import maintain_logs

    args = _cli_parser().parse_args(argv)
    maintenance = None
    if args.maintenance != 'none':
        maintenance = maintain_logs(
            args.log_dir, data_dir=args.data_dir,
            dry_run=args.maintenance == 'dry-run')
    report = diagnose_logs(
        args.log_dir,
        data_dir=args.data_dir,
        window_hours=args.window_hours,
        max_items=args.max_items,
        max_output_bytes=args.max_bytes,
        scan_bytes=args.scan_bytes,
        request_id=args.request_id,
        conversation_id=args.conversation_id,
        task_id=args.task_id,
        trace_id=args.trace_id,
        requesting_user_id=args.user_id,
        include_all_users=not bool(args.user_id),
    )
    if maintenance is not None:
        summary = {
            'mode': args.maintenance,
            'before_bytes': maintenance.get('before_bytes', 0),
            'after_bytes_estimate': maintenance.get('after_bytes_estimate', 0),
            'reclaimed_bytes_estimate': maintenance.get(
                'reclaimed_bytes_estimate', 0),
            'rotated_count': len(maintenance.get('rotated') or []),
            'compacted_count': len(maintenance.get('compacted') or []),
            'removed_count': len(maintenance.get('removed') or []),
            'permissions_hardened_count': len(
                maintenance.get('permissions_hardened') or []),
            'over_budget_bytes': maintenance.get('over_budget_bytes', 0),
            'errors': maintenance.get('errors') or [],
        }
        sys.stderr.write('maintenance=' + json.dumps(
            summary, ensure_ascii=False, separators=(',', ':')) + '\n')
    compact = json.dumps(
        report, ensure_ascii=False, separators=(',', ':'), default=str)
    rendered = (json.dumps(report, ensure_ascii=False, indent=2, default=str)
                if args.pretty else compact)
    output_limit = max(4 * 1024, min(256 * 1024, int(args.max_bytes)))
    if len((rendered + '\n').encode('utf-8')) > output_limit:
        rendered = compact
        if args.pretty:
            sys.stderr.write(
                'pretty output exceeded --max-bytes; emitted compact JSON\n')
    sys.stdout.write(rendered + '\n')
    return 2 if maintenance and maintenance.get('errors') else 0


__all__ = ['SCHEMA_VERSION', 'diagnose_logs', 'main']


if __name__ == '__main__':
    raise SystemExit(main())
