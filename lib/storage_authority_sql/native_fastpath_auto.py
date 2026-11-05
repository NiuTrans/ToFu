"""Automatic native fastpath selection, capacity governance and planned exit.

Responsibility: decide at every managed native daemon launch whether the
whole-authority fastpath (``docs/TOFUDB_FASTPATH.md``) should serve the
authority from a faster local volume, and explain the decision; keep an active
front's small local tier from filling silently; and turn a front that no longer
fits (or that the operator switched off) back into a classic authority before
the next launch, through the engine's lossless, verified ``fastpath-export``.
It also governs the other native tree automatic placement puts on a local
volume, the reconstructible search projection: a local rebuild is admitted
only with room for the measured projection, a copy whose volume turns
critical yields it at the next launch, and a yielded copy is reclaimed off the
launch path once no engine holds it.

The engine stays the admission authority. ``auto``/``on`` only nominate a base
through the shared placement policy (``serverctl_pkg.local_placement``, purpose
``native-authority``) and launch ``--fastpath-admission auto``, under which a
typed pre-relocation refusal serves the classic authority instead of failing
the launch. Corpus observation (the census) never runs on the launch path: the
supervisor refreshes it in the background and the next launch consumes it, so
an unmeasured authority is never relocated.

Entry points (``lib.storage.supervisor``): ``complete_planned_exit`` and
``plan_launch`` before one native spawn, ``record_launch`` after readiness and
``run_maintenance`` from the monitor; ``may_seed`` sizes the outer startup
budget; ``automatic_state`` feeds the database panel. Projection launch
preparation (``projection_paths``) calls ``search_projection_launch_decision``;
``search_projection_placement_state`` feeds the panel.

Dependencies: standard library, ``serverctl_pkg.local_placement``,
``serverctl_pkg.runtime_replica`` (lazy co-tenant relief), ``lib.json_store``, ``lib.log``,
``lib.storage.generated_v2_semantics`` and ``lib.storage.startup_budget``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading
import time
import uuid

from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger
from lib.storage.generated_v2_semantics import (
    MAX_NATIVE_FASTPATH_FSYNC_REGRESSION,
    MIN_NATIVE_FASTPATH_FSYNC_SPEEDUP,
    MIN_NATIVE_FASTPATH_METADATA_SPEEDUP,
)
from serverctl_pkg import local_placement

logger = get_logger(__name__)

STATE_FILE_NAME = 'native-fastpath-auto.json'
STATE_FORMAT = 'tofu.native-fastpath-auto/v1'
AUTHORITY_NAME = 'tofudb-authority'
FASTPATH_MARKER = 'FASTPATH'
PENDING_MARKER = 'FASTPATH_PENDING'
_MARKER_MAX_BYTES = 16384

# A census older than this no longer describes the corpus a launch relocates.
CENSUS_MAX_AGE_SECONDS = 24 * 3600.0
FRONT_CENSUS_MAX_AGE_SECONDS = 6 * 3600.0
MAINTENANCE_INTERVAL_SECONDS = 300.0
# Planned activation (two whole-corpus copies) may use at most this fraction of
# the bounded fastpath seed startup budget; the rest is estimation margin.
ACTIVATION_BUDGET_FRACTION = 0.5
# Sequential copy rate charged on top of per-file publication costs.
COPY_BYTES_PER_SECOND = 100 * 1024 * 1024
# Admission requires room for the corpus plus an equal growth allowance above
# the placement reserve, in bytes and in files. An active front is under
# pressure below half its size of headroom and critical below a fifth.
ADMISSION_GROWTH_RATIO = 1.0
INODE_RESERVE_FRACTION = 0.05
PRESSURE_HEADROOM_RATIO = 0.5
CRITICAL_HEADROOM_RATIO = 0.2
MIN_PRESSURE_HEADROOM_BYTES = 2 * 1024 ** 3
MIN_CRITICAL_HEADROOM_BYTES = 1024 ** 3
# After a capacity exit the tier is not re-selected for this long.
EXIT_COOLDOWN_SECONDS = 7 * 24 * 3600.0
# A failed export is retried at a later launch, never in a tight loop.
EXIT_RETRY_SECONDS = 6 * 3600.0
_DETAIL_LIMIT = 500
_CENSUS_BLOCK_DIRECTORIES = 4
_CENSUS_SAMPLED_FILES = 32
_DEFAULT_BLOCK_BYTES = 16 * 1024
_FSYNC_SAMPLES = 16
_PUBLISH_SAMPLES = 8

_STATE_LOCK = threading.Lock()
_EXIT_LOCK = threading.Lock()


@dataclass(frozen=True)
class LaunchPlan:
    """One explainable launch decision for the native authority."""

    mode: str
    selected: bool
    base: str
    reason: str
    detail: str = ''
    checks: tuple[dict[str, object], ...] = ()
    startup_timeout: float | None = None
    census_measured_at: float | None = None
    inputs: str = ''

    def environment(self) -> dict[str, str]:
        """Child environment additions; an explicit selection is untouched."""
        if not self.selected or self.mode == 'explicit':
            return {}
        return {'TOFU_DB_FASTPATH_DIR': self.base, 'TOFU_DB_FASTPATH_ADMISSION': 'auto'}

    def as_status(self) -> dict[str, object]:
        return {'mode': self.mode, 'selected': self.selected, 'base': self.base,
                'reason': self.reason, 'detail': self.detail, 'checks': list(self.checks)}


@dataclass
class _Checks:
    entries: list[dict[str, object]] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = '') -> bool:
        self.entries.append({'check': name, 'passed': passed, 'detail': detail[:_DETAIL_LIMIT]})
        return passed


_LAUNCH_INPUT_KEYS = (
    'TOFU_DB_FASTPATH', 'TOFU_LOCAL_DIR', 'XDG_STATE_HOME', 'HOME', 'TOFU_DB_DURABILITY',
    'TOFU_DB_WAL_FLUSH_INTERVAL_MS', 'TOFUDB_TRANSPORT', 'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S',
)


def _launch_inputs(environ: Mapping[str, str]) -> str:
    """Fingerprint of the settings a launch decision depends on."""
    values = [str(environ.get(key, '') or '') for key in _LAUNCH_INPUT_KEYS]
    return hashlib.sha256(json.dumps(values).encode('utf-8')).hexdigest()[:16]


def fastpath_mode(environ: Mapping[str, str]) -> str:
    """``explicit`` (operator base), else ``off``, ``auto`` or ``on``.

    ``TOFU_DB_FASTPATH`` is read here (and named by ``MODE_KNOBS``) so the
    knob registry sees both the literal read and the shared policy owner.
    """
    if str(environ.get('TOFU_DB_FASTPATH_DIR', '') or '').strip():
        return 'explicit'
    value = str(environ.get('TOFU_DB_FASTPATH', '') or '').strip().lower()
    if value in local_placement._OFF_VALUES:
        return 'off'
    if value in local_placement._ON_VALUES:
        return 'on'
    return local_placement.placement_mode(
        local_placement.PURPOSE_NATIVE_AUTHORITY, environ)


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / STATE_FILE_NAME


def load_state(data_dir: Path) -> dict:
    state = read_json(str(state_path(data_dir)), default=None, max_bytes=1 << 20)
    if not isinstance(state, dict) or state.get('format') != STATE_FORMAT:
        return {'format': STATE_FORMAT}
    return state


def _update_state(data_dir: Path, update: Callable[[dict], None]) -> dict:
    with _STATE_LOCK:
        state = load_state(data_dir)
        update(state)
        state['format'] = STATE_FORMAT
        write_json_atomic(str(state_path(data_dir)), state, mode=0o600)
        return state


def _record(state: Mapping[str, object], key: str) -> dict:
    value = state.get(key)
    return value if isinstance(value, dict) else {}


def read_marker(path: Path) -> dict | None:
    """Bounded no-follow read of a fastpath marker; ``None`` when unusable."""
    if not hasattr(os, 'O_NOFOLLOW'):
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        raw = os.read(descriptor, _MARKER_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(raw) > _MARKER_MAX_BYTES:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _marker_base(marker: dict | None) -> str:
    """The base recorded by a marker: ``<base>/tofudb-fastpath/<namespace>``."""
    local = str((marker or {}).get('local_authority') or '')
    if not local or not os.path.isabs(local):
        return ''
    return str(Path(local).parent.parent)


def _median(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[len(ordered) // 2]


def measure_fsync_us(directory: Path, samples: int = _FSYNC_SAMPLES) -> float | None:
    """Median 4 KiB write+fsync, the engine's commit-barrier probe."""
    path = Path(directory) / f'.tofu-fastpath-fsync-{uuid.uuid4().hex}'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return None
    timings = []
    try:
        for _ in range(samples):
            started = time.perf_counter()
            os.write(descriptor, b'\0' * 4096)
            os.fsync(descriptor)
            timings.append((time.perf_counter() - started) * 1e6)
    except OSError:
        return None
    finally:
        os.close(descriptor)
        try:
            os.unlink(path)
        except OSError:
            pass
    return _median(timings)


def measure_publish_us(directory: Path, samples: int = _PUBLISH_SAMPLES) -> float | None:
    """Median durable small-file publication (write, fsync, rename, dir sync).

    Native backup and restore publish every block this way, so the per-file
    cost bounds whole-corpus copy time on metadata-bound volumes.
    """
    directory = Path(directory)
    timings = []
    try:
        directory_descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return None
    try:
        for _ in range(samples):
            name = uuid.uuid4().hex
            temporary = directory / f'.tofu-fastpath-publish-{name}.tmp'
            final = directory / f'.tofu-fastpath-publish-{name}'
            started = time.perf_counter()
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, b'\0' * 4096)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, final)
            os.fsync(directory_descriptor)
            timings.append((time.perf_counter() - started) * 1e6)
            os.unlink(final)
    except OSError:
        return None
    finally:
        os.close(directory_descriptor)
    return _median(timings)


def engine_admits(durable_fsync_us: float, local_fsync_us: float,
                  durable_lookup_us: float, local_lookup_us: float) -> bool:
    """Mirror of the engine's ``CandidateProbe::admits`` (same contract constants)."""
    fsync_win = durable_fsync_us >= local_fsync_us * MIN_NATIVE_FASTPATH_FSYNC_SPEEDUP
    metadata_win = (durable_lookup_us >= local_lookup_us * MIN_NATIVE_FASTPATH_METADATA_SPEEDUP
                    and local_fsync_us <= durable_fsync_us * MAX_NATIVE_FASTPATH_FSYNC_REGRESSION)
    return fsync_win or metadata_win


def take_census(authority: Path, *, now: float | None = None) -> dict:
    """Bounded estimate of the corpus a relocation copies, without a full walk.

    Samples a few loose-block shards and payload segments; walking millions of
    files on a network volume takes hours. Native backup/restore republish
    every reachable block as one file, so packed bytes count as blocks of the
    sampled loose size (an over-estimate when packed blocks are larger).
    """
    started = time.monotonic()
    authority = Path(authority)
    blocks_root = authority / 'blocks'
    shards: list[str] = []
    if blocks_root.is_dir():
        with os.scandir(blocks_root) as entries:
            shards = sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
    sampled_shards = shards[::max(1, len(shards) // _CENSUS_BLOCK_DIRECTORIES)][:_CENSUS_BLOCK_DIRECTORIES]
    shard_counts, block_sizes = [], []
    for name in sampled_shards:
        count = 0
        with os.scandir(blocks_root / name) as entries:
            for entry in entries:
                if entry.name.startswith('.'):
                    continue
                count += 1
                if len(block_sizes) < _CENSUS_SAMPLED_FILES:
                    try:
                        block_sizes.append(entry.stat(follow_symlinks=False).st_size)
                    except OSError:
                        pass
        shard_counts.append(count)
    loose_files = round(sum(shard_counts) / len(shard_counts) * len(shards)) if shard_counts else 0
    segment_root = authority / 'payload-segments'
    segment_names: list[str] = []
    if segment_root.is_dir():
        with os.scandir(segment_root) as entries:
            segment_names = [entry.name for entry in entries if entry.name.endswith('.pseg')]
    segment_sizes = []
    for name in segment_names[::max(1, len(segment_names) // _CENSUS_SAMPLED_FILES)][:_CENSUS_SAMPLED_FILES]:
        try:
            segment_sizes.append((segment_root / name).stat().st_size)
        except OSError:
            pass
    segment_bytes = (round(sum(segment_sizes) / len(segment_sizes) * len(segment_names))
                     if segment_sizes else 0)
    block_bytes = (sum(block_sizes) / len(block_sizes)) if block_sizes else _DEFAULT_BLOCK_BYTES
    root_bytes = 0
    if authority.is_dir():
        with os.scandir(authority) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    try:
                        root_bytes += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
    packed_blocks = round(segment_bytes / max(1.0, block_bytes))
    return {
        'authority': str(authority),
        'measuredAt': time.time() if now is None else now,
        'looseBlockFiles': loose_files,
        'payloadSegmentFiles': len(segment_names),
        'estimatedBlocks': loose_files + packed_blocks,
        'estimatedBytes': round(loose_files * block_bytes) + segment_bytes + root_bytes,
        'sampledShards': len(sampled_shards),
        'elapsedSeconds': round(time.monotonic() - started, 3),
    }


def activation_seconds(census: Mapping[str, object], source_publish_us: float,
                       target_publish_us: float) -> float:
    """Planned cost of the two whole-corpus copies of a first activation."""
    blocks = int(census.get('estimatedBlocks') or 0)
    corpus_bytes = int(census.get('estimatedBytes') or 0)
    per_file = (source_publish_us + target_publish_us) / 1e6
    return blocks * per_file + 2 * corpus_bytes / COPY_BYTES_PER_SECOND


def seed_startup_timeout(environ: Mapping[str, str]) -> float:
    from lib.storage.startup_budget import fastpath_seed_startup_timeout

    return fastpath_seed_startup_timeout(environ)


def _fresh(record: object, subject: str, now: float, max_age: float) -> dict | None:
    if (not isinstance(record, dict) or record.get('authority') != subject
            or not isinstance(record.get('measuredAt'), (int, float))
            or now - float(record['measuredAt']) > max_age):
        return None
    return record


def _inode_check(base: Path, required: int,
                 statvfs: Callable[[str], os.statvfs_result]) -> tuple[bool, str]:
    """Whether ``required`` more files fit above the inode reserve."""
    try:
        observed = statvfs(str(base))
    except OSError as exc:
        return False, f'unobserved: {exc}'
    if not observed.f_files:
        return True, 'filesystem reports no inode limit'
    reserve = int(observed.f_files * INODE_RESERVE_FRACTION)
    passed = observed.f_favail - required >= reserve
    return passed, (f'{observed.f_favail} free inodes for ~{required} block files '
                    f'(reserve {reserve})')


def plan_launch(
    data_dir: Path,
    environ: Mapping[str, str],
    *,
    transport: str,
    projection_directory: str = '',
    now: float | None = None,
    placement: Callable[..., local_placement.Placement] = local_placement.choose_local_root,
    fsync_probe: Callable[[Path], float | None] = measure_fsync_us,
    publish_probe: Callable[[Path], float | None] = measure_publish_us,
    lookup_probe: Callable[[str], float | None] = local_placement.measure_lookup_latency,
    statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
) -> LaunchPlan:
    """Decide this launch's native fastpath selection from cheap observations."""
    now = time.time() if now is None else now
    data_dir = Path(data_dir)
    authority = data_dir / AUTHORITY_NAME
    mode = fastpath_mode(environ)
    checks = _Checks()
    inputs = _launch_inputs(environ)
    census_at: list[float | None] = [None]

    def plan(selected: bool, base: str, reason: str, detail: str = '') -> LaunchPlan:
        return LaunchPlan(mode, selected, base, reason, detail[:_DETAIL_LIMIT],
                          tuple(checks.entries),
                          seed_startup_timeout(environ) if selected else None,
                          census_at[0], inputs)

    def decline(reason: str, detail: str = '') -> LaunchPlan:
        return plan(False, '', reason, detail)

    if mode == 'explicit':
        return decline('explicit', 'TOFU_DB_FASTPATH_DIR selects the base; engine admission is required')
    if os.path.lexists(authority / FASTPATH_MARKER):
        # The fence is irreversible until a completed export: only the recorded
        # base may serve, whatever the mode, or the authority stays down.
        base = _marker_base(read_marker(authority / FASTPATH_MARKER))
        if not base:
            return decline('fenced-unreadable',
                           'the relocation fence is unreadable; recover it with the documented operator path')
        checks.add('relocation-fence', True, base)
        detail = 'resuming the recorded local front'
        if mode == 'off':
            detail += '; TOFU_DB_FASTPATH=off applies after a completed planned exit'
        return plan(True, base, 'fenced-resume', detail)
    if mode == 'off':
        return decline('disabled', 'TOFU_DB_FASTPATH=off')
    state = load_state(data_dir)
    if os.path.lexists(authority / PENDING_MARKER):
        base = _marker_base(read_marker(authority / PENDING_MARKER))
        last = _record(state, 'lastLaunch')
        if not base or (last.get('base') == base and last.get('outcome') == 'engine-declined'):
            return decline('preparation-blocked',
                           'an interrupted preparation no longer matches its source; review and '
                           'retire FASTPATH_PENDING and its shadow (docs/TOFUDB_FASTPATH.md)')
        checks.add('pending-preparation', True, base)
        return plan(True, base, 'preparation-resume', 'resuming an interrupted seed')
    if not checks.add('daemon-transport', transport == 'daemon', transport):
        return decline('embedded-transport', 'shadow replication needs the daemon transport')
    durability = str(environ.get('TOFU_DB_DURABILITY', '') or '').strip().lower()
    strict = (durability in ('', 'strict')
              and not str(environ.get('TOFU_DB_WAL_FLUSH_INTERVAL_MS', '') or '').strip())
    if not checks.add('strict-durability', strict, durability or 'automatic'):
        return decline('durability-override', 'an explicit Normal durability tier stays classic')
    if not checks.add('online-replication', 'TOFU_DB_DISABLE_ONLINE_BACKGROUND' not in environ):
        return decline('online-background-disabled', 'the shadow needs the online backup worker')
    source = local_placement.describe_volume(authority)
    if not checks.add('remote-source', source.storage_class in local_placement.REMOTE_CLASSES,
                      f'{source.filesystem_type} ({source.storage_class})'):
        return decline('source-local', 'the authority already lives on a local volume')
    census = _fresh(state.get('census'), str(authority), now, CENSUS_MAX_AGE_SECONDS)
    if not checks.add('census', census is not None):
        return decline('census-pending', 'the corpus has not been measured yet; the running '
                       'server measures it in the background for the next launch')
    census_at[0] = float(census['measuredAt'])
    exit_record = _record(state, 'exit')
    completed_at = exit_record.get('completedAt')
    if (exit_record.get('reason') == 'capacity' and isinstance(completed_at, (int, float))
            and now - completed_at < EXIT_COOLDOWN_SECONDS):
        checks.add('exit-cooldown', False, f'capacity exit at {completed_at:.0f}')
        return decline('capacity-exit-cooldown', 'the last front left this tier for capacity')
    corpus_bytes = int(census.get('estimatedBytes') or 0)
    corpus_blocks = int(census.get('estimatedBlocks') or 0)
    chosen = placement(authority, local_placement.PURPOSE_NATIVE_AUTHORITY, environ=environ,
                       required_bytes=int(corpus_bytes * (1 + ADMISSION_GROWTH_RATIO)))
    if not checks.add('placement', chosen.selected, json.dumps(chosen.as_status(), sort_keys=True)):
        return decline(f'placement-{chosen.reason}', _placement_detail(chosen, corpus_bytes))
    base = Path(chosen.root)
    if projection_directory:
        projection = Path(projection_directory)
        overlap = projection.is_relative_to(base) or base.is_relative_to(projection)
        if not checks.add('projection-disjoint', not overlap, str(projection)):
            return decline('projection-overlap', 'the search projection must stay outside the front')
    inodes_ok, inode_detail = _inode_check(
        base, int(corpus_blocks * (1 + ADMISSION_GROWTH_RATIO)), statvfs)
    if not checks.add('inodes', inodes_ok, inode_detail):
        return decline('inode-capacity', f'the local tier cannot hold the corpus as files: {inode_detail}')
    source_publish = census.get('sourcePublishMicros')
    target_publish = publish_probe(base)
    if not isinstance(source_publish, (int, float)) or target_publish is None:
        checks.add('activation-cost', False, 'publication cost unmeasured')
        return decline('activation-unmeasured', 'per-file publication cost could not be measured')
    seconds = activation_seconds(census, float(source_publish), target_publish)
    budget = seed_startup_timeout(environ) * ACTIVATION_BUDGET_FRACTION
    if not checks.add('activation-cost', seconds <= budget,
                      f'{seconds:.0f}s for ~{corpus_blocks} blocks / {corpus_bytes} bytes; '
                      f'budget {budget:.0f}s'):
        return decline('activation-too-costly',
                       f'first activation copies ~{corpus_blocks} block files twice '
                       f'(~{seconds / 3600:.1f} h); the seed budget allows {budget:.0f}s')
    if mode == 'on':
        checks.add('speedup', True, 'operator consent (TOFU_DB_FASTPATH=on); the engine probe decides')
        return plan(True, str(base), 'selected', 'operator consent')
    durable_fsync, local_fsync = fsync_probe(authority), fsync_probe(base)
    durable_lookup = chosen.source_latency_us or lookup_probe(str(authority))
    local_lookup = chosen.target_latency_us or lookup_probe(str(base))
    if None in (durable_fsync, local_fsync, durable_lookup, local_lookup):
        checks.add('speedup', False, 'unmeasured')
        return decline('speedup-unmeasured', 'fsync or lookup latency could not be measured')
    detail = (f'fsync {durable_fsync:.0f}->{local_fsync:.0f}us, '
              f'lookup {durable_lookup:.0f}->{local_lookup:.0f}us')
    if not checks.add('speedup', engine_admits(durable_fsync, local_fsync, durable_lookup,
                                               local_lookup), detail):
        return decline('speedup-insufficient', detail)
    return plan(True, str(base), 'selected', detail)


def _placement_detail(placement: local_placement.Placement, corpus_bytes: int) -> str:
    reasons = {
        'candidate-persistence-unknown': (
            'the only local candidate is a container overlay whose persistence the mount table '
            'cannot prove; TOFU_DB_FASTPATH=on accepts its loss window, or point TOFU_LOCAL_DIR '
            'at a persistent local disk'),
        'candidate-persistence-ephemeral': 'temporary or memory-backed candidates cannot hold commits',
        'insufficient-space': (f'the local tier cannot hold the ~{corpus_bytes} byte corpus plus '
                               'equal growth room above its free-space reserve'),
        'insufficient-speedup': 'local metadata lookups are not decisively faster',
        'candidate-on-source-volume': 'the candidate shares the authority volume',
        'candidate-not-local': 'the candidate root is not on a local volume',
        'no-candidate': 'no candidate root is configured',
    }
    return reasons.get(placement.reason, placement.reason)


def record_launch(data_dir: Path, plan: LaunchPlan, witness: object,
                  declined: object = None, *, now: float | None = None) -> dict:
    """Persist this launch's decision and the engine's actual outcome."""
    now = time.time() if now is None else now
    declined = declined if isinstance(declined, dict) else {}
    if isinstance(witness, dict):
        outcome = 'active'
    elif plan.selected:
        outcome = 'engine-declined'
    else:
        outcome = 'classic'
    record = {
        'at': now, 'mode': plan.mode, 'reason': plan.reason, 'detail': plan.detail,
        'base': plan.base, 'outcome': outcome,
        'engineReason': str(declined.get('reason') or '')[:80],
        'engineDetail': str(declined.get('detail') or '')[:_DETAIL_LIMIT],
        'censusMeasuredAt': plan.census_measured_at, 'inputs': plan.inputs,
        'checks': list(plan.checks),
    }
    if outcome == 'engine-declined':
        logger.warning('Native fastpath was nominated at %s but the engine declined (%s: %s); '
                       'serving the classic authority', plan.base, record['engineReason'],
                       record['engineDetail'])
    elif outcome == 'active' and plan.reason == 'selected':
        logger.warning('Native fastpath activated automatically at %s (%s)', plan.base, plan.detail)
    else:
        logger.info('Native fastpath %s: %s %s', outcome, plan.reason, plan.detail)

    def update(state: dict) -> None:
        state['lastLaunch'] = record
        authority = Path(data_dir) / AUTHORITY_NAME
        if outcome == 'active':
            front = str(witness.get('localAuthority') or '')
            previous = _record(state, 'activeFront')
            same = previous.get('localAuthority') == front
            if same and previous.get('origin'):
                origin = previous['origin']
            elif plan.reason in ('selected', 'preparation-resume'):
                origin = 'automatic'
            else:
                origin = 'explicit' if plan.mode == 'explicit' else 'unknown'
            state['activeFront'] = {
                'localAuthority': front,
                'shadowDirectory': str(witness.get('shadowDirectory') or ''),
                'origin': origin, 'since': previous.get('since', now) if same else now,
            }
        elif not os.path.lexists(authority / FASTPATH_MARKER):
            state.pop('activeFront', None)

    return _update_state(data_dir, update)


def refresh_census(data_dir: Path, *, now: float | None = None) -> dict:
    """Measure the classic corpus and the durable volume's publication cost."""
    data_dir = Path(data_dir)
    census = take_census(data_dir / AUTHORITY_NAME, now=now)
    census['sourcePublishMicros'] = measure_publish_us(data_dir)
    return _update_state(data_dir, lambda state: state.__setitem__('census', census))['census']


def needs_census(data_dir: Path, environ: Mapping[str, str], *, now: float | None = None) -> bool:
    """Whether a background census would inform the next launch decision."""
    now = time.time() if now is None else now
    authority = Path(data_dir) / AUTHORITY_NAME
    if fastpath_mode(environ) not in ('auto', 'on') or os.path.lexists(authority / FASTPATH_MARKER):
        return False
    if local_placement.describe_volume(authority).storage_class not in local_placement.REMOTE_CLASSES:
        return False
    return _fresh(load_state(data_dir).get('census'), str(authority), now,
                  CENSUS_MAX_AGE_SECONDS) is None


def headroom_status(front: Path, front_bytes: int, front_files: int = 0, *,
                    disk_usage: Callable[[str], object] = shutil.disk_usage,
                    statvfs: Callable[[str], os.statvfs_result] = os.statvfs) -> dict:
    """Classify an active front's remaining growth room (bytes and files)."""
    try:
        usage = disk_usage(str(front))
    except OSError as exc:
        return {'state': 'unobserved', 'detail': str(exc)[:_DETAIL_LIMIT]}
    total, free = int(usage.total), int(usage.free)
    headroom = free - local_placement.reserve_bytes(total)
    pressure = max(MIN_PRESSURE_HEADROOM_BYTES, int(front_bytes * PRESSURE_HEADROOM_RATIO))
    critical = max(MIN_CRITICAL_HEADROOM_BYTES, int(front_bytes * CRITICAL_HEADROOM_RATIO))
    state = 'critical' if headroom < critical else 'pressure' if headroom < pressure else 'ok'
    status = {'freeBytes': free, 'totalBytes': total, 'headroomBytes': headroom,
              'frontBytes': front_bytes, 'pressureBelowBytes': pressure,
              'criticalBelowBytes': critical}
    try:
        files = statvfs(str(front))
    except OSError:
        files = None
    if files is not None and files.f_files:
        inode_headroom = files.f_favail - int(files.f_files * INODE_RESERVE_FRACTION)
        status.update(inodesFree=files.f_favail, inodeHeadroom=inode_headroom)
        if inode_headroom < max(1, int(front_files * CRITICAL_HEADROOM_RATIO)):
            state = 'critical'
        elif state == 'ok' and inode_headroom < max(1, int(front_files * PRESSURE_HEADROOM_RATIO)):
            state = 'pressure'
    status['state'] = state
    return status


def _relieve_co_tenants(front: Path) -> dict[str, int]:
    """Retire idle reconstructible runtime replicas sharing the front's volume."""
    from serverctl_pkg import runtime_replica

    relief: dict[str, int] = {}
    try:
        device = os.stat(front).st_dev
    except OSError:
        return relief
    for root in local_placement.candidate_roots(local_placement.PURPOSE_RUNTIME):
        try:
            if not root.is_dir() or os.stat(root).st_dev != device:
                continue
            # In-use generations always survive; everything else is a
            # reconstructible cache the front's growth outranks.
            result = runtime_replica.collect_idle_registries(root, everything=True)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning('Runtime replica relief under %s failed: %s', root, exc)
            continue
        for name, value in result.items():
            relief[name] = relief.get(name, 0) + int(value)
    return relief


def govern(data_dir: Path, witness: object, environ: Mapping[str, str], *,
           now: float | None = None,
           disk_usage: Callable[[str], object] = shutil.disk_usage,
           statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
           relieve: Callable[[Path], dict[str, int]] = _relieve_co_tenants) -> dict:
    """One capacity-governance pass for an active front.

    Pressure first reclaims reconstructible co-tenants on the same volume
    (runtime replica generations nothing is using); a front that stays
    critical requests the planned exit,
    which the next launch performs before serving. User work is never refused
    and the front is never deleted while it is the live authority.
    """
    now = time.time() if now is None else now
    if not isinstance(witness, dict) or not witness.get('localAuthority'):
        return {'state': 'inactive'}
    front = Path(str(witness['localAuthority']))
    state = load_state(data_dir)
    sizing = _record(state, 'frontCensus') or _record(state, 'census')
    front_bytes = int(sizing.get('estimatedBytes') or 0)
    front_files = int(sizing.get('estimatedBlocks') or 0)

    def observe() -> dict:
        return headroom_status(front, front_bytes, front_files, disk_usage=disk_usage, statvfs=statvfs)

    status = observe()
    if status['state'] in ('pressure', 'critical'):
        relief = relieve(front)
        status = {**observe(), 'relief': relief}
    mode = fastpath_mode(environ)
    exit_requested = status['state'] == 'critical' and mode in ('auto', 'on')
    previous = _record(state, 'capacity')
    if status['state'] != previous.get('state'):
        log = (logger.error if status['state'] == 'critical'
               else logger.warning if status['state'] == 'pressure' else logger.info)
        log('Native fastpath front %s capacity %s (headroom %s bytes, %s inodes) for a ~%s-byte '
            'front%s', front, status['state'], status.get('headroomBytes'),
            status.get('inodeHeadroom'), front_bytes,
            '; the next restart exports it back to the durable volume' if exit_requested else '')

    def update(record: dict) -> None:
        record['capacity'] = {**status, 'at': now}
        if exit_requested and not _record(record, 'exitRequested'):
            record['exitRequested'] = {'at': now, 'reason': 'capacity', 'front': str(front)}

    _update_state(data_dir, update)
    return status


# Search-projection placement governance. The index fastpath
# (docs/INDEX_FASTPATH.md) places the reconstructible native search projection
# on a local volume, where native reconstruction rebuilds it from the
# authority: a large corpus rebuilds into millions of loose block files. A new
# local copy is admitted only for the measured data-volume projection plus
# equal growth room, a copy whose volume turns critical yields it at the next
# launch, and a yielded copy is reclaimed off the launch path once no engine
# holds it. These are the census, growth room and headroom rules of a front.
PROJECTION_LAUNCH_RECORD = 'searchProjection'
PROJECTION_CENSUS_RECORD = 'searchProjectionCensus'
PROJECTION_CAPACITY_RECORD = 'searchProjectionCapacity'
PROJECTION_RECLAIM_RECORD = 'searchProjectionReclaim'
_PROJECTION_NAMESPACE = 'tofudb-search'
_RETIRED_MARK = '.retired-'
_HEX = frozenset('0123456789abcdef')
# Serializes a launch's decide-and-record with the reclaim's check-and-rename,
# so a copy a launch has just selected again is never renamed under it.
_PROJECTION_LOCK = threading.Lock()
_projection_removal_lock = threading.Lock()
_projection_removal: threading.Thread | None = None


def _is_directory(path: Path) -> bool:
    return os.path.isdir(path) and not os.path.islink(path)


def _existing_ancestor(path: Path) -> Path:
    while not os.path.exists(path) and path.parent != path:
        path = path.parent
    return path


def _copy_files(census: Mapping[str, object]) -> int:
    """Inodes a native tree occupies; packed blocks share their segment file."""
    return int(census.get('looseBlockFiles') or 0) + int(census.get('payloadSegmentFiles') or 0)


def _capacity_detail(status: Mapping[str, object], files: int, size: int) -> str:
    return (f"{status.get('inodeHeadroom', 'unreported')} spare inodes and "
            f"{status.get('headroomBytes')} spare bytes above the reserve for a "
            f"~{files}-file, ~{size}-byte copy")[:_DETAIL_LIMIT]


def _projection_admission(base: Path, census: Mapping[str, object], *,
                          disk_usage: Callable[[str], object],
                          statvfs: Callable[[str], os.statvfs_result]) -> dict:
    """Room for a rebuild, which materializes blocks as loose files first."""
    blocks = int(census.get('estimatedBlocks') or 0)
    files = int(blocks * (1 + ADMISSION_GROWTH_RATIO))
    size = int(int(census.get('estimatedBytes') or 0) * (1 + ADMISSION_GROWTH_RATIO))
    probe = _existing_ancestor(base)
    space = local_placement.has_free_space(probe, size, disk_usage=disk_usage)
    inodes, inode_detail = _inode_check(probe, files, statvfs)
    selected = space and inodes
    detail = (f'rebuilding the ~{blocks}-block data-volume projection with equal growth room '
              f'needs ~{size} bytes ({"available" if space else "not available"}); '
              f'{inode_detail}')
    return {'selected': selected, 'reason': 'selected' if selected else 'insufficient-capacity',
            'detail': detail[:_DETAIL_LIMIT]}


def _store_projection_census(state: dict, census: Mapping[str, object],
                             keep: tuple[str, ...]) -> None:
    censuses = {path: record for path, record in _record(state, PROJECTION_CENSUS_RECORD).items()
                if path in keep}
    censuses[str(census['authority'])] = dict(census)
    state[PROJECTION_CENSUS_RECORD] = censuses


def _recorded_census(censuses: Mapping[str, object], directory: Path) -> dict:
    """Latest measurement of ``directory``, however old: the best size estimate."""
    record = censuses.get(str(directory))
    return record if isinstance(record, dict) and record.get('authority') == str(directory) else {}


def _record_projection_launch(data_dir: Path, decision: Mapping[str, object]) -> None:
    candidate, legacy = str(decision['candidate']), str(decision['legacy'])

    def update(state: dict) -> None:
        state[PROJECTION_LAUNCH_RECORD] = {
            key: decision[key] for key in ('at', 'candidate', 'legacy', 'selected', 'reason', 'detail')}
        reclaim = dict(_record(state, PROJECTION_RECLAIM_RECORD))
        # A copy this launch serves again is never reclaimed.
        pending = [path for path in reclaim.get('pending') or []
                   if isinstance(path, str) and path != candidate]
        if decision['reason'] == 'capacity-critical':
            pending.append(candidate)
        reclaim['pending'] = pending
        state[PROJECTION_RECLAIM_RECORD] = reclaim
        if not decision['selected']:
            state.pop(PROJECTION_CAPACITY_RECORD, None)

    _update_state(data_dir, update)
    if decision['reason'] == 'capacity-critical':
        logger.warning('Search projection copy %s no longer fits its local volume (%s); serving %s '
                       'and reclaiming the copy off the launch path', candidate,
                       decision['detail'], legacy)
    elif not decision['selected']:
        logger.info('Search projection stays at %s (%s: %s)', legacy, decision['reason'],
                    decision['detail'])


def search_projection_launch_decision(
        data_dir: Path, candidate: Path, legacy: Path, *, launch: bool,
        now: float | None = None,
        disk_usage: Callable[[str], object] | None = None,
        statvfs: Callable[[str], os.statvfs_result] | None = None) -> dict:
    """Whether the automatic local copy may serve the projection at a launch.

    An existing copy keeps serving unless its volume is capacity critical for
    it, sized by its latest background measurement (unmeasured, only a volume
    already below the reserve is critical). A new copy needs room for the
    measured data-volume projection (``census-pending`` until the monitor has
    measured it); with no projection anywhere the local copy starts empty.
    Nothing is measured on the launch path. ``launch=True`` records the
    decision and queues a yielded copy for reclaim; ``launch=False`` predicts
    it from persisted observations without writing.
    """
    if not launch:
        return _projection_decision(data_dir, candidate, legacy, now=now,
                                    disk_usage=disk_usage, statvfs=statvfs)
    with _PROJECTION_LOCK:
        decision = _projection_decision(data_dir, candidate, legacy, now=now,
                                        disk_usage=disk_usage, statvfs=statvfs)
        _record_projection_launch(Path(data_dir), decision)
    return decision


def _projection_decision(data_dir: Path, candidate: Path, legacy: Path, *, now: float | None,
                         disk_usage: Callable[[str], object] | None,
                         statvfs: Callable[[str], os.statvfs_result] | None) -> dict:
    now = time.time() if now is None else now
    disk_usage = shutil.disk_usage if disk_usage is None else disk_usage
    statvfs = os.statvfs if statvfs is None else statvfs
    data_dir, candidate, legacy = Path(data_dir), Path(candidate), Path(legacy)
    censuses = _record(load_state(data_dir), PROJECTION_CENSUS_RECORD)
    if _is_directory(candidate):
        census = _recorded_census(censuses, candidate)
        files, size = _copy_files(census), int(census.get('estimatedBytes') or 0)
        status = headroom_status(candidate, size, files, disk_usage=disk_usage, statvfs=statvfs)
        critical = status['state'] == 'critical'
        decision = {'selected': not critical,
                    'reason': 'capacity-critical' if critical else 'selected',
                    'detail': _capacity_detail(status, files, size)}
    elif _is_directory(legacy):
        census = _fresh(censuses.get(str(legacy)), str(legacy), now, CENSUS_MAX_AGE_SECONDS)
        if census is None:
            decision = {'selected': False, 'reason': 'census-pending',
                        'detail': 'the data-volume projection is measured in the background '
                                  'before a local rebuild is admitted'}
        else:
            decision = _projection_admission(candidate.parent, census,
                                             disk_usage=disk_usage, statvfs=statvfs)
    else:
        decision = {'selected': True, 'reason': 'selected',
                    'detail': 'no projection exists yet; the local copy starts empty'}
    decision.update(at=now, candidate=str(candidate), legacy=str(legacy))
    return decision


def _owned_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    getuid = getattr(os, 'getuid', None)
    return stat.S_ISDIR(metadata.st_mode) and (getuid is None or metadata.st_uid == getuid())


def _retired_copy_name(name: str, identity: str) -> bool:
    prefix = identity + _RETIRED_MARK
    suffix = name[len(prefix):]
    return name.startswith(prefix) and len(suffix) == 32 and set(suffix) <= _HEX


def _retire_projection_copy(copy: Path) -> Path | None:
    """Rename an unused copy out of its namespace; ``None`` while an engine holds it."""
    try:
        import fcntl
    except ImportError:
        return None
    try:
        descriptor = os.open(copy / 'LOCK', os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        descriptor = None
    except OSError:
        return None
    try:
        if descriptor is not None:
            try:
                # The native engine holds this flock for as long as it is open.
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return None
        retired = copy.with_name(f'{copy.name}{_RETIRED_MARK}{uuid.uuid4().hex}')
        os.rename(copy, retired)
        return retired
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _start_projection_removal(retired: list[Path]) -> None:
    """One background remover per process; leftovers are found again by name."""
    global _projection_removal
    with _projection_removal_lock:
        if _projection_removal is not None and _projection_removal.is_alive():
            return

        def remove() -> None:
            for path in retired:
                started = time.monotonic()
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    logger.warning('Removing the retired search projection copy %s failed: %s',
                                   path, exc)
                    continue
                logger.info('Removed the retired search projection copy %s in %.1f s', path,
                            time.monotonic() - started)

        _projection_removal = threading.Thread(target=remove, name='search-projection-reclaim',
                                               daemon=True)
        _projection_removal.start()


def _reclaim_projection_copies(data_dir: Path, candidate: Path, active: str, now: float,
                               remove: Callable[[list[Path]], object]) -> dict:
    """Retire yielded copies of this authority's namespace, then remove them."""
    from lib.storage_authority_sql.projection_paths import namespace_identity

    identity = namespace_identity((Path(data_dir).resolve() / AUTHORITY_NAME).resolve())
    namespace = candidate.parent
    waiting: list[str] = []
    outcomes: list[dict] = []
    retired: list[Path] = []
    with _PROJECTION_LOCK:
        state = load_state(data_dir)
        serving = _record(state, PROJECTION_LAUNCH_RECORD)
        pending = [path for path in _record(state, PROJECTION_RECLAIM_RECORD).get('pending') or []
                   if isinstance(path, str)]
        for text in dict.fromkeys(pending):
            copy = Path(text)
            if (text == active or (serving.get('selected') and text == serving.get('candidate'))
                    or namespace.name != _PROJECTION_NAMESPACE
                    or copy.parent != namespace or copy.name != identity
                    or not _owned_directory(copy)):
                outcome = 'refused' if os.path.lexists(copy) else 'absent'
            else:
                moved = _retire_projection_copy(copy)
                if moved is None:
                    waiting.append(text)
                    outcome = 'in-use'
                else:
                    retired.append(moved)
                    outcome = 'retired'
            outcomes.append({'directory': text, 'outcome': outcome})
        if outcomes:
            def update(record: dict) -> None:
                record[PROJECTION_RECLAIM_RECORD] = {
                    'pending': waiting, 'last': {'at': now, 'outcomes': outcomes}}

            _update_state(data_dir, update)
    if namespace.name == _PROJECTION_NAMESPACE and _owned_directory(namespace):
        with os.scandir(namespace) as entries:
            for entry in entries:
                if _retired_copy_name(entry.name, identity) and _owned_directory(Path(entry.path)):
                    retired.append(Path(entry.path))
    retired = list(dict.fromkeys(retired))
    if retired:
        remove(retired)
    if not retired and not outcomes:
        return {}
    return {'retired': [str(path) for path in retired], 'waiting': waiting}


def _govern_projection_copy(data_dir: Path, copy: Path, census: Mapping[str, object], *,
                            now: float, disk_usage: Callable[[str], object],
                            statvfs: Callable[[str], os.statvfs_result],
                            relieve: Callable[[Path], dict[str, int]]) -> dict:
    files, size = _copy_files(census), int(census.get('estimatedBytes') or 0)

    def observe() -> dict:
        return headroom_status(copy, size, files, disk_usage=disk_usage, statvfs=statvfs)

    status = observe()
    if status['state'] in ('pressure', 'critical'):
        relief = relieve(copy)
        status = {**observe(), 'relief': relief}
    previous = _record(load_state(data_dir), PROJECTION_CAPACITY_RECORD)
    if status['state'] != previous.get('state'):
        log = (logger.error if status['state'] == 'critical'
               else logger.warning if status['state'] == 'pressure' else logger.info)
        log('Search projection copy %s capacity %s (%s)%s', copy, status['state'],
            _capacity_detail(status, files, size),
            '; the next storage launch serves the data-volume projection and reclaims this copy'
            if status['state'] == 'critical' else '')

    def update(record: dict) -> None:
        record[PROJECTION_CAPACITY_RECORD] = {**status, 'at': now, 'directory': str(copy)}

    _update_state(data_dir, update)
    return status


def maintain_search_projection(
        data_dir: Path, projection_directory: str, *, now: float | None = None,
        disk_usage: Callable[[str], object] | None = None,
        statvfs: Callable[[str], os.statvfs_result] | None = None,
        relieve: Callable[[Path], dict[str, int]] = _relieve_co_tenants,
        remove: Callable[[list[Path]], object] | None = None) -> dict:
    """Reclaim yielded copies, then measure and govern the serving projection.

    A serving local copy keeps a fresh census and a headroom classification;
    critical is acted on by the next launch, never by moving a live index. A
    serving data-volume projection is measured so the next launch can decide
    whether a local rebuild fits.
    """
    now = time.time() if now is None else now
    disk_usage = shutil.disk_usage if disk_usage is None else disk_usage
    statvfs = os.statvfs if statvfs is None else statvfs
    data_dir = Path(data_dir)
    state = load_state(data_dir)
    launch = _record(state, PROJECTION_LAUNCH_RECORD)
    candidate, legacy = str(launch.get('candidate') or ''), str(launch.get('legacy') or '')
    if not candidate:
        return {}
    active = str(projection_directory or '')
    result: dict = {}
    reclaim = _reclaim_projection_copies(data_dir, Path(candidate), active, now,
                                         remove or _start_projection_removal)
    if reclaim:
        result['reclaim'] = reclaim
    censuses = _record(state, PROJECTION_CENSUS_RECORD)
    if active and active == candidate and launch.get('selected'):
        copy = Path(active)
        census = _fresh(censuses.get(active), active, now, FRONT_CENSUS_MAX_AGE_SECONDS)
        if census is None:
            census = take_census(copy, now=now)
            _update_state(data_dir, lambda record: _store_projection_census(
                record, census, (candidate, legacy)))
        result['capacity'] = _govern_projection_copy(
            data_dir, copy, census, now=now, disk_usage=disk_usage, statvfs=statvfs,
            relieve=relieve)
    elif active and active == legacy and not launch.get('selected'):
        if _fresh(censuses.get(legacy), legacy, now, CENSUS_MAX_AGE_SECONDS) is None:
            census = take_census(Path(legacy), now=now)
            _update_state(data_dir, lambda record: _store_projection_census(
                record, census, (candidate, legacy)))
            result['census'] = census
    return result


def search_projection_placement_state(data_dir: Path) -> dict:
    """Panel projection: last placement decision, serving capacity and reclaim."""
    state = load_state(data_dir)
    return {
        'lastLaunch': state.get(PROJECTION_LAUNCH_RECORD),
        'capacity': state.get(PROJECTION_CAPACITY_RECORD),
        'reclaim': _record(state, PROJECTION_RECLAIM_RECORD),
    }


def run_maintenance(data_dir: Path, environ: Mapping[str, str], witness: object, *,
                    projection_directory: str = '', now: float | None = None) -> dict:
    """Background pass from the supervisor monitor; never on the launch path."""
    now = time.time() if now is None else now
    data_dir = Path(data_dir)
    result: dict = {}
    try:
        projection = maintain_search_projection(data_dir, projection_directory, now=now)
    except Exception:
        logger.warning('Search projection capacity maintenance failed', exc_info=True)
        projection = {}
    if projection:
        result['searchProjection'] = projection
    if isinstance(witness, dict) and witness.get('localAuthority'):
        front = Path(str(witness['localAuthority']))
        state = load_state(data_dir)
        if _fresh(state.get('frontCensus'), str(front), now, FRONT_CENSUS_MAX_AGE_SECONDS) is None:
            census = take_census(front, now=now)
            _update_state(data_dir, lambda record: record.__setitem__('frontCensus', census))
        result['capacity'] = govern(data_dir, witness, environ, now=now)
        return result
    if needs_census(data_dir, environ, now=now):
        result['census'] = refresh_census(data_dir, now=now)
    return result


def _exit_reason(state: Mapping[str, object], authority: Path,
                 environ: Mapping[str, str], now: float) -> str | None:
    if not os.path.lexists(authority / FASTPATH_MARKER):
        return None
    mode = fastpath_mode(environ)
    if mode == 'explicit':
        return None
    journal = _record(state, 'exit')
    failed_at = journal.get('failedAt')
    if (journal.get('phase') == 'failed' and isinstance(failed_at, (int, float))
            and now - failed_at < EXIT_RETRY_SECONDS):
        return None
    requested = _record(state, 'exitRequested')
    if requested and mode in ('auto', 'on'):
        return str(requested.get('reason') or 'capacity')
    if mode == 'off' and _record(state, 'activeFront').get('origin') == 'automatic':
        return 'disabled'
    return None


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def complete_planned_exit(data_dir: Path, binary: str, environ: Mapping[str, str], *,
                          timeout: float, now: float | None = None,
                          run: Callable[..., subprocess.CompletedProcess] = subprocess.run
                          ) -> dict | None:
    """Export an automatic front back into a classic authority before serving.

    Runs before the daemon starts, only for automatic fronts that are capacity
    critical or that ``TOFU_DB_FASTPATH=off`` switched off. The engine export
    is lossless (surviving front plus its WAL tail) and verified, and the swap
    is journaled so an interruption resumes instead of guessing. A failed
    export never blocks service: the front keeps serving and the exit is
    retried at a later launch. The retired seed and shadow stay on the durable
    volume for the operator; the exported front's local copy is removed after
    the new authority is installed, because relieving the small tier is the
    point and every byte of it now lives in the verified export.
    """
    now = time.time() if now is None else now
    data_dir = Path(data_dir)
    authority = data_dir / AUTHORITY_NAME
    with _EXIT_LOCK:
        state = load_state(data_dir)
        journal = _record(state, 'exit')
        if journal.get('phase') not in ('exporting', 'swapping'):
            reason = _exit_reason(state, authority, environ, now)
            if reason is None:
                return None
            marker = read_marker(authority / FASTPATH_MARKER)
            if not marker or not marker.get('local_authority'):
                logger.error('Native fastpath exit requested but the relocation fence is unreadable; '
                             'operator recovery is required')
                return None
            stamp = time.strftime('%Y%m%dT%H%M%S', time.gmtime(now))
            journal = {
                'phase': 'exporting', 'reason': reason, 'startedAt': now, 'stamp': stamp,
                'target': str(data_dir / f'{AUTHORITY_NAME}.fastpath-exit-{stamp}'),
                'retiredSeed': str(data_dir / f'{AUTHORITY_NAME}.fastpath-seed-{stamp}'),
                'front': str(marker['local_authority']),
                'shadow': str(marker.get('shadow_directory') or ''),
            }
            _update_state(data_dir, lambda record: record.__setitem__('exit', journal))
        target, retired_seed = Path(journal['target']), Path(journal['retiredSeed'])
        if journal['phase'] == 'exporting':
            logger.warning('Native fastpath planned exit (%s): exporting the front back to %s '
                           'before serving', journal['reason'], target)
            try:
                if os.path.lexists(target):
                    target.rename(target.with_name(f'{target.name}.incomplete-{uuid.uuid4().hex[:8]}'))
                exported = run([binary, 'fastpath-export', '--data-dir', str(authority),
                                '--backup-dir', str(target)], capture_output=True, text=True,
                               timeout=timeout, env=dict(environ))
                if exported.returncode != 0 or 'export_verified=true' not in (exported.stdout or ''):
                    raise RuntimeError('fastpath-export failed: '
                                       + (exported.stderr or exported.stdout or '')[-_DETAIL_LIMIT:])
                inspected = run([binary, 'inspect', '--data-dir', str(target)], capture_output=True,
                                text=True, timeout=timeout, env=dict(environ))
                if inspected.returncode != 0 or 'authority_uuid=' not in (inspected.stdout or ''):
                    raise RuntimeError('the exported authority does not open: '
                                       + (inspected.stderr or '')[-_DETAIL_LIMIT:])
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                failed = {**journal, 'phase': 'failed', 'failedAt': time.time(),
                          'error': str(exc)[-_DETAIL_LIMIT:]}
                _update_state(data_dir, lambda record: record.__setitem__('exit', failed))
                logger.error('Native fastpath planned exit failed; the front keeps serving and the '
                             'exit is retried after %.0f h: %s', EXIT_RETRY_SECONDS / 3600, exc)
                return failed
            journal = {**journal, 'phase': 'swapping',
                       'exportReport': (exported.stdout or '').strip()[-_DETAIL_LIMIT:]}
            _update_state(data_dir, lambda record: record.__setitem__('exit', journal))
        # Swap failures raise: the journal resumes them, and a half-swapped
        # data directory fails closed in native launch instead of initializing.
        if os.path.lexists(authority) and os.path.lexists(authority / FASTPATH_MARKER):
            if os.path.lexists(retired_seed):
                raise RuntimeError(f'native fastpath exit cannot retire the seed: {retired_seed} exists')
            authority.rename(retired_seed)
            _sync_directory(data_dir)
        if os.path.lexists(target) and not os.path.lexists(authority):
            target.rename(authority)
            _sync_directory(data_dir)
        if not authority.is_dir() or os.path.lexists(authority / FASTPATH_MARKER):
            raise RuntimeError('native fastpath exit could not install the exported authority')
        stamp = journal['stamp']
        for location, action in ((journal.get('shadow'), 'retire'), (journal.get('front'), 'remove')):
            if not location or not os.path.lexists(location):
                continue
            try:
                if action == 'retire':
                    Path(location).rename(f'{location}.retired-{stamp}')
                else:
                    shutil.rmtree(location)
            except OSError as exc:
                # A leftover front only blocks later activation (an unclaimed
                # artifact the engine refuses); it never changes served data.
                logger.warning('Native fastpath exit could not %s %s: %s', action, location, exc)
        completed = {**journal, 'phase': 'complete', 'completedAt': time.time()}

        def finish(record: dict) -> None:
            record['exit'] = completed
            for key in ('exitRequested', 'activeFront', 'capacity', 'census', 'frontCensus'):
                record.pop(key, None)

        _update_state(data_dir, finish)
        logger.warning('Native fastpath front exported back to %s (%s); the retired seed %s and '
                       'its shadow stay for operator review', authority, journal['reason'],
                       retired_seed)
        return completed


def may_seed(data_dir: Path, environ: Mapping[str, str]) -> bool:
    """Whether this boot could seed, restore, resume or exit a fastpath front."""
    mode = fastpath_mode(environ)
    if mode == 'explicit':
        return True
    authority = Path(data_dir) / AUTHORITY_NAME
    if os.path.lexists(authority / FASTPATH_MARKER) or os.path.lexists(authority / PENDING_MARKER):
        return True
    if mode == 'off':
        return False
    state = load_state(data_dir)
    if _record(state, 'exit').get('phase') in ('exporting', 'swapping'):
        return True
    census = _fresh(state.get('census'), str(authority), time.time(), CENSUS_MAX_AGE_SECONDS)
    if census is None:
        return False
    # The same measured corpus and settings already led to a decline; the
    # long budget returns when either changes.
    last = _record(state, 'lastLaunch')
    return not (last.get('outcome') in ('classic', 'engine-declined')
                and last.get('censusMeasuredAt') == census.get('measuredAt')
                and last.get('inputs') == _launch_inputs(environ))


def automatic_state(data_dir: Path, environ: Mapping[str, str]) -> dict:
    """Panel projection: mode, last decision, capacity and exit status."""
    state = load_state(data_dir)
    return {
        'mode': fastpath_mode(environ),
        'lastLaunch': state.get('lastLaunch'),
        'census': state.get('census'),
        'activeFront': state.get('activeFront'),
        'capacity': state.get('capacity'),
        'exitRequested': state.get('exitRequested'),
        'exit': state.get('exit'),
    }


__all__ = [
    'LaunchPlan', 'activation_seconds', 'automatic_state', 'complete_planned_exit',
    'engine_admits', 'fastpath_mode', 'govern', 'headroom_status', 'maintain_search_projection',
    'may_seed', 'needs_census', 'plan_launch', 'record_launch', 'refresh_census',
    'run_maintenance', 'search_projection_launch_decision', 'search_projection_placement_state',
    'take_census',
]
