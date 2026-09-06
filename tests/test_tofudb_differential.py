"""Semantic differential gate: Tofu-DB vs the legacy Python storage authority.

``docs/STORAGE.md`` lists "semantic differential tests" as a hard launch gate
before Tofu-DB may be selected as a storage backend.  This suite replays
identical operation scripts against

* the legacy authority — a real SQLite-backed Sidecar (``tests._chat_sidecar``,
  the same client/service contract production uses), and
* the pre-authority engine — a real ``tofu-db serve`` process speaking the
  storage.v2 wire protocol (``scripts/tofudb_v2_client.py``),

and compares every observable result: success values (after documented
normalization) and classified error codes.

Coverage includes all 305 currently executable catalog operations, including
``record.*``, ``project.recent.*``, ``project.relink``, ``event.*``,
``artifact.*``, the complete
executable conversation/Turn, provider, model-routing, scheduler, timer, queue,
Project Brain, worker-job, task-result, tenant-user, credential, billing,
orchestration, Goal-run, and swarm-persistence surfaces, plus ``system.schema_version`` and
``system.reclaim``.
All five owner-scoped Research Foundry artifact, direction, and workspace
operations are covered, including workspace revision CAS. The exact bounded
``rate_limit.record_and_check`` hot path is covered as well.
All ten bounded owner-scoped ``paper.library.*`` operations, all eight
``paper.report.*`` operations, both ``paper.translation.*`` operations, and
all four ``paper.note.*`` operations are covered end to end.
All three ``raw_archive.*`` operations are covered, including parent fencing,
quota scrubbing, strict lazy decompression, and owner isolation.
All five owner-scoped ``daily_cost.*`` cache operations are covered.
Both ``log_aggregate.*`` observability operations are covered, including
SQLite-LIKE substring semantics and the bounded sweep's index tie order.
Both ``plugin.*`` manifest operations are covered, including normalization
coercions, same-version byte equality, and the append-only migration walk.
All eleven scheduler task/poll operations and all twelve timer operations are
covered, as are all sixteen queue operations.
The Project Brain foundation covers ``get``, four work transitions, and
``narrative.add`` with the projection and immutable stream compared together.
The seven ``worker_job.*`` operations are also covered end to end, plus
all seven ``task_results.*`` operations, ``compaction_archive.*`` (6),
``desktop.egress_agent.*`` (3),
all four ``tool_result_artifact.*`` operations,
``browser.site_observation.*`` (2), all seven
``tenant.user.*`` operations, all eleven ``credential.*`` operations, and the
seven executable ``billing.wallet.*``/``billing.ledger.*`` operations and all
four ``billing.payment.*`` operations, all three redemption-code operations,
and ``billing.reserve.stale``. All nineteen ``orchestration.*`` and
``goal.run.*`` operations are covered, including definition CAS, event
projection, terminal fencing, Goal supersession, and cross-owner startup
retirement. All sixteen ``integration.*`` operations cover durable workspace
registration, checkpoint queue transitions, global worker claiming/CAS,
metadata, project status, and events.
Three tiers define the denominator honestly:

* the 331-op storage catalog — the OperationSpec registries in
  ``lib/storage_sidecar/operation_domains/*`` and the ``name:`` table in
  ``packages/tofu-db/src/generated_storage_operations.rs`` enumerate
  exactly the same names (earlier 75/85 figures were snapshots of the
  still-growing Rust port);
* the subset tofu-db's executor actually serves — the rest answers
  ``operation_not_implemented`` (port WIP: the unported executors
  outside the covered set — the ``task_results.*`` maintenance ops
  landed last and are fully active);
* the differentially covered set above.

Gate completion definition (final state): coverage ratchets to every
operation the executor serves — ``_PORT_BACKLOG`` is now empty, so the
ratchet flips red the moment any further op becomes executable — all
``KNOWN_DIVERGENCES`` entries are adjudicated or fixed, and the staged
activation set stays empty (now satisfied: all six ``task_results.*``
scripts are active, so any future staged op flips the ratchet red).

``test_tofudb_port_ratchet`` compares the generated executable IR registry and
turns red the moment one more operation becomes executable, so port progress
converts immediately into coverage work; ``_PORT_BACKLOG`` tracked
implemented-but-uncovered ops and has ratcheted to zero.  The
``conversation.search`` deep path (ranking, snippet windows, edit/delete
visibility) is probed by ``_search_scripts()``: turns seed the projection
on both authorities and eventual steps poll each side's asynchronous
search worker until the result anchors and stabilizes before comparison.
``_task_results_scripts()`` activates piecemeal through
``_ACTIVE_TASK_RESULTS_SCRIPTS`` as the executor lands op by op (all seven
scripts and the complete domain are active).

The harness is domain-agnostic: extending coverage means adding entries to
``SCRIPTS`` and, where a semantic difference is intentional, a rationale
entry in ``KNOWN_DIVERGENCES``.  An *unexpected* divergence fails the gate;
a *documented* divergence that disappears also fails, so the manifest
ratchets toward zero.

Tofu-DB binary resolution order:
  1. ``TOFUDB_DIFFERENTIAL_BIN`` test-only environment override;
  2. a cargo build via the shared project toolchain (see the
     ``rustup-shared-toolchain-do-not-update`` project memory) when cargo is
     reachable;
  3. ``packages/tofu-db/target/debug/tofu-db`` (may lag the working tree —
     a fresh build is always preferred);
  4. otherwise the suite skips.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.tofudb_v2_client import (  # noqa: E402
    StorageV2Error,
    StorageV2Session,
)

pytest_plugins = ('tests._chat_sidecar',)

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CARGO_CANDIDATES = (
    Path(
        '/path/to/your/data'
        '.rustup/toolchains/stable-x86_64-unknown-linux-gnu/bin/cargo'
    ),
    Path(shutil.which('cargo') or '/nonexistent'),
)
_TEST_SECRET = b'tofudb-differential-test-secret-0001'  # 34 ASCII bytes
_OWNER_ID = 11
_TENANT_ID = 7

# Wall-clock fields both sides fill from their own clock; compare presence
# and type, never equality.  Conversation header timestamps are NOT here:
# the scripts pin them to deterministic values through the create/update
# payloads so they stay compared.
_DROP_FIELDS = {'updated_at_ms', 'created_at_ms', 'first_event_at_ms',
                'deletedAt', 'requested_at_ms', 'abort_requested_at',
                'serverNowMs', 'occurredAt'}

# Far-future retention cutoff (post-2100) that qualifies every appended
# event for an owner-scope prune sweep.
_FAR_FUTURE_MS = 4_000_000_000_000

# Deterministic conversation header timestamps pinned through create/update
# payloads so the documents stay comparable across the two clocks.
_CONV_TS = 1_700_000_000_000

# Eventual-consistency polling for asynchronously-derived projections.
_EVENTUAL_POLL_INTERVAL_S = 0.05
_EVENTUAL_STABLE_WINDOW_S = 0.75
_EVENTUAL_TIMEOUT_S = 45.0

# ── Binary resolution ────────────────────────────────────────────────────
def _resolve_tofudb_binary() -> Path:
    override = os.environ.get('TOFUDB_DIFFERENTIAL_BIN', '').strip()
    if override:
        binary = Path(override)
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
        pytest.skip(f'TOFUDB_DIFFERENTIAL_BIN is not executable: {override}')
    manifest = _PROJECT_ROOT / 'packages/tofu-db/Cargo.toml'
    cargo = next((path for path in _CARGO_CANDIDATES if path.is_file()), None)
    if cargo is not None:
        target = _PROJECT_ROOT / 'packages/tofu-db/target-differential'
        env = dict(os.environ)
        env.setdefault(
            'CARGO_HOME',
            '/path/to/your/data',
        )
        env.setdefault(
            'RUSTUP_HOME',
            '/path/to/your/data',
        )
        # cargo needs rustc on PATH; the shared toolchain bin directory
        # provides both (see the rustup-shared-toolchain project memory).
        env['PATH'] = f'{cargo.parent}:{env.get("PATH", "")}'
        env['CARGO_TARGET_DIR'] = str(target)
        try:
            subprocess.run(
                [str(cargo), 'build', '--manifest-path', str(manifest)],
                check=True, capture_output=True, text=True, timeout=1800,
                env=env,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass  # fall through to any checked-in build
        else:
            built = target / 'debug/tofu-db'
            if built.is_file():
                return built
    prebuilt = _PROJECT_ROOT / 'packages/tofu-db/target/debug/tofu-db'
    if prebuilt.is_file() and os.access(prebuilt, os.X_OK):
        return prebuilt
    pytest.skip(
        'no tofu-db binary available '
        '(set TOFUDB_DIFFERENTIAL_BIN or install cargo)'
    )


# ── Drivers ──────────────────────────────────────────────────────────────
@dataclass
class Outcome:
    """One normalized observable operation result."""

    ok: bool
    value: Any = None
    error_code: str = ''
    error_message: str = ''


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize(item)
            for key, item in sorted(value.items())
            if key not in _DROP_FIELDS
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _drop_comparison_fields(value: Any, fields: frozenset[str]) -> Any:
    """Remove explicitly nondeterministic fields for one comparison step."""
    if isinstance(value, dict):
        return {
            key: _drop_comparison_fields(item, fields)
            for key, item in sorted(value.items())
            if key not in fields
        }
    if isinstance(value, list):
        return [_drop_comparison_fields(item, fields) for item in value]
    return value


class LegacyDriver:
    """Drives the real Python storage authority through its public client."""

    def __init__(self):
        from lib.storage import get_storage_client

        self._client = get_storage_client(write=True)

    def execute(
        self, operation: str, payload: dict[str, Any], command_id: str | None,
        *, maintenance: bool = False,
    ) -> Outcome:
        from lib.storage.errors import StorageError

        try:
            if maintenance:
                result = self._client.maintenance(
                    operation, payload, deadline=15.0)
            elif command_id is None:
                result = self._client.query(operation, payload, deadline=15.0)
            else:
                result = self._client.command(
                    operation, payload, command_id, deadline=15.0)
        except StorageError as exc:
            return Outcome(
                ok=False, error_code=exc.code, error_message=str(exc))
        return Outcome(ok=True, value=_normalize(result))


class TofuDbDriver:
    """Drives a real ``tofu-db serve`` process over storage.v2."""

    def __init__(self, session: StorageV2Session):
        self._session = session

    def execute(
        self, operation: str, payload: dict[str, Any], command_id: str | None,
        *, maintenance: bool = False,
    ) -> Outcome:
        assert not maintenance or command_id is not None
        deadline = int(time.time() * 1000) + 15_000
        try:
            response = self._session.request(
                operation, payload,
                command_id=command_id, deadline_unix_ms=deadline,
            )
        except StorageV2Error as exc:
            return Outcome(
                ok=False, error_code=exc.code, error_message=exc.message)
        if response.status != 0:
            return Outcome(
                ok=False, error_code=response.error_code or 'unknown',
                error_message=response.error_message or '',
            )
        return Outcome(ok=True, value=_normalize(response.json()))


# ── Fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture(scope='module')
def tofudb_binary() -> Path:
    return _resolve_tofudb_binary()


@pytest.fixture()
def tofudb_daemon(tofudb_binary: Path):
    """Run one fresh-authority ``tofu-db serve`` until the test ends."""
    data_dir = Path(tempfile.mkdtemp(prefix='tofudb-diff-authority-'))
    search_projection_parent = Path(tempfile.mkdtemp(
        prefix='tofudb-diff-search-projection-parent-',
    ))
    # The daemon initializes a missing projection authority; an existing empty
    # directory is deliberately treated as an invalid/corrupt authority.
    search_projection_dir = search_projection_parent / 'projection'
    try:
        subprocess.run(
            [str(tofudb_binary), 'init', '--data-dir', str(data_dir)],
            check=True, capture_output=True, text=True, timeout=120,
        )
        env = dict(os.environ)
        env['TOFU_STORAGE_TOKEN'] = _TEST_SECRET.decode('ascii')
        process = subprocess.Popen(
            [
                str(tofudb_binary), 'serve',
                '--data-dir', str(data_dir),
                '--owner-id', str(_OWNER_ID),
                '--tenant-id', str(_TENANT_ID),
                '--search-projection-dir', str(search_projection_dir),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )
        try:
            readiness_line = process.stdout.readline().strip()
            if not readiness_line:
                raise RuntimeError('tofu-db serve exited before readiness')
            readiness = json.loads(readiness_line)
            port = int(readiness['port'])
            session = StorageV2Session(
                '127.0.0.1', port, _TEST_SECRET,
                owner_id=_OWNER_ID, tenant_id=_TENANT_ID, timeout=15.0,
            )
            yield TofuDbDriver(session)
            session.close()
        finally:
            if process.stdin:
                process.stdin.close()  # EOF is the supervised shutdown signal
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(search_projection_parent, ignore_errors=True)


@pytest.fixture()
def legacy_driver(chat_sidecar) -> LegacyDriver:  # noqa: ARG001
    return LegacyDriver()


# ── Operation scripts ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Step:
    operation: str
    payload: dict[str, Any]
    command: bool = False
    # storage.v1 has a distinct maintenance frame; storage.v2 still requires
    # a command ID because the operation can mutate authority state.
    maintenance: bool = False
    # Names a KNOWN_DIVERGENCES entry when this step legitimately differs.
    expect_divergence: str = ''
    # False for baseline-reset steps whose result counts depend on history
    # the two fixtures legitimately do not share (module- vs function-
    # scoped authorities).  The step still executes on both sides.
    compare: bool = True
    # Recursively ignored only for this step. This is reserved for values such
    # as clone-assigned wall-clock timestamps which callers cannot prescribe.
    ignore_fields: frozenset[str] = frozenset()
    # True for reads of asynchronously-derived projections
    # (conversation.search): both authorities rebuild their search index in
    # a background worker, so the read is polled until the observed value
    # anchors and stabilizes before comparison (see _execute_step).
    eventual: bool = False
    # Minimum list length required before stability counting starts — proves
    # the projection pipeline caught up with the seeded mutations.
    eventual_min_hits: int = 0


def _cid() -> str:
    return f'diff-{uuid.uuid4().hex[:24]}'


@dataclass(frozen=True)
class _Ref:
    """A payload placeholder resolved from an earlier step's outcome.

    Server-generated identities (attempt ids, branch lane ids) are random
    on both authorities, so a step that must name one declares a reference
    into the producing step's outcome; ``_run_script`` resolves it against
    the run actually in flight, giving each driver its own authority's id.
    """

    step: int
    path: tuple[str, ...]


def _resolve_payload_refs(
    payload: Any, outcomes: list[tuple[Step, Outcome]],
) -> Any:
    if isinstance(payload, _Ref):
        value = outcomes[payload.step][1].value
        for key in payload.path:
            value = value[key]
        return value
    if isinstance(payload, dict):
        return {
            key: _resolve_payload_refs(item, outcomes)
            for key, item in payload.items()
        }
    if isinstance(payload, list):
        return [_resolve_payload_refs(item, outcomes) for item in payload]
    return payload


# Server-generated identities carry no semantic content: attempt ids are
# uuid4 in both engines; branch lane ids are random in legacy and
# command-derived in Tofu-DB.  Each run's outcomes are tokenized
# independently — the i-th distinct server id maps to ``<gen:i>`` — so
# aligned scripts compare equal while the ids stay structurally checked
# (every readback of one id keeps one placeholder within a run).
_SERVER_ID_PATTERN = re.compile(
    r'^(?:lane_)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}$'
)


def _tokenize_server_ids(value: Any, tokens: dict[str, str]) -> Any:
    if isinstance(value, dict):
        tokenized = {
            key: _tokenize_server_ids(item, tokens)
            for key, item in sorted(value.items())
        }
        # Deletion identities are a set.  Both authorities expose them in
        # lexical order, but UUIDs are generated independently, so lexical
        # order can map to different semantic Turn roles in the two runs.
        # Compare the already-tokenized set without weakening identity
        # consistency checks elsewhere in the response.
        deleted_turn_ids = tokenized.get('deletedTurnIds')
        if isinstance(deleted_turn_ids, list):
            tokenized['deletedTurnIds'] = sorted(
                deleted_turn_ids, key=lambda item: json.dumps(
                    item, sort_keys=True, separators=(',', ':')))
        return tokenized
    if isinstance(value, list):
        return [_tokenize_server_ids(item, tokens) for item in value]
    if isinstance(value, str) and _SERVER_ID_PATTERN.match(value):
        if value not in tokens:
            tokens[value] = f'<gen:{len(tokens)}>'
        return tokens[value]
    return value


def test_deleted_turn_identity_comparison_is_order_independent() -> None:
    input_turn_id = '00000000-0000-4000-8000-000000000001'
    output_turn_id = '00000000-0000-4000-8000-000000000002'
    tokens = {
        input_turn_id: '<gen:0>',
        output_turn_id: '<gen:1>',
    }

    assert _tokenize_server_ids(
        {'deletedTurnIds': [output_turn_id, input_turn_id]}, tokens,
    ) == {'deletedTurnIds': ['<gen:0>', '<gen:1>']}


def _record_scripts() -> dict[str, list[Step]]:
    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        # One fresh namespace per script so un-prefixed list queries only
        # observe that script's own rows.
        return make(f'diff_{uuid.uuid4().hex[:8]}')

    return {
        'record put/get round trip': build(lambda ns: [
            Step('record.put', {
                'namespace': ns, 'key': 'alpha',
                'value': {'n': 1, 's': 'x', 'l': [True, None]},
            }, command=True),
            Step('record.get', {'namespace': ns, 'key': 'alpha'}),
        ]),
        'record get missing returns null': build(lambda ns: [
            Step('record.get', {'namespace': ns, 'key': 'absent'}),
        ]),
        'record put overwrites and bumps version': build(lambda ns: [
            Step('record.put', {'namespace': ns, 'key': 'k', 'value': 1},
                 command=True),
            Step('record.put', {'namespace': ns, 'key': 'k', 'value': 2},
                 command=True),
            Step('record.get', {'namespace': ns, 'key': 'k'}),
        ]),
        'record put version CAS conflict then success': build(lambda ns: [
            Step('record.put', {'namespace': ns, 'key': 'cas', 'value': 'v1'},
                 command=True),
            Step('record.put', {
                'namespace': ns, 'key': 'cas', 'value': 'bad',
                'expected_version': 9,
            }, command=True),
            Step('record.put', {
                'namespace': ns, 'key': 'cas', 'value': 'v2',
                'expected_version': 1,
            }, command=True),
            Step('record.get', {'namespace': ns, 'key': 'cas'}),
        ]),
        'record delete then absent': build(lambda ns: [
            Step('record.put', {'namespace': ns, 'key': 'gone', 'value': 1},
                 command=True),
            Step('record.delete', {'namespace': ns, 'key': 'gone'},
                 command=True),
            Step('record.get', {'namespace': ns, 'key': 'gone'}),
            # Idempotent re-delete must agree on both sides.
            Step('record.delete', {'namespace': ns, 'key': 'gone'},
                 command=True),
        ]),
        'record delete version CAS conflict': build(lambda ns: [
            Step('record.put', {'namespace': ns, 'key': 'dcas', 'value': 1},
                 command=True),
            Step('record.delete', {
                'namespace': ns, 'key': 'dcas', 'expected_version': 5,
            }, command=True),
            Step('record.get', {'namespace': ns, 'key': 'dcas'}),
        ]),
        'record list prefix and order': build(lambda ns: [
            Step('record.put', {'namespace': ns, 'key': 'p/b', 'value': 2},
                 command=True),
            Step('record.put', {'namespace': ns, 'key': 'p/a', 'value': 1},
                 command=True),
            Step('record.put', {'namespace': ns, 'key': 'q/z', 'value': 3},
                 command=True),
            Step('record.list', {'namespace': ns, 'prefix': 'p/'}),
            Step('record.list', {'namespace': ns, 'prefix': ''}),
        ]),
        'record list limit bound is rejected': build(lambda ns: [
            Step('record.list', {'namespace': ns, 'limit': 0}),
            Step('record.list', {'namespace': ns, 'limit': 1001}),
        ]),
        # A prefix containing '_' or '%' is a literal byte prefix on both
        # authorities (legacy escapes LIKE metacharacters; Tofu-DB compares
        # literal bytes).  Seed one key each side of the would-be wildcard
        # to prove both return only the literal match.
        'record list prefix treats wildcards literally': build(lambda ns: [
            Step('record.put', {'namespace': ns, 'key': 'p_a/1', 'value': 1},
                 command=True),
            Step('record.put', {'namespace': ns, 'key': 'pxa/1', 'value': 2},
                 command=True),
            Step('record.list', {'namespace': ns, 'prefix': 'p_a'}),
        ]),
        'record unicode keys and values': build(lambda ns: [
            Step('record.put', {
                'namespace': ns, 'key': '键/🚀',
                'value': {'文本': '值', 'emoji': '🚀'},
            }, command=True),
            Step('record.get', {'namespace': ns, 'key': '键/🚀'}),
            Step('record.list', {'namespace': ns, 'prefix': '键/'}),
        ]),
    }


def _project_recent_scripts() -> dict[str, list[Step]]:
    """Recent-project domain; payload user_id must match the session owner.

    The owner scope is fixed by the wire session, so every script opens
    with a clear: the legacy authority is module-scoped and retains rows
    across tests, while each tofu-db authority starts empty.
    """
    user = _OWNER_ID

    def reset() -> Step:
        return Step(
            'project.recent.clear', {'user_id': user},
            command=True, compare=False,
        )

    return {
        'project.recent touch increments and lists newest first': [
            reset(),
            Step('project.recent.touch', {
                'user_id': user, 'project_path': '/alpha', 'last_used': 100,
            }, command=True),
            Step('project.recent.touch', {
                'user_id': user, 'project_path': '/beta', 'last_used': 200,
            }, command=True),
            Step('project.recent.touch', {
                'user_id': user, 'project_path': '/alpha', 'last_used': 300,
            }, command=True),
            Step('project.recent.list', {'user_id': user}),
        ],
        'project.recent touch_many dedupes and counts': [
            reset(),
            Step('project.recent.touch_many', {
                'user_id': user,
                'project_paths': ['/x', '/y', '/x'],
                'last_used': 50,
            }, command=True),
            Step('project.recent.list', {'user_id': user}),
        ],
        'project.recent touch_many rejects empty batch': [
            reset(),
            Step('project.recent.touch_many', {
                'user_id': user, 'project_paths': [], 'last_used': 50,
            }, command=True),
        ],
        'project.recent clear removes the owner scope': [
            reset(),
            Step('project.recent.touch', {
                'user_id': user, 'project_path': '/gone', 'last_used': 1,
            }, command=True),
            Step('project.recent.clear', {'user_id': user}, command=True),
            Step('project.recent.list', {'user_id': user}),
        ],
    }


def _project_relink_scripts() -> dict[str, list[Step]]:
    """Cross-plane project.relink: recent entry, brain scope, pins.

    Paths carry a unique suffix so residue in the module-scoped legacy
    authority (trash capsules, brain scopes) never collides across scripts.
    The started work item is finished after the comparisons so no active
    work leaks into later ``project_brain.active.list`` steps.
    """
    user = _OWNER_ID
    suffix = uuid.uuid4().hex[:12]
    old = f'/relink/{suffix}/old'
    new = f'/relink/{suffix}/new'
    conv_active = f'conv_relink_a_{suffix}'
    conv_trashed = f'conv_relink_t_{suffix}'
    task_id = f'project-task-relink-{suffix}'
    work_id = 'pw_' + hashlib.sha256(task_id.encode()).hexdigest()[:24]

    def reset() -> Step:
        return Step(
            'project.recent.clear', {'user_id': user},
            command=True, compare=False,
        )

    return {
        'project.relink moves recent brain and conversation pins': [
            reset(),
            Step('project.recent.touch', {
                'user_id': user, 'project_path': old, 'last_used': 100,
            }, command=True),
            Step('conversation.create', {
                'conv_id': conv_active, 'user_id': user, 'title': 'Active',
                'settings': {'projectPath': old},
                'created_at': _CONV_TS, 'updated_at': _CONV_TS,
            }, command=True),
            Step('conversation.create', {
                'conv_id': conv_trashed, 'user_id': user, 'title': 'Trashed',
                'settings': {'projectPath': old, 'readOnlyPaths': [old, '/other']},
                'created_at': _CONV_TS, 'updated_at': _CONV_TS,
            }, command=True),
            Step('conversation.delete',
                 {'conv_id': conv_trashed, 'user_id': user}, command=True),
            Step('project_brain.work.start', {
                'owner_user_id': user, 'project_key': old,
                'work_item': {
                    'id': work_id, 'taskId': task_id,
                    'conversationId': conv_active,
                    'title': 'Relink work', 'trigger': 'file_write',
                    'status': 'active', 'changedPaths': [], 'artifacts': [],
                    'resultSummary': '', 'startedAt': 1, 'finishedAt': None,
                    '_titlePriority': 100, '_titleRefined': False,
                },
                'timestamp': 1,
            }, command=True),
            Step('project.relink', {
                'user_id': user, 'old_path': old, 'new_path': new,
            }, command=True),
            Step('project.recent.list', {'user_id': user}),
            Step('conversation.get',
                 {'conv_id': conv_active, 'user_id': user}),
            Step('project_brain.get',
                 {'owner_user_id': user, 'project_key': new}),
            Step('project_brain.active.list', {'owner_user_id': user}),
            Step('project_brain.work.finish', {
                'owner_user_id': user, 'project_key': new,
                'work_id': work_id, 'status': 'completed',
                'result_summary': '', 'timestamp': 2,
            }, command=True),
            Step('conversation.delete',
                 {'conv_id': conv_active, 'user_id': user}, command=True),
        ],
        'project.relink merges an existing recent entry': [
            reset(),
            Step('project.recent.touch', {
                'user_id': user,
                'project_path': f'{old}m', 'last_used': 100,
            }, command=True),
            Step('project.recent.touch', {
                'user_id': user,
                'project_path': f'{old}m', 'last_used': 200,
            }, command=True),
            Step('project.recent.touch', {
                'user_id': user,
                'project_path': f'{new}m', 'last_used': 150,
            }, command=True),
            Step('project.relink', {
                'user_id': user,
                'old_path': f'{old}m', 'new_path': f'{new}m',
            }, command=True),
            Step('project.recent.list', {'user_id': user}),
        ],
        'project.relink rejects identical and unknown paths': [
            reset(),
            Step('project.relink', {
                'user_id': user,
                'old_path': f'{old}i', 'new_path': f'{old}i',
            }, command=True),
            Step('project.relink', {
                'user_id': user,
                'old_path': f'{old}u', 'new_path': f'{new}u',
            }, command=True),
        ],
    }


def _event_scripts() -> dict[str, list[Step]]:
    """Task-event stream domain; one fresh task namespace per script.

    ``event.prune`` is an owner-scope retention sweep, not task-scoped, so
    it doubles as the baseline reset for the module-scoped legacy authority
    (each tofu-db authority already starts empty).  Reset sweeps are not
    compared: their counts depend on residue the two fixtures legitimately
    do not share.
    """
    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        return make(f'evt_{uuid.uuid4().hex[:8]}')

    def resets() -> list[Step]:
        return [
            Step('event.prune', {
                'created_before_ms': _FAR_FUTURE_MS, 'limit': 1000,
                'retention_class': 'streaming',
            }, command=True, compare=False),
            Step('event.prune', {
                'created_before_ms': _FAR_FUTURE_MS, 'limit': 1000,
                'retention_class': 'structural',
            }, command=True, compare=False),
        ]

    return {
        'event append/list/bounds/latest round trip': build(lambda t: [
            *resets(),
            Step('event.append', {
                'task_id': t, 'sequence': 0,
                'event': {'type': 'token_delta', 'text': 'a'},
            }, command=True),
            Step('event.append', {
                'task_id': t, 'sequence': 1,
                'event': {'type': 'token_delta', 'text': 'b'},
            }, command=True),
            Step('event.append', {
                'task_id': t, 'sequence': 2,
                'event': {
                    'type': 'messages_snapshot', 'kind': 'request',
                    'messages': [{'role': 'user', 'content': 'hi'}],
                },
            }, command=True),
            Step('event.list', {'task_id': t}),
            Step('event.list', {'task_id': t, 'after_sequence': 1}),
            Step('event.list', {'task_id': t, 'limit': 2}),
            Step('event.bounds', {'task_id': t}),
            Step('event.latest', {'task_id': t}),
        ]),
        'event append dedup and sequence conflict': build(lambda t: [
            *resets(),
            Step('event.append', {
                'task_id': t, 'sequence': 0,
                'event': {'type': 'token_delta', 'text': 'same'},
            }, command=True),
            # Idempotent replay of the identical payload dedupes.
            Step('event.append', {
                'task_id': t, 'sequence': 0,
                'event': {'type': 'token_delta', 'text': 'same'},
            }, command=True),
            # Same sequence carrying a different payload is a conflict.
            Step('event.append', {
                'task_id': t, 'sequence': 0,
                'event': {'type': 'token_delta', 'text': 'different'},
            }, command=True),
            Step('event.bounds', {'task_id': t}),
        ]),
        'event append rejects invalid envelopes': build(lambda t: [
            *resets(),
            # Negative sequence.
            Step('event.append', {
                'task_id': t, 'sequence': -1,
                'event': {'type': 'token_delta'},
            }, command=True),
            # Only the task stream crosses this boundary.
            Step('event.append', {
                'task_id': t, 'sequence': 0,
                'stream_kind': 'project_brain',
                'event': {'type': 'token_delta'},
            }, command=True),
            Step('event.bounds', {'task_id': t}),
        ]),
        'event append_batch dedupes and validates': build(lambda t: [
            *resets(),
            Step('event.append_batch', {
                'events': [
                    {'task_id': t, 'sequence': 0,
                     'event': {'type': 'token_delta', 'text': 'x'}},
                    {'task_id': t, 'sequence': 1,
                     'event': {'type': 'token_delta', 'text': 'y'}},
                    # In-batch replay of an already-durable row dedupes.
                    {'task_id': t, 'sequence': 0,
                     'event': {'type': 'token_delta', 'text': 'x'}},
                ],
            }, command=True),
            # Full replay of the same batch inserts nothing.
            Step('event.append_batch', {
                'events': [
                    {'task_id': t, 'sequence': 0,
                     'event': {'type': 'token_delta', 'text': 'x'}},
                    {'task_id': t, 'sequence': 1,
                     'event': {'type': 'token_delta', 'text': 'y'}},
                ],
            }, command=True),
            # Empty batches are rejected by both authorities.
            Step('event.append_batch', {'events': []}, command=True),
            Step('event.bounds', {'task_id': t}),
        ]),
        'event list type and prefix filters': build(lambda t: [
            *resets(),
            Step('event.append', {
                'task_id': t, 'sequence': 0,
                'event': {'type': 'messages_snapshot', 'kind': 'request'},
            }, command=True),
            Step('event.append', {
                'task_id': t, 'sequence': 1,
                'event': {'type': 'token_delta', 'text': 'd'},
            }, command=True),
            Step('event.append', {
                'task_id': t, 'sequence': 2,
                'event': {'type': 'messages_snapshot', 'kind': 'state'},
            }, command=True),
            Step('event.list', {'task_id': t, 'types': ['messages_snapshot']}),
            Step('event.list', {'task_id': t, 'type_prefixes': ['token']}),
            Step('event.list', {
                'task_id': t, 'types': ['messages_snapshot'], 'limit': 1,
            }),
            # Empty streams project stable empty shapes.
            Step('event.list', {'task_id': f'{t}_empty'}),
            Step('event.bounds', {'task_id': f'{t}_empty'}),
            Step('event.latest', {'task_id': f'{t}_empty'}),
        ]),
        'event inspector_summary counts roots and swarm children': (
            build(lambda t: [
                *resets(),
                Step('event.append', {
                    'task_id': t, 'sequence': 0,
                    'event': {'type': 'messages_snapshot', 'kind': 'request'},
                }, command=True),
                Step('event.append', {
                    'task_id': t, 'sequence': 1,
                    'event': {'type': 'messages_snapshot', 'kind': 'state'},
                }, command=True),
                # Streaming rows never enter the structural fold.
                Step('event.append', {
                    'task_id': t, 'sequence': 2,
                    'event': {'type': 'token_delta', 'text': 'noise'},
                }, command=True),
                Step('event.append', {
                    'task_id': f'{t}#agent:1', 'sequence': 0,
                    'event': {'type': 'messages_snapshot', 'kind': 'request'},
                }, command=True),
                Step('event.inspector_summary', {'task_ids': [t]}),
                Step('event.inspector_summary',
                     {'task_ids': [f'{t}_absent']}),
            ])
        ),
        'event prune removes by retention class': build(lambda t: [
            *resets(),
            Step('event.append', {
                'task_id': t, 'sequence': 0,
                'event': {'type': 'token_delta', 'text': 's0'},
            }, command=True),
            Step('event.append', {
                'task_id': t, 'sequence': 1,
                'event': {'type': 'token_delta', 'text': 's1'},
            }, command=True),
            Step('event.append', {
                'task_id': t, 'sequence': 2,
                'event': {'type': 'messages_snapshot', 'kind': 'state'},
            }, command=True),
            # Single-task sweeps keep the physical-retirement queue to one
            # entry so has_more is deterministic on both engines.
            Step('event.prune', {
                'created_before_ms': _FAR_FUTURE_MS, 'limit': 100,
                'retention_class': 'streaming',
            }, command=True),
            Step('event.bounds', {'task_id': t}),
            Step('event.prune', {
                'created_before_ms': _FAR_FUTURE_MS, 'limit': 100,
                'retention_class': 'structural',
            }, command=True),
            Step('event.bounds', {'task_id': t}),
            # Unknown retention classes are rejected by both authorities.
            Step('event.prune', {
                'created_before_ms': _FAR_FUTURE_MS,
                'retention_class': 'bogus',
            }, command=True),
        ]),
    }


def _conversation_scripts() -> dict[str, list[Step]]:
    """Conversation header domain; deterministic timestamps via payloads.

    Every script deletes the conversations it creates, so the owner scope
    returns to an empty baseline on the module-scoped legacy authority and
    ``conversation.count`` stays comparable against each fresh tofu-db
    authority.
    """
    user = _OWNER_ID

    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        return make(f'conv_{uuid.uuid4().hex[:8]}')

    return {
        'conversation create/get/count/list/delete lifecycle': build(
            lambda c: [
                Step('conversation.count', {'user_id': user}),
                Step('conversation.create', {
                    'conv_id': c, 'user_id': user, 'title': 'Alpha',
                    'settings': {'projectPath': '/p'},
                    'created_at': _CONV_TS, 'updated_at': _CONV_TS,
                }, command=True),
                # Duplicate ids conflict on both authorities.
                Step('conversation.create', {
                    'conv_id': c, 'user_id': user, 'title': 'Again',
                    'created_at': _CONV_TS, 'updated_at': _CONV_TS,
                }, command=True),
                Step('conversation.get', {'conv_id': c, 'user_id': user}),
                Step('conversation.count', {'user_id': user}),
                Step('conversation.list', {
                    'user_id': user, 'ids': [c, f'{c}_absent'],
                    'order_by': 'id_asc',
                }),
                Step('conversation.list', {
                    'user_id': user, 'ids': [c], 'include_messages': False,
                    'order_by': 'id_asc',
                }),
                Step('conversation.metadata.update', {
                    'conv_id': c, 'user_id': user,
                    'updates': {'title': 'Beta', 'updated_at': _CONV_TS + 10},
                }, command=True),
                Step('conversation.get', {'conv_id': c, 'user_id': user}),
                Step('conversation.settings.update', {
                    'conv_id': c, 'user_id': user,
                    'updates': {'model': 'x'},
                }, command=True),
                Step('conversation.get', {'conv_id': c, 'user_id': user}),
                Step('conversation.delete', {'conv_id': c, 'user_id': user},
                     command=True),
                Step('conversation.get', {'conv_id': c, 'user_id': user}),
                Step('conversation.count', {'user_id': user}),
                # A trashed id is reported, not silently recreated.
                Step('conversation.delete', {'conv_id': c, 'user_id': user},
                     command=True),
            ]
        ),
        'conversation list filters and ordering': build(lambda ns: [
            Step('conversation.create', {
                'conv_id': f'{ns}_a', 'user_id': user, 'title': 'One',
                'created_at': _CONV_TS, 'updated_at': _CONV_TS,
            }, command=True),
            Step('conversation.create', {
                'conv_id': f'{ns}_b', 'user_id': user, 'title': 'Two',
                'created_at': _CONV_TS + 1, 'updated_at': _CONV_TS + 1,
            }, command=True),
            Step('conversation.create', {
                'conv_id': f'{ns}_c', 'user_id': user, 'title': 'Three',
                'created_at': _CONV_TS + 2, 'updated_at': _CONV_TS + 2,
            }, command=True),
            Step('conversation.list', {
                'user_id': user,
                'ids': [f'{ns}_a', f'{ns}_b', f'{ns}_c'],
                'order_by': 'updated_at_desc', 'include_messages': False,
            }),
            Step('conversation.list', {
                'user_id': user,
                'ids': [f'{ns}_a', f'{ns}_b', f'{ns}_c'],
                'order_by': 'id_asc', 'include_messages': False,
            }),
            Step('conversation.list', {
                'user_id': user,
                'ids': [f'{ns}_a', f'{ns}_b', f'{ns}_c'],
                'updated_at_gte': _CONV_TS + 1,
                'order_by': 'updated_at_desc', 'include_messages': False,
            }),
            # An empty id filter short-circuits to an empty page.
            Step('conversation.list', {'user_id': user, 'ids': []}),
            Step('conversation.delete',
                 {'conv_id': f'{ns}_a', 'user_id': user}, command=True),
            Step('conversation.delete',
                 {'conv_id': f'{ns}_b', 'user_id': user}, command=True),
            Step('conversation.delete',
                 {'conv_id': f'{ns}_c', 'user_id': user}, command=True),
            Step('conversation.count', {'user_id': user}),
        ]),
        'conversation settings revision CAS': build(lambda c: [
            Step('conversation.create', {
                'conv_id': c, 'user_id': user, 'title': 'Cas',
                'settings': {'a': 1},
                'created_at': _CONV_TS, 'updated_at': _CONV_TS,
            }, command=True),
            Step('conversation.settings.update', {
                'conv_id': c, 'user_id': user,
                'updates': {'b': 2}, 'expected_rev': 0,
            }, command=True),
            # A stale expected revision is refused without a conflict flag.
            Step('conversation.settings.update', {
                'conv_id': c, 'user_id': user,
                'updates': {'c': 3}, 'expected_rev': 5,
            }, command=True),
            # Replace mode requires the full expected snapshot.
            Step('conversation.settings.update', {
                'conv_id': c, 'user_id': user,
                'updates': {'z': 9}, 'replace': True,
            }, command=True),
            Step('conversation.settings.update', {
                'conv_id': c, 'user_id': user,
                'updates': {'z': 9}, 'replace': True,
                'expected_settings': {'a': 1, 'b': 2},
            }, command=True),
            Step('conversation.get', {'conv_id': c, 'user_id': user}),
            # Missing conversations report, never raise.
            Step('conversation.settings.update', {
                'conv_id': f'{c}_absent', 'user_id': user,
                'updates': {'m': 1},
            }, command=True),
            Step('conversation.metadata.update', {
                'conv_id': f'{c}_absent', 'user_id': user,
                'updates': {'title': 'Nope'},
            }, command=True),
            # Unknown metadata keys are rejected by both authorities.
            Step('conversation.metadata.update', {
                'conv_id': c, 'user_id': user,
                'updates': {'bogus': 1},
            }, command=True),
            Step('conversation.delete', {'conv_id': c, 'user_id': user},
                 command=True),
        ]),
        'conversation clone snapshots an inert copy': build(lambda c: [
            # A missing source reports, never raises.
            Step('conversation.clone', {
                'conv_id': f'{c}_absent', 'user_id': user,
                'destination_conv_id': f'{c}_d0',
            }, command=True),
            Step('conversation.create', {
                'conv_id': c, 'user_id': user, 'title': 'Src',
                'settings': {'k': 1},
                'created_at': _CONV_TS, 'updated_at': _CONV_TS,
            }, command=True),
            Step('conversation.clone', {
                'conv_id': c, 'user_id': user,
                'destination_conv_id': f'{c}_d1',
            }, command=True),
            # An occupied destination id conflicts on both authorities.
            Step('conversation.clone', {
                'conv_id': c, 'user_id': user,
                'destination_conv_id': f'{c}_d1',
            }, command=True),
            # A blank override title is a protocol error.
            Step('conversation.clone', {
                'conv_id': c, 'user_id': user,
                'destination_conv_id': f'{c}_d2', 'title': '   ',
            }, command=True),
            Step('conversation.clone', {
                'conv_id': c, 'user_id': user,
                'destination_conv_id': f'{c}_d3', 'title': 'Custom',
            }, command=True),
            Step('conversation.get',
                 {'conv_id': f'{c}_d1', 'user_id': user},
                 ignore_fields=frozenset({'created_at', 'updated_at'})),
            Step('conversation.get',
                 {'conv_id': f'{c}_d3', 'user_id': user},
                 ignore_fields=frozenset({'created_at', 'updated_at'})),
            Step('conversation.delete', {'conv_id': c, 'user_id': user},
                 command=True),
            Step('conversation.delete',
                 {'conv_id': f'{c}_d1', 'user_id': user}, command=True),
            Step('conversation.delete',
                 {'conv_id': f'{c}_d3', 'user_id': user}, command=True),
            Step('conversation.count', {'user_id': user}),
        ]),
        'conversation restore revives a trashed header': build(lambda c: [
            # Missing trash rows report, never raise.
            Step('conversation.restore', {
                'conv_id': f'{c}_absent', 'user_id': user,
            }, command=True),
            Step('conversation.create', {
                'conv_id': c, 'user_id': user, 'title': 'Revive',
                'settings': {'a': 1},
                'created_at': _CONV_TS, 'updated_at': _CONV_TS,
            }, command=True),
            # A live id is a conflict, not a restore.
            Step('conversation.restore', {'conv_id': c, 'user_id': user},
                 command=True),
            Step('conversation.delete', {'conv_id': c, 'user_id': user},
                 command=True),
            Step('conversation.restore', {'conv_id': c, 'user_id': user},
                 command=True),
            Step('conversation.get', {'conv_id': c, 'user_id': user}),
            Step('conversation.count', {'user_id': user}),
            Step('conversation.delete', {'conv_id': c, 'user_id': user},
                 command=True),
        ]),
        'conversation purge removes active and trashed rows': build(
            lambda c: [
                Step('conversation.purge',
                     {'conv_id': f'{c}_absent', 'user_id': user},
                     command=True),
                Step('conversation.create', {
                    'conv_id': c, 'user_id': user, 'title': 'Purg',
                    'created_at': _CONV_TS, 'updated_at': _CONV_TS,
                }, command=True),
                # Purging a live conversation is permanent.
                Step('conversation.purge', {'conv_id': c, 'user_id': user},
                     command=True),
                Step('conversation.get', {'conv_id': c, 'user_id': user}),
                Step('conversation.purge', {'conv_id': c, 'user_id': user},
                     command=True),
                # The purged id is immediately reusable.
                Step('conversation.create', {
                    'conv_id': c, 'user_id': user, 'title': 'Purg2',
                    'created_at': _CONV_TS, 'updated_at': _CONV_TS,
                }, command=True),
                # A trashed conversation purges out of the trash table.
                Step('conversation.delete', {'conv_id': c, 'user_id': user},
                     command=True),
                Step('conversation.purge', {'conv_id': c, 'user_id': user},
                     command=True),
                # Nothing remains to restore.
                Step('conversation.restore',
                     {'conv_id': c, 'user_id': user}, command=True),
                Step('conversation.count', {'user_id': user}),
            ]
        ),
        'conversation trash.prune sweeps the oldest page': build(lambda c: [
            # Reset sweeps: legacy trash residue accumulates across the
            # module-scoped authority while each tofu-db authority starts
            # empty, so sweep counts legitimately differ (compare=False).
            Step('conversation.trash.prune', {
                'deleted_before_ms': _FAR_FUTURE_MS, 'max_conversations': 64,
            }, command=True, compare=False),
            Step('conversation.trash.prune', {
                'deleted_before_ms': _FAR_FUTURE_MS, 'max_conversations': 64,
            }, command=True, compare=False),
            Step('conversation.trash.prune', {
                'deleted_before_ms': _FAR_FUTURE_MS, 'max_conversations': 64,
            }, command=True, compare=False),
            Step('conversation.create', {
                'conv_id': f'{c}_a', 'user_id': user, 'title': 'T1',
                'created_at': _CONV_TS, 'updated_at': _CONV_TS,
            }, command=True),
            Step('conversation.create', {
                'conv_id': f'{c}_b', 'user_id': user, 'title': 'T2',
                'created_at': _CONV_TS, 'updated_at': _CONV_TS,
            }, command=True),
            Step('conversation.delete',
                 {'conv_id': f'{c}_a', 'user_id': user}, command=True),
            Step('conversation.delete',
                 {'conv_id': f'{c}_b', 'user_id': user}, command=True),
            # With an empty baseline the sweep retires exactly these two.
            Step('conversation.trash.prune', {
                'deleted_before_ms': _FAR_FUTURE_MS, 'max_conversations': 10,
            }, command=True),
            # Cutoff is required; page size is bounded.
            Step('conversation.trash.prune', {}, command=True),
            Step('conversation.trash.prune', {
                'deleted_before_ms': _FAR_FUTURE_MS, 'max_conversations': 0,
            }, command=True),
            Step('conversation.trash.prune', {
                'deleted_before_ms': _FAR_FUTURE_MS, 'max_conversations': 65,
            }, command=True),
        ]),
        'conversation search validation and empty projection': [
            # Queries shorter than two characters short-circuit to empty.
            Step('conversation.search', {'user_id': user, 'query': 'a'}),
            # No settled-turn search projections exist in this suite, so
            # nothing can match (snippet ranking is deferred until the
            # turn.* domain can seed projections).
            Step('conversation.search', {'user_id': user, 'query': 'hello'}),
            Step('conversation.search', {
                'user_id': user, 'query': 'hello world', 'limit': 5,
                'snippet_radius': 10,
            }),
            # Keep the protocol failure last: the legacy client's circuit
            # breaker deliberately suppresses later requests after repeated
            # application errors, which is not search semantics.
            Step('conversation.search', {'user_id': user}),
        ],
        'conversation activity_dates counts candidates': build(lambda c: [
            # The cutoff is required and boundaries must strictly increase.
            Step('conversation.activity_dates', {
                'user_id': user, 'day_boundaries_ms': [1, 2],
            }),
            Step('conversation.activity_dates', {
                'user_id': user, 'updated_at_gte': 0,
                'day_boundaries_ms': [5],
            }),
            Step('conversation.activity_dates', {
                'user_id': user, 'updated_at_gte': 0,
                'day_boundaries_ms': [3, 3],
            }),
            Step('conversation.create', {
                'conv_id': f'{c}_a', 'user_id': user, 'title': 'A',
                'created_at': _CONV_TS, 'updated_at': _CONV_TS,
            }, command=True),
            Step('conversation.create', {
                'conv_id': f'{c}_b', 'user_id': user, 'title': 'B',
                'created_at': _CONV_TS + 1000,
                'updated_at': _CONV_TS + 1000,
            }, command=True),
            Step('conversation.activity_dates', {
                'user_id': user, 'updated_at_gte': _CONV_TS,
                'day_boundaries_ms': [_CONV_TS - 10, _CONV_TS + 500,
                                      _CONV_TS + 2000],
            }),
            Step('conversation.activity_dates', {
                'user_id': user, 'updated_at_gte': _CONV_TS,
                'created_at_lt': _CONV_TS + 1,
                'day_boundaries_ms': [_CONV_TS - 10, _CONV_TS + 500],
            }),
            Step('conversation.delete',
                 {'conv_id': f'{c}_a', 'user_id': user}, command=True),
            Step('conversation.delete',
                 {'conv_id': f'{c}_b', 'user_id': user}, command=True),
        ]),
    }


def _artifact_scripts() -> dict[str, list[Step]]:
    conversation_id = f'artifact_{uuid.uuid4().hex[:12]}'
    first_id = f'{conversation_id}_v1'
    duplicate_id = f'{conversation_id}_duplicate'
    second_id = f'{conversation_id}_v2'
    base = {
        'conv_id': conversation_id,
        'task_id': 'task-a',
        'msg_id': 'message-a',
        'source': 'write_file',
        'format': 'markdown',
        'title': 'report.md',
        'source_ref': {'path': 'report.md'},
        'meta': {'words': 2},
    }
    return {
        'artifact dedupe versions library pin delete lifecycle': [
            Step('artifact.create', {
                **base, 'artifact_id': first_id,
                'content': '# first\n', 'created_at': 100,
            }, command=True),
            Step('artifact.create', {
                **base, 'artifact_id': duplicate_id,
                'content': '# first\n', 'created_at': 101,
            }, command=True),
            Step('artifact.create', {
                **base, 'artifact_id': second_id,
                'content': '# second\n', 'created_at': 102,
            }, command=True),
            Step('artifact.get', {
                'artifact_id': second_id, 'include_content': True,
            }),
            Step('artifact.versions', {'artifact_id': second_id}),
            Step('artifact.pin', {
                'artifact_id': first_id, 'pinned': True,
            }, command=True),
            Step('artifact.library', {'limit': 20}),
            Step('artifact.delete', {
                'artifact_id': second_id, 'deleted_at': 200,
            }, command=True),
            Step('artifact.get', {
                'artifact_id': second_id, 'include_content': False,
            }),
            Step('artifact.list', {
                'conv_id': conversation_id, 'include_deleted': True,
            }),
        ],
    }


_TURN_TS = 1_800_000_000_000


def _turn_scripts() -> dict[str, list[Step]]:
    """Turn, attempt, branch and conversation-sync domain.

    Every identity and clock is pinned through the payload so transcript,
    revision and sync-sequence arithmetic stay exactly comparable.  Two
    server-generated values remain runtime-random on both authorities —
    attempt ids (uuid4 in both engines) and branch lane ids (random in
    legacy, command-derived in Tofu-DB) — so steps reference them through
    ``_Ref`` and the gate tokenizes them per run before comparison.
    """
    user = _OWNER_ID

    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        return make(f'diff_{uuid.uuid4().hex[:8]}')

    def defaults() -> dict[str, Any]:
        return {
            'allowCreate': True,
            'title': 'Turn scripts',
            'createdAt': _TURN_TS,
            'settings': {'theme': 'dark', 'flags': ['a', 'b']},
        }

    def append(
        conv: str, turn: str, ordinal: int, *, actor: str = 'human',
        lane: Any = 'main', content: Any = None,
    ) -> Step:
        at = _TURN_TS + 1000 * (ordinal + 1)
        return Step('turn.append_settled', {
            'conversation_id': conv,
            'user_id': user,
            'actor': actor,
            'status': 'completed',
            'projection': {
                'content': f'body of {turn}' if content is None else content,
            },
            'lane_id': lane,
            'command_id': f'{turn}_command',
            'turn_id': turn,
            'created_at': at,
            'now': at,
            'conversation_defaults': defaults(),
        }, command=True)

    def read(
        operation: str, conv: str,
        ignore: frozenset[str] = frozenset(), **extra: Any,
    ) -> Step:
        return Step(
            operation, {'conversation_id': conv, 'user_id': user, **extra},
            ignore_fields=ignore,
        )

    # Wall-clock fields refreshed by edit/branch mutations on both sides:
    # edit steps bump updatedAt; branch lanes add a wall-clock createdAt
    # inside the parent projection's _branchLanes descriptor.
    edited = frozenset({'updatedAt'})
    branched = frozenset({'updatedAt', 'createdAt'})
    attempt_clock = frozenset({'updatedAt', 'createdAt'})
    recovery_clock = attempt_clock | frozenset({'settledAt', 'occurredAt'})
    visible_event_fields = attempt_clock | frozenset({
        'startedAt',
        # Physical encoded projection bytes differ by authority codec.
        'projectionBytes',
    })
    dispatch_fields = attempt_clock | frozenset({'taskId', 'idempotencyKey'})
    timing_fields = frozenset({'created_at', 'settled_at', 'task_id'})
    dispatch_now = int(time.time() * 1000)

    return {
        'turn create pair commits one revision and replays by command': build(lambda ns: [
            Step('turn.create_pair', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'command_id': f'{ns}_pair_command',
                'input_projection': {'content': 'hello pair'},
                'config': {'model': 'differential'},
                'now': _TURN_TS + 1000,
                'conversation_defaults': defaults(),
            }, command=True),
            Step('turn.create_pair', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'command_id': f'{ns}_pair_command',
                'input_projection': {'content': 'hello pair'},
                'config': {'model': 'differential'},
                'now': _TURN_TS + 1000,
                'conversation_defaults': defaults(),
            }, command=True),
            Step('turn.create_pair', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'command_id': f'{ns}_second_pair_command',
                'input_projection': {'content': 'must conflict'},
                'now': _TURN_TS + 2000,
            }, command=True),
            read('turn.list', f'{ns}_conv'),
            read('turn.revision', f'{ns}_conv'),
            Step('turn.events.list', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user,
            }),
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'after': 0, 'limit': 10,
            }),
        ]),
        'turn create pair binds an exact durable queue row': build(lambda ns: [
            Step('turn.create_pair', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'command_id': f'{ns}_queued_pair_command',
                'input_projection': 'queued pair',
                'config': {'model': 'queued-differential'},
                'queue_binding': {
                    'queueId': f'{ns}_queue', 'kind': 'real',
                    'priority': 7, 'createdAt': _TURN_TS + 1000,
                },
                'now': _TURN_TS + 1000,
                'conversation_defaults': defaults(),
            }, command=True),
            read('turn.list', f'{ns}_conv'),
            Step('queue.list', {
                'conv_id': f'{ns}_conv', 'user_id': user,
            }),
            Step('turn.attempt.get', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user,
            }),
        ]),
        'turn queued pair activates atomically into the live lane': build(lambda ns: [
            Step('turn.create_pair', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'command_id': f'{ns}_queued_pair_command',
                'input_projection': {'content': 'activate queued pair'},
                'config': {'model': 'queued-differential'},
                'dispatch_mode': 'conversation_executor',
                'queue_binding': {
                    'queueId': f'{ns}_queue', 'kind': 'real',
                    'priority': 7, 'createdAt': _TURN_TS + 1000,
                },
                'now': _TURN_TS + 1000,
                'conversation_defaults': defaults(),
            }, command=True),
            Step('turn.queue.activate', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'queue_id': f'{ns}_queue',
            }, command=True),
            Step('queue.list', {
                'conv_id': f'{ns}_conv', 'user_id': user,
            }),
            Step('turn.attempt.get', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user,
            }),
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'after': 0, 'limit': 10,
            }),
        ]),
        'turn queued pair cancellation deletes only the never started pair': build(lambda ns: [
            Step('turn.create_pair', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'command_id': f'{ns}_queued_pair_command',
                'input_projection': {'content': 'cancel queued pair'},
                'config': {'model': 'queued-differential'},
                'queue_binding': {
                    'queueId': f'{ns}_queue', 'kind': 'real',
                    'priority': 7, 'createdAt': _TURN_TS + 1000,
                },
                'now': _TURN_TS + 1000,
                'conversation_defaults': defaults(),
            }, command=True),
            Step('turn.queue.cancel', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'queue_id': f'{ns}_queue',
            }, command=True),
            Step('queue.list', {
                'conv_id': f'{ns}_conv', 'user_id': user,
            }),
            read('turn.list', f'{ns}_conv'),
            Step('turn.attempt.get', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user,
            }),
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'after': 0, 'limit': 10,
            }),
        ]),
        'turn steer commit atomically appends one live operator injection': build(lambda ns: [
            Step('turn.create_pair', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'command_id': f'{ns}_pair_command',
                'input_projection': {'content': 'steer this response'},
                'config': {'model': 'steer-differential'},
                'now': _TURN_TS + 1000,
                'conversation_defaults': defaults(),
            }, command=True),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'task_id': f'{ns}_task', 'user_id': user,
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.steer.commit', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'command_id': f'{ns}_steer_command',
                'text': 'focus on the atomic durable boundary',
            }, command=True, ignore_fields=edited),
            read(
                'turn.get', f'{ns}_conv', edited,
                turn_id=_Ref(0, ('turn', 'turnId')),
            ),
            read('turn.revision', f'{ns}_conv'),
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'after': 0, 'limit': 10,
            }, ignore_fields=edited),
        ]),
        'turn related announce bridges one live root revision and replay event': build(lambda ns: [
            Step('turn.create_pair', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'command_id': f'{ns}_pair_command',
                'input_projection': {'content': 'related input'},
                'config': {'model': 'related-differential'},
                'now': _TURN_TS + 1000,
                'conversation_defaults': defaults(),
            }, command=True),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'task_id': f'{ns}_task', 'user_id': user,
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.related.announce', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'turn_ids': [_Ref(0, ('submittedTurn', 'turnId'))],
                'user_id': user,
            }, command=True),
            read(
                'turn.get', f'{ns}_conv', edited,
                turn_id=_Ref(0, ('turn', 'turnId')),
            ),
            Step('turn.events.list', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user, 'projection_mode': 'patch',
            }, ignore_fields=edited),
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'after': 0, 'limit': 10,
            }, ignore_fields=edited),
        ]),
        'turn visible sync commits flow phases with stable tool segments': build(lambda ns: [
            Step('turn.create_pair', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'command_id': f'{ns}_pair_command',
                'input_projection': {'content': 'visible input'},
                'config': {'model': 'visible-differential'},
                'now': _TURN_TS + 1000,
                'conversation_defaults': defaults(),
            }, command=True),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'task_id': f'{ns}_task', 'user_id': user,
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.event.record', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'task_id': f'{ns}_task', 'user_id': user,
                'projection': {
                    'content': 'live root', 'thinking': '',
                    'toolRounds': [{
                        'roundNum': 1, 'toolCallId': f'{ns}_tool',
                        'toolName': 'run_command',
                        'toolArgs': {'cmd': 'make test'},
                        'toolContent': 'passed', 'status': 'done',
                    }],
                },
            }, command=True, ignore_fields=edited),
            Step('turn.visible.sync', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'root_turn_id': _Ref(0, ('turn', 'turnId')),
                'default_kind': 'flow_node', 'run_id': f'{ns}_run',
                'messages': [
                    {
                        'role': 'assistant', 'content': 'planner result',
                        'thinking': '', '_isFlowPlanner': True,
                        '_flowIteration': 2, 'model': 'visible-model',
                        'provider_id': 'visible-provider',
                        'toolRounds': [{
                            'roundNum': 1, 'toolCallId': f'{ns}_tool',
                            'toolName': 'run_command', 'status': 'done',
                        }],
                    },
                    {
                        'role': 'user', 'content': 'review this',
                        '_isFlowReview': True, '_flowIteration': 2,
                        '_flowApproved': False,
                    },
                ],
            }, command=True, ignore_fields=edited),
            read('turn.list', f'{ns}_conv', attempt_clock),
            Step('turn.events.list', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user, 'projection_mode': 'patch',
            }, ignore_fields=visible_event_fields),
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'after': 0, 'limit': 10,
            }, ignore_fields=visible_event_fields),
        ]),
        'turn append get exists list revision round trip': build(lambda ns: [
            read('turn.exists', f'{ns}_conv'),
            read('turn.get', f'{ns}_conv', turn_id=f'{ns}_t1'),
            read('turn.revision', f'{ns}_conv'),
            append(f'{ns}_conv', f'{ns}_t1', 0),
            append(f'{ns}_conv', f'{ns}_t2', 1, content='回答 🚀'),
            read('turn.exists', f'{ns}_conv'),
            read('turn.get', f'{ns}_conv', turn_id=f'{ns}_t1'),
            read('turn.get', f'{ns}_conv', turn_id=f'{ns}_missing'),
            read('turn.list', f'{ns}_conv'),
            read('turn.list', f'{ns}_conv', lane_id='main'),
            read('turn.list', f'{ns}_conv', lane_id='side'),
            read('turn.revision', f'{ns}_conv'),
        ]),
        'turn legacy image read is owner revision and index scoped': build(lambda ns: [
            Step('turn.append_settled', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'actor': 'assistant', 'status': 'completed',
                'projection': {
                    'content': 'historical image',
                    'images': [
                        {
                            'base64': 'aGVsbG8=',
                            'preview': 'data:image/png;base64,aGVsbG8=',
                            'mediaType': ' IMAGE/PNG ',
                        },
                        {'preview': 'data:image/webp;base64,d2VicA=='},
                    ],
                },
                'lane_id': 'main', 'command_id': f'{ns}_image_command',
                'turn_id': f'{ns}_image_turn', 'created_at': _TURN_TS + 1000,
                'now': _TURN_TS + 1000,
                'conversation_defaults': defaults(),
            }, command=True),
            read(
                'turn.image.get', f'{ns}_conv',
                turn_id=f'{ns}_image_turn', projection_revision=1,
                image_index=0,
            ),
            read(
                'turn.image.get', f'{ns}_conv',
                turn_id=f'{ns}_image_turn', projection_revision=1,
                image_index=1,
            ),
            read(
                'turn.image.get', f'{ns}_conv',
                turn_id=f'{ns}_image_turn', projection_revision=2,
                image_index=0,
            ),
            read(
                'turn.image.get', f'{ns}_conv',
                turn_id=f'{ns}_image_turn', projection_revision=1,
                image_index=2,
            ),
        ]),
        'turn append validation and duplicate identity': build(lambda ns: [
            Step('turn.append_settled', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'actor': 'nobody', 'command_id': f'{ns}_bad_actor',
            }, command=True),
            Step('turn.append_settled', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'actor': 'human', 'status': 'running',
                'command_id': f'{ns}_bad_status',
            }, command=True),
            Step('turn.append_settled', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'actor': 'human', 'projection': 'not-an-object',
                'command_id': f'{ns}_bad_projection',
            }, command=True),
            append(f'{ns}_conv', f'{ns}_t1', 0),
            # Re-using one turn identity is rejected by both authorities
            # with database_conflict (legacy maps the duplicate claim
            # before the SQLite primary key can leak its class).
            Step('turn.append_settled', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'actor': 'human', 'status': 'completed',
                'projection': {'content': 'duplicate'},
                'lane_id': 'main', 'command_id': f'{ns}_t1_duplicate',
                'turn_id': f'{ns}_t1',
                'created_at': _TURN_TS + 2000, 'now': _TURN_TS + 2000,
                'conversation_defaults': defaults(),
            }, command=True),
            read('turn.list', f'{ns}_conv'),
        ]),
        'turn attempt identity resolves per run': build(lambda ns: [
            append(f'{ns}_conv', f'{ns}_t1', 0, actor='assistant'),
            Step('turn.attempt.get', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user,
            }),
            Step('turn.events.list', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user, 'projection_mode': 'patch',
            }),
            Step('turn.events.list', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user, 'after': 1, 'limit': 1,
                'projection_mode': 'patch',
            }),
            Step('turn.attempt.get', {
                'attempt_id': f'{ns}_missing', 'user_id': user,
            }),
            Step('turn.events.list', {
                'attempt_id': f'{ns}_missing', 'user_id': user,
            }),
            Step('turn.attempt.claim', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user,
                'dispatch_owner_id': 'differential-worker',
            }, command=True),
            Step('turn.attempt.claim', {
                'attempt_id': f'{ns}_missing', 'user_id': user,
            }, command=True),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'task_id': f'{ns}_terminal_task', 'user_id': user,
                'dispatch_owner_id': 'differential-worker',
            }, command=True),
            Step('turn.events.list', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user, 'projection_mode': 'patch',
            }),
            Step('turn.attempt.start', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'task_id': f'{ns}_terminal_task', 'user_id': user,
            }, command=True),
            Step('turn.events.list', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'user_id': user, 'after': 1, 'limit': 1,
                'projection_mode': 'patch',
            }),
            read('turn.get', f'{ns}_conv', turn_id=f'{ns}_t1'),
            read('turn.list', f'{ns}_conv'),
            # An assistant Turn replay event carries the same top-level
            # turnId/attemptId routing identity as the legacy envelope.
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'after': 0,
            }),
        ]),
        'turn attempt regenerate creates and replays atomically': build(lambda ns: [
            append(f'{ns}_conv', f'{ns}_human', 0),
            append(f'{ns}_conv', f'{ns}_assistant', 1, actor='assistant'),
            Step('turn.attempt.create', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_assistant',
                'command_id': f'{ns}_regenerate',
                'operation': 'regenerate',
                'target_actor': 'assistant', 'target_kind': 'reply',
                'expected_projection_revision': 1,
                'config': {'temperature': 0.25},
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.attempt.create', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_assistant',
                'command_id': f'{ns}_regenerate',
                'operation': 'regenerate',
                'target_actor': 'assistant', 'target_kind': 'reply',
                'expected_projection_revision': 1,
                'config': {'temperature': 0.25},
            }, command=True, ignore_fields=attempt_clock),
            read('turn.get', f'{ns}_conv', ignore=attempt_clock,
                 turn_id=f'{ns}_assistant'),
            Step('turn.attempt.get', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'user_id': user,
            }, ignore_fields=attempt_clock),
            Step('turn.events.list', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'user_id': user, 'projection_mode': 'patch',
            }, ignore_fields=attempt_clock),
        ]),
        'turn attempt global discovery dispatches one durable worker job': build(lambda ns: [
            append(f'{ns}_conv', f'{ns}_assistant', 0, actor='assistant'),
            Step('turn.attempt.create', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_assistant',
                'command_id': f'{ns}_dispatch_create',
                'operation': 'regenerate',
                'dispatch_mode': 'conversation_executor',
                'target_actor': 'assistant', 'target_kind': 'reply',
                'expected_projection_revision': 1,
                'config': {'model': 'dispatch-model'},
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.attempt.dispatchable.list', {
                'created_before_ms': 9_223_372_036_854_775_000,
                'limit': 8,
            }, ignore_fields=attempt_clock),
            Step('turn.attempt.dispatch_worker', {
                'attempt_id': _Ref(1, ('attempt', 'attemptId')),
                'user_id': user,
                'principal': {
                    'kind': 'user',
                    'subject_id': 'differential-user',
                    'owner_user_id': user,
                    'tenant_id': 'differential-tenant',
                    'scopes': ['chat:read', 'chat:write'],
                },
                'priority': 7,
                'now_ms': dispatch_now,
            }, command=True, ignore_fields=dispatch_fields),
            Step('turn.attempt.dispatch_worker', {
                'attempt_id': _Ref(1, ('attempt', 'attemptId')),
                'user_id': user,
                'principal': {
                    'kind': 'user',
                    'subject_id': 'differential-user',
                    'owner_user_id': user,
                    'tenant_id': 'differential-tenant',
                    'scopes': ['chat:read', 'chat:write'],
                },
                'priority': 7,
                'now_ms': dispatch_now,
            }, command=True, ignore_fields=dispatch_fields),
            Step('turn.attempt.dispatchable.list', {
                'created_before_ms': 9_223_372_036_854_775_000,
            }),
            Step('worker_job.get', {
                'task_id': _Ref(3, ('job', 'taskId')),
                'user_id': user,
            }, ignore_fields=dispatch_fields),
            Step('turn.timing_trace.list', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'limit': 10,
            }, ignore_fields=timing_fields),
            Step('turn.timing_trace.get', {
                'task_id': _Ref(3, ('job', 'taskId')), 'user_id': user,
            }, ignore_fields=frozenset({'taskId'})),
        ]),
        'turn perception receipt is idempotent without revision churn': build(lambda ns: [
            append(f'{ns}_conv', f'{ns}_human', 0),
            append(f'{ns}_conv', f'{ns}_assistant', 1, actor='assistant'),
            Step('turn.attempt.create', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_assistant',
                'command_id': f'{ns}_perception_attempt',
                'operation': 'regenerate',
                'target_actor': 'assistant', 'target_kind': 'reply',
                'expected_projection_revision': 1,
                'config': {},
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'task_id': f'{ns}_perception_task', 'user_id': user,
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.perception.record', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_assistant',
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'observation': {
                    'observationId': 'paint:terminal:1',
                    'attemptId': _Ref(2, ('attempt', 'attemptId')),
                    'kind': 'terminal_painted', 'clientId': 'diff-page',
                    'serverEmittedAt': 1_000, 'receivedAt': 1_125,
                    'paintedAt': 1_160, 'projectionRevision': 2,
                    'visibility': 'visible',
                },
            }, command=True),
            Step('turn.perception.record', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_assistant',
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'observation': {
                    'observationId': 'paint:terminal:1',
                    'attemptId': _Ref(2, ('attempt', 'attemptId')),
                    'kind': 'terminal_painted', 'clientId': 'diff-page',
                    'serverEmittedAt': 1_000, 'receivedAt': 1_125,
                    'paintedAt': 1_160, 'projectionRevision': 2,
                    'visibility': 'visible',
                },
            }, command=True),
            Step('turn.timing_trace.get', {
                'task_id': f'{ns}_perception_task', 'user_id': user,
            }, ignore_fields=frozenset({'recordedAt'})),
            read('turn.get', f'{ns}_conv', ignore=attempt_clock,
                 turn_id=f'{ns}_assistant'),
        ]),
        'turn event record advances live and terminal authority': build(lambda ns: [
            append(f'{ns}_conv', f'{ns}_human', 0),
            append(f'{ns}_conv', f'{ns}_assistant', 1, actor='assistant'),
            Step('turn.attempt.create', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_assistant',
                'command_id': f'{ns}_event_attempt',
                'operation': 'regenerate',
                'target_actor': 'assistant', 'target_kind': 'reply',
                'expected_projection_revision': 1,
                'config': {},
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'task_id': f'{ns}_event_task', 'user_id': user,
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.event.record', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'task_id': f'{ns}_event_task', 'user_id': user,
                'slim': True, 'content': 'live answer',
                'thinking': 'live reasoning',
                'event_payload': {
                    'phase': 'generating',
                    'projection': {'mustNotPersistTwice': True},
                },
                'now': _TURN_TS + 3_000,
            }, command=True, ignore_fields=attempt_clock),
            read('turn.get', f'{ns}_conv', ignore=attempt_clock,
                 turn_id=f'{ns}_assistant'),
            Step('turn.events.list', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'user_id': user, 'projection_mode': 'patch',
            }, ignore_fields=attempt_clock),
            Step('turn.event.record', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'task_id': f'{ns}_event_task', 'user_id': user,
                'terminal': True, 'status': 'completed', 'slim': True,
                'content': 'final answer', 'thinking': 'done',
                'settlement': {
                    'outcome': 'completed', 'cause': 'finished',
                    'resumeOptions': [],
                },
                'event_payload': {'status': 'completed', 'phase': None},
                'now': _TURN_TS + 4_000,
            }, command=True, ignore_fields=attempt_clock),
            read('turn.get', f'{ns}_conv', ignore=attempt_clock,
                 turn_id=f'{ns}_assistant'),
            Step('turn.attempt.get', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'user_id': user,
            }, ignore_fields=attempt_clock),
        ]),
        'turn replay and attempt event retention advance in bounded order': build(lambda ns: [
            # The legacy fixture retains one authority across parameterized
            # scripts, whereas tofu-db gets a fresh authority per case.  The
            # owner-global maintenance counts and first victim therefore are
            # not cross-backend comparable here; the final target replay and
            # permanent Turn remain exact comparison points.  Rust unit tests
            # pin the isolated per-call progress payloads.
            append(f'{ns}_conv', f'{ns}_human', 0),
            append(f'{ns}_conv', f'{ns}_assistant', 1, actor='assistant'),
            Step('turn.attempt.create', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_assistant',
                'command_id': f'{ns}_retention_attempt',
                'operation': 'regenerate', 'target_actor': 'assistant',
                'target_kind': 'reply', 'expected_projection_revision': 1,
                'config': {},
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'task_id': f'{ns}_retention_task', 'user_id': user,
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.event.record', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'task_id': f'{ns}_retention_task', 'user_id': user,
                'slim': True, 'content': 'partial', 'thinking': 'working',
                'now': _TURN_TS + 3_000,
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.event.record', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'task_id': f'{ns}_retention_task', 'user_id': user,
                'terminal': True, 'status': 'completed', 'slim': True,
                'content': 'durable answer', 'thinking': 'done',
                'settlement': {
                    'outcome': 'completed', 'cause': 'finished',
                    'resumeOptions': [],
                },
                'now': _TURN_TS + 4_000,
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.sync.prune', {
                'user_id': user, 'created_before_ms': _FAR_FUTURE_MS,
                'max_rows': 2,
            }, command=True, compare=False),
            Step('turn.sync.prune', {
                'user_id': user, 'created_before_ms': _FAR_FUTURE_MS,
                'max_rows': 20_000,
            }, command=True, compare=False),
            Step('turn.events.prune', {
                'user_id': user, 'settled_before_ms': _FAR_FUTURE_MS,
                'max_attempts': 16, 'max_rows': 1,
            }, command=True, compare=False),
            Step('turn.events.list', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'user_id': user, 'projection_mode': 'patch',
            }, ignore_fields=attempt_clock, compare=False),
            Step('turn.events.prune', {
                'user_id': user, 'settled_before_ms': _FAR_FUTURE_MS,
                'max_attempts': 256, 'max_rows': 200_000,
            }, command=True, compare=False),
            Step('turn.events.list', {
                'attempt_id': _Ref(2, ('attempt', 'attemptId')),
                'user_id': user, 'projection_mode': 'patch',
            }),
            read('turn.get', f'{ns}_conv', ignore=attempt_clock,
                 turn_id=f'{ns}_assistant'),
        ]),
        'turn recovery is chunked guarded and replay visible': build(lambda ns: [
            # Quiesce attempts left recoverable by earlier scripts before
            # seeding: the legacy authority is module-scoped and shared
            # while the Tofu-DB daemon is fresh per script, so unscoped
            # recovery counts are only deterministic once foreign leftovers
            # are drained (compare=False; their owning scripts have already
            # finished, and three default chunks cover far more than the
            # current leftover population).
            Step('turn.recover', {'user_id': user}, command=True,
                 compare=False),
            Step('turn.recover', {'user_id': user}, command=True,
                 compare=False),
            Step('turn.recover', {'user_id': user}, command=True,
                 compare=False),
            append(f'{ns}_conv_1', f'{ns}_assistant_1', 0, actor='assistant'),
            Step('turn.attempt.create', {
                'conversation_id': f'{ns}_conv_1', 'user_id': user,
                'turn_id': f'{ns}_assistant_1', 'command_id': f'{ns}_attempt_1',
                'operation': 'regenerate', 'expected_projection_revision': 1,
                'target_actor': 'assistant', 'target_kind': 'reply',
                'config': {'model': 'gpt-4o'},
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(4, ('attempt', 'attemptId')),
                'task_id': f'{ns}_task_1', 'user_id': user,
            }, command=True, ignore_fields=attempt_clock),
            append(f'{ns}_conv_2', f'{ns}_assistant_2', 0, actor='assistant'),
            Step('turn.attempt.create', {
                'conversation_id': f'{ns}_conv_2', 'user_id': user,
                'turn_id': f'{ns}_assistant_2', 'command_id': f'{ns}_attempt_2',
                'operation': 'regenerate', 'expected_projection_revision': 1,
                'target_actor': 'assistant', 'target_kind': 'reply',
                'config': {'model': 'gpt-4o'},
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(7, ('attempt', 'attemptId')),
                'task_id': f'{ns}_task_2', 'user_id': user,
            }, command=True, ignore_fields=attempt_clock),
            append(f'{ns}_conv_3', f'{ns}_assistant_3', 0, actor='assistant'),
            Step('turn.attempt.create', {
                'conversation_id': f'{ns}_conv_3', 'user_id': user,
                'turn_id': f'{ns}_assistant_3', 'command_id': f'{ns}_attempt_3',
                'operation': 'regenerate', 'expected_projection_revision': 1,
                'target_actor': 'assistant', 'target_kind': 'reply',
                'config': {'model': 'gpt-4o'},
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(10, ('attempt', 'attemptId')),
                'task_id': f'{ns}_task_3', 'user_id': user,
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.recover', {'user_id': user, 'max_rows': 1}, command=True),
            Step('turn.recover', {'user_id': user, 'max_rows': 1}, command=True),
            Step('turn.recover', {
                'user_id': user, 'exclude_task_ids': [f'{ns}_task_3'],
            }, command=True),
            Step('turn.attempt.get', {
                'attempt_id': _Ref(10, ('attempt', 'attemptId')), 'user_id': user,
            }, ignore_fields=recovery_clock),
            Step('turn.recover', {
                'user_id': user, 'created_before_ms': 1,
            }, command=True),
            Step('turn.recover', {'user_id': user}, command=True),
            Step('turn.attempt.get', {
                'attempt_id': _Ref(10, ('attempt', 'attemptId')), 'user_id': user,
            }, ignore_fields=recovery_clock),
            Step('turn.events.list', {
                'attempt_id': _Ref(10, ('attempt', 'attemptId')),
                'user_id': user, 'projection_mode': 'patch',
            }, ignore_fields=recovery_clock),
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv_3', 'user_id': user, 'after': 0,
            }, ignore_fields=recovery_clock),
            Step('turn.recover', {
                'user_id': user, 'max_bytes': 8 * 1024 * 1024 + 1,
            }, command=True),
            append(f'{ns}_conv_4', f'{ns}_assistant_4', 0, actor='assistant'),
            Step('turn.attempt.create', {
                'conversation_id': f'{ns}_conv_4', 'user_id': user,
                'turn_id': f'{ns}_assistant_4', 'command_id': f'{ns}_attempt_4',
                'operation': 'regenerate', 'expected_projection_revision': 1,
                'target_actor': 'assistant', 'target_kind': 'reply',
                'config': {'model': 'gpt-4o'},
            }, command=True, ignore_fields=attempt_clock),
            Step('turn.attempt.claim', {
                'attempt_id': _Ref(23, ('attempt', 'attemptId')),
                'user_id': user, 'dispatch_owner_id': 'recovery-worker',
            }, command=True),
            Step('turn.recover', {'user_id': user}, command=True),
            Step('turn.attempt.get', {
                'attempt_id': _Ref(23, ('attempt', 'attemptId')), 'user_id': user,
            }, ignore_fields=recovery_clock | frozenset({'taskId'})),
        ]),
        'turn compaction folds history and preserves a live tail': build(lambda ns: [
            append(f'{ns}_conv', f'{ns}_t0', 0),
            append(f'{ns}_conv', f'{ns}_t1', 1),
            append(f'{ns}_conv', f'{ns}_t2', 2),
            append(f'{ns}_conv', f'{ns}_t3', 3),
            Step('turn.compact', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'expected_conversation_revision': _Ref(3, ('conversationRevision',)),
                'summary_turn_id': f'{ns}_summary',
                'summary_projection': {
                    'content': 'Earlier context summary', 'thinking': '',
                    'segments': [], 'toolRounds': [],
                    'compaction': {'blockId': 'compaction', 'sourceTurns': 2},
                },
                'delete_turn_ids': [f'{ns}_t1', f'{ns}_t2', f'{ns}_t1'],
                'projection_updates': [{
                    'turn_id': f'{ns}_t3', 'expected_projection_revision': 1,
                    'projection': {
                        'content': 'retained tail', 'thinking': '',
                        'segments': [], 'toolRounds': [],
                    },
                }],
                'insert_after_turn_id': f'{ns}_t0',
                'insert_before_turn_id': f'{ns}_t3',
            }, command=True, ignore_fields=attempt_clock),
            read('turn.list', f'{ns}_conv', ignore=attempt_clock),
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv', 'user_id': user, 'after': 4,
            }, ignore_fields=recovery_clock),
            Step('turn.compact', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'expected_conversation_revision': 4,
                'summary_turn_id': f'{ns}_stale_summary',
                'summary_projection': {
                    'content': 'stale',
                    'compaction': {'blockId': 'compaction'},
                },
                'delete_turn_ids': [],
                'projection_updates': [],
                'insert_before_turn_id': f'{ns}_t0',
            }, command=True),
        ]),
        'turn projection update CAS and coercion': build(lambda ns: [
            append(f'{ns}_conv', f'{ns}_t1', 0),
            Step('turn.projection.update', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_t1', 'expected_projection_revision': 1,
                'projection': {'content': 'edited', 'tags': ['x', 'y']},
            }, command=True, ignore_fields=edited),
            read('turn.get', f'{ns}_conv', ignore=edited,
                 turn_id=f'{ns}_t1'),
            Step('turn.projection.update', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_t1', 'expected_projection_revision': 1,
                'projection': {'content': 'stale'},
            }, command=True),
            Step('turn.projection.update', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_missing',
                'expected_projection_revision': 0,
                'projection': {'content': 'x'},
            }, command=True),
            # A non-object projection coerces to a content document.
            Step('turn.projection.update', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_id': f'{ns}_t1', 'expected_projection_revision': 2,
                'projection': 'plain text',
            }, command=True, ignore_fields=edited),
            read('turn.get', f'{ns}_conv', ignore=edited,
                 turn_id=f'{ns}_t1'),
            read('turn.revision', f'{ns}_conv'),
        ]),
        'turn branch create delete lifecycle': build(lambda ns: [
            append(f'{ns}_conv', f'{ns}_t1', 0),
            Step('turn.branch.create', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'parent_turn_id': f'{ns}_t1',
                'expected_projection_revision': 1,
                'title': 'Side branch', 'anchor_text': 'anchor',
            }, command=True, ignore_fields=branched),
            Step('turn.append_settled', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'actor': 'human',
                'projection': {'content': 'branch reply'},
                'lane_id': _Ref(1, ('lane', 'laneId')),
                'command_id': f'{ns}_t2_command',
                'turn_id': f'{ns}_t2',
                'created_at': _TURN_TS + 2000,
                'now': _TURN_TS + 2000,
            }, command=True),
            # Both branch and main lanes sit at ordinal 0; both
            # authorities break the cross-lane tie by turn_id bytes.
            Step('turn.list', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
            }, ignore_fields=branched),
            read('turn.list', f'{ns}_conv', ignore=branched,
                 lane_id='main'),
            Step('turn.branch.create', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'parent_turn_id': f'{ns}_t1',
                'expected_projection_revision': 1,
            }, command=True),
            Step('turn.branch.create', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'parent_turn_id': f'{ns}_missing',
                'expected_projection_revision': 0,
            }, command=True),
            Step('turn.branch.delete', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'parent_turn_id': f'{ns}_t1',
                'lane_id': _Ref(1, ('lane', 'laneId')),
            }, command=True, ignore_fields=edited),
            read('turn.get', f'{ns}_conv', turn_id=f'{ns}_t2'),
            read('turn.list', f'{ns}_conv', ignore=edited),
            Step('turn.branch.delete', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'parent_turn_id': f'{ns}_t1',
                'lane_id': _Ref(1, ('lane', 'laneId')),
            }, command=True),
        ]),
        'turn delete and delta tombstones': build(lambda ns: [
            append(f'{ns}_conv', f'{ns}_t1', 0),
            append(f'{ns}_conv', f'{ns}_t2', 1),
            read('turn.list_delta', f'{ns}_conv', since_ms=0),
            # lower = since - 5s overlap = _TURN_TS + 1001, so only t2
            # (pinned updated_at _TURN_TS + 2000) survives the filter.
            read('turn.list_delta', f'{ns}_conv',
                 since_ms=_TURN_TS + 6001),
            Step('turn.delete', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_ids': [f'{ns}_t1'],
            }, command=True),
            read('turn.get', f'{ns}_conv', turn_id=f'{ns}_t1'),
            read('turn.list', f'{ns}_conv'),
            read('turn.list_delta', f'{ns}_conv', since_ms=0),
            read('turn.list_delta', f'{ns}_conv', since_ms=0,
                 known_revisions={f'{ns}_t2': 1}),
            read('turn.list_delta', f'{ns}_conv', since_ms=0,
                 known_revisions={f'{ns}_t2': 0}),
            Step('turn.delete', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_ids': [],
            }, command=True),
            Step('turn.delete', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'turn_ids': [f'{ns}_missing'],
            }, command=True),
            read('turn.revision', f'{ns}_conv'),
        ]),
        'turn sync snapshot page and changes': build(lambda ns: [
            append(f'{ns}_conv', f'{ns}_t1', 0),
            append(f'{ns}_conv', f'{ns}_t2', 1),
            read('turn.sync.snapshot', f'{ns}_conv'),
            read('turn.sync.snapshot', f'{ns}_conv', turn_limit=1),
            read('turn.sync.snapshot', f'{ns}_conv',
                 include_artifact_hint=True),
            read('turn.sync.snapshot', f'{ns}_conv',
                 include_artifact_hint='yes'),
            read('turn.sync.snapshot', f'{ns}_missing'),
            read('turn.sync.page', f'{ns}_conv', lane_id='main',
                 sync_sequence=_Ref(2, ('syncSequence',)), limit=1),
            read('turn.sync.page', f'{ns}_conv', lane_id='main',
                 sync_sequence=_Ref(2, ('syncSequence',)), limit=1,
                 before_ordinal=1),
            read('turn.sync.page', f'{ns}_conv', lane_id='main',
                 sync_sequence=999),
            read('turn.sync.page', f'{ns}_conv', lane_id='side',
                 sync_sequence=_Ref(2, ('syncSequence',))),
            # Turn-specific replay envelopes retain their top-level identity.
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'after': 0,
            }),
            Step('turn.sync.changes', {
                'conversation_id': f'{ns}_conv', 'user_id': user,
                'after': 0, 'limit': 1,
            }),
            read('turn.sync.changes', f'{ns}_conv', after=2),
            read('turn.sync.changes', f'{ns}_conv', after=9),
            read('turn.sync.changes', f'{ns}_missing'),
            read('turn.sync.changes', f'{ns}_conv', limit=0),
        ]),
    }


def _provider_scripts() -> dict[str, list[Step]]:
    """Owner-scoped BYO provider CRUD; all clocks ride the payload.

    ``tenant_id`` namespaces the owner boundary so scripts stay isolated
    on the module-scoped legacy authority without a cleanup pass.
    """
    user = _OWNER_ID

    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        return make(f'diff_{uuid.uuid4().hex[:8]}')

    return {
        'provider lifecycle validation and isolation': build(lambda ns: [
            Step('provider.list', {'owner_user_id': user, 'tenant_id': ns}),
            Step('provider.get', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p1',
            }),
            Step('provider.create', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p1', 'name': 'Main',
                'base_url': 'https://api.example.com',
                'api_key_ciphertext': 'cipher-1', 'key_hint': 'hint-1',
                'models': [{'id': 'm1', 'label': '模型'}],
                'extra_headers': {'X-A': 'b'},
                'thinking_format': 'openai', 'created_at': 1000,
            }, command=True),
            # Duplicate identity must conflict on both authorities.
            Step('provider.create', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p1', 'name': 'Again',
                'base_url': 'https://api.example.com',
                'models': [], 'extra_headers': {}, 'created_at': 1500,
            }, command=True),
            Step('provider.create', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p2', 'name': 'Second',
                'base_url': 'https://b.example.com',
                'models': [], 'extra_headers': {}, 'created_at': 2000,
            }, command=True),
            # created_at DESC, id DESC: p2 precedes p1.
            Step('provider.list', {'owner_user_id': user, 'tenant_id': ns}),
            Step('provider.update', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p1',
                'updates': {'name': 'Renamed', 'disabled': True},
                'updated_at': 3000,
            }, command=True),
            Step('provider.update', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p1', 'updates': {}, 'updated_at': 1,
            }, command=True),
            Step('provider.update', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p1',
                'updates': {'bogus': 1}, 'updated_at': 1,
            }, command=True),
            Step('provider.touch', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p1', 'used_at': 4000,
            }, command=True),
            Step('provider.touch', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_missing', 'used_at': 1,
            }, command=True),
            Step('provider.get', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p1',
            }),
            Step('provider.delete', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p1',
            }, command=True),
            Step('provider.delete', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p1',
            }, command=True),
            Step('provider.delete', {
                'owner_user_id': user, 'tenant_id': ns,
                'provider_id': f'{ns}_p2',
            }, command=True),
            Step('provider.list', {'owner_user_id': user, 'tenant_id': ns}),
        ]),
    }


def _model_routing_scripts() -> dict[str, list[Step]]:
    """Model-routing v2 authority, migration receipts and secrets.

    ``tenant_id`` namespaces the owner boundary so each script owns a fresh
    authority row on the module-scoped legacy side.
    """
    user = _OWNER_ID

    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        return make(f'diff_{uuid.uuid4().hex[:8]}')

    def doc(revision: int, route: str) -> dict[str, Any]:
        return {
            'contract_version': 'tofu.model-routing/v2',
            'revision': revision,
            'routes': {'default': route},
        }

    return {
        'model routing commit CAS and migration receipt': build(lambda ns: [
            Step('model_routing.get',
                 {'owner_user_id': user, 'tenant_id': ns}),
            Step('model_routing.migration_receipt',
                 {'owner_user_id': user, 'tenant_id': ns}),
            Step('model_routing.commit', {
                'owner_user_id': user, 'tenant_id': ns,
                'expected_revision': 0, 'document': doc(1, 'm1'),
                'updated_at': 1000,
            }, command=True),
            Step('model_routing.get',
                 {'owner_user_id': user, 'tenant_id': ns}),
            # Stale expected revision must conflict on both authorities.
            Step('model_routing.commit', {
                'owner_user_id': user, 'tenant_id': ns,
                'expected_revision': 0, 'document': doc(1, 'm1'),
                'updated_at': 1500,
            }, command=True),
            Step('model_routing.commit', {
                'owner_user_id': user, 'tenant_id': ns,
                'expected_revision': 1,
                'document': {'contract_version': 'wrong', 'revision': 2},
                'updated_at': 1,
            }, command=True),
            Step('model_routing.commit', {
                'owner_user_id': user, 'tenant_id': ns,
                'expected_revision': 1, 'document': doc(5, 'm2'),
                'updated_at': 1,
            }, command=True),
            Step('model_routing.commit', {
                'owner_user_id': user, 'tenant_id': ns,
                'expected_revision': 1, 'document': doc(2, 'm2'),
                'updated_at': 2000,
                'migration_receipt': {'status': 'applied', 'migratedAt': 1},
            }, command=True),
            Step('model_routing.migration_receipt',
                 {'owner_user_id': user, 'tenant_id': ns}),
        ]),
        'model routing receipt put without document probe': build(
            lambda ns: [
                Step('model_routing.commit', {
                    'owner_user_id': user, 'tenant_id': ns,
                    'expected_revision': 0, 'document': doc(1, 'm1'),
                    'updated_at': 1000,
                }, command=True),
                # Existing authority accepts a receipt-only replacement.
                Step('model_routing.migration_receipt.put', {
                    'owner_user_id': user, 'tenant_id': ns,
                    'migration_receipt': {'status': 'rejected'},
                    'updated_at': 3000,
                }, command=True),
                Step('model_routing.migration_receipt',
                     {'owner_user_id': user, 'tenant_id': ns}),
                Step('model_routing.get',
                     {'owner_user_id': user, 'tenant_id': ns}),
            ],
        ),
        'model routing receipt put seeds an empty authority': build(
            lambda ns: [
                Step('model_routing.migration_receipt.put', {
                    'owner_user_id': user, 'tenant_id': ns,
                    'migration_receipt': {'status': 'failed'},
                    'document': doc(0, ''),
                    'updated_at': 1000,
                }, command=True),
                # Replacing the receipt on the now-existing authority does
                # not require or rewrite the empty authority document.
                Step('model_routing.migration_receipt.put', {
                    'owner_user_id': user, 'tenant_id': ns,
                    'migration_receipt': {'status': 'recovered'},
                    'updated_at': 2000,
                }, command=True),
                Step('model_routing.get',
                     {'owner_user_id': user, 'tenant_id': ns}),
                Step('model_routing.migration_receipt',
                     {'owner_user_id': user, 'tenant_id': ns}),
            ],
        ),
        'model routing secret lifecycle': build(lambda ns: [
            Step('model_routing.secret.list',
                 {'owner_user_id': user, 'tenant_id': ns}),
            Step('model_routing.secret.get', {
                'owner_user_id': user, 'tenant_id': ns,
                'secret_reference': f'{ns}_s1',
            }),
            Step('model_routing.secret.put', {
                'owner_user_id': user, 'tenant_id': ns,
                'secret_reference': f'{ns}_s1', 'ciphertext': 'c1',
                'key_hint': 'h1', 'updated_at': 1000,
            }, command=True),
            Step('model_routing.secret.put', {
                'owner_user_id': user, 'tenant_id': ns,
                'secret_reference': f'{ns}_s2', 'ciphertext': 'c2',
                'updated_at': 2000,
            }, command=True),
            # Re-put updates ciphertext/hint but preserves created_at.
            Step('model_routing.secret.put', {
                'owner_user_id': user, 'tenant_id': ns,
                'secret_reference': f'{ns}_s1', 'ciphertext': 'c1b',
                'key_hint': 'h1b', 'updated_at': 3000,
            }, command=True),
            Step('model_routing.secret.get', {
                'owner_user_id': user, 'tenant_id': ns,
                'secret_reference': f'{ns}_s1',
            }),
            Step('model_routing.secret.list',
                 {'owner_user_id': user, 'tenant_id': ns}),
            Step('model_routing.secret.delete', {
                'owner_user_id': user, 'tenant_id': ns,
                'secret_reference': f'{ns}_s2',
            }, command=True),
            Step('model_routing.secret.delete', {
                'owner_user_id': user, 'tenant_id': ns,
                'secret_reference': f'{ns}_s2',
            }, command=True),
            Step('model_routing.secret.put', {
                'owner_user_id': user, 'tenant_id': ns,
                'secret_reference': f'{ns}_s3', 'ciphertext': '',
                'updated_at': 1,
            }, command=True),
        ]),
        'model routing secret prune respects actives and cutoff': build(
            lambda ns: [
                Step('model_routing.secret.put', {
                    'owner_user_id': user, 'tenant_id': ns,
                    'secret_reference': f'{ns}_old', 'ciphertext': 'c',
                    'updated_at': 100,
                }, command=True),
                Step('model_routing.secret.put', {
                    'owner_user_id': user, 'tenant_id': ns,
                    'secret_reference': f'{ns}_active', 'ciphertext': 'c',
                    'updated_at': 200,
                }, command=True),
                Step('model_routing.secret.put', {
                    'owner_user_id': user, 'tenant_id': ns,
                    'secret_reference': f'{ns}_new', 'ciphertext': 'c',
                    'updated_at': 9000,
                }, command=True),
                Step('model_routing.secret.prune', {
                    'owner_user_id': user, 'tenant_id': ns,
                    'active_secret_references': [f'{ns}_active'],
                    'updated_before': 1000,
                }, command=True),
                Step('model_routing.secret.list',
                     {'owner_user_id': user, 'tenant_id': ns}),
                Step('model_routing.secret.prune', {
                    'owner_user_id': user, 'tenant_id': ns,
                    'active_secret_references': 'not-a-list',
                    'updated_before': 1,
                }, command=True),
                Step('model_routing.secret.prune', {
                    'owner_user_id': user, 'tenant_id': ns,
                    'active_secret_references': [], 'updated_before': 10000,
                }, command=True),
                Step('model_routing.secret.list',
                     {'owner_user_id': user, 'tenant_id': ns}),
            ],
        ),
    }


def _system_scripts() -> dict[str, list[Step]]:
    """System metadata plus backend-specific bounded physical reclamation."""
    return {
        'system schema version': [
            Step('system.schema_version', {}),
        ],
        'system bounded reclaim': [
            Step('system.reclaim', {
                'max_pages': 1, 'min_free_pages': 0, 'budget_ms': 10,
            }, command=True, expect_divergence=(
                'system.reclaim backend physical metrics')),
        ],
    }


def _rate_limit_scripts() -> dict[str, list[Step]]:
    """Exact per-bucket admission and validation on the request hot path."""
    suffix = uuid.uuid4().hex[:12]
    base = {
        'endpoint': f'/rate-limit/{suffix}',
        'client_key': f'client-{suffix}',
        'limit': 2,
        'per_seconds': 60,
    }
    return {
        'rate limit admits exactly the configured bucket capacity': [
            Step('rate_limit.record_and_check', {
                **base, 'event_id': f'{suffix}-event-1',
            }, command=True),
            Step('rate_limit.record_and_check', {
                **base, 'event_id': f'{suffix}-event-2',
            }, command=True),
            Step('rate_limit.record_and_check', {
                **base, 'event_id': f'{suffix}-event-3',
            }, command=True),
            Step('rate_limit.record_and_check', {
                **base, 'client_key': f'other-{suffix}',
                'event_id': f'{suffix}-other-event',
            }, command=True),
            Step('rate_limit.record_and_check', {
                **base, 'event_id': f'{suffix}-invalid-limit', 'limit': 0,
            }, command=True),
            Step('rate_limit.record_and_check', {
                **base, 'event_id': f'{suffix}-invalid-window',
                'per_seconds': 604801,
            }, command=True),
        ],
    }


def _daily_cost_scripts() -> dict[str, list[Step]]:
    """Date-ordered owner cache, including overwrite and both delete scopes."""
    user = _OWNER_ID

    def reset() -> Step:
        return Step(
            'daily_cost.delete', {'user_id': user}, command=True, compare=False,
        )

    return {
        'daily cost month latest probe overwrite and deletion': [
            reset(),
            Step('daily_cost.latest', {'user_id': user}),
            Step('daily_cost.upsert', {
                'user_id': user, 'date': '2026-08-13', 'cost': 2.5,
                'conversations': {'conv-1': {'cost': 2.5, 'tokens': 10}},
                'computed_at': 1000,
            }, command=True),
            Step('daily_cost.upsert', {
                'user_id': user, 'date': '2026-08-12', 'cost': 1.25,
                'conversations': {'conv-1': {'cost': 1.25, 'tokens': 8}},
                'computed_at': 900,
            }, command=True),
            Step('daily_cost.upsert', {
                'user_id': user, 'date': '2026-08-13', 'cost': 3.0,
                'conversations': {'conv-1': {'cost': 3.0, 'tokens': 12}},
                'computed_at': 1100,
            }, command=True),
            Step('daily_cost.month', {
                'user_id': user, 'year': 2026, 'month': 8,
            }),
            Step('daily_cost.latest', {'user_id': user}),
            Step('daily_cost.persisted_dates', {
                'user_id': user,
                'dates': ['2026-08-11', '2026-08-13', '2026-08-13'],
            }),
            Step('daily_cost.delete', {
                'user_id': user, 'date': '2026-08-12',
            }, command=True),
            Step('daily_cost.delete', {
                'user_id': user, 'date': '2026-08-12',
            }, command=True),
            Step('daily_cost.delete', {'user_id': user}, command=True),
            Step('daily_cost.latest', {'user_id': user}),
        ],
        'daily cost preserves Python digit syntax and rejects malformed bounds': [
            reset(),
            Step('daily_cost.upsert', {
                'user_id': user, 'date': '２０２６-08-13', 'cost': 1,
                'conversations': {}, 'computed_at': 1,
            }, command=True),
            Step('daily_cost.persisted_dates', {
                'user_id': user, 'dates': ['２０２６-08-13'],
            }),
            Step('daily_cost.delete', {
                'user_id': user, 'date': '２０２６-08-13',
            }, command=True),
            Step('daily_cost.upsert', {
                'user_id': user, 'date': '2026-8-01', 'cost': 1,
                'conversations': {}, 'computed_at': 1,
            }, command=True),
            Step('daily_cost.month', {
                'user_id': user, 'year': 1969, 'month': 1,
            }),
            Step('daily_cost.persisted_dates', {
                'user_id': user, 'dates': ['not-a-date'],
            }),
        ],
    }


def _log_aggregate_scripts() -> dict[str, list[Step]]:
    """Tenant-global observability aggregates: merge, sweep, and LIKE parity.

    The table is global (no owner) and the legacy fixture persists across
    scripts while the tofu-db daemon starts fresh per script, so every script
    opens with far-future sweeps that drain earlier leftovers on both sides
    (compare=False — swept counts legitimately differ in that reset step).
    """

    def drain() -> Step:
        return Step(
            'log_aggregate.flush',
            {'rows': [], 'cutoff_ms': _FAR_FUTURE_MS},
            command=True, compare=False,
        )

    def row(
        fingerprint: str,
        level: str,
        template: str,
        count: int,
        first_seen: int,
        last_seen: int,
        **extra: Any,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            'fingerprint': fingerprint,
            'level': level,
            'logger': '',
            'template': template,
            'sample': '',
            'count': count,
            'first_seen': first_seen,
            'last_seen': last_seen,
        }
        item.update(extra)
        return item

    valid_row = row('fp.valid', 'info', 'ok', 1, 10, 20)

    return {
        'log aggregate flush merges sweeps and query sorts filters totals': [
            drain(), drain(), drain(),
            Step('log_aggregate.flush', {
                'rows': [
                    row('aaa.disk-full', 'error', 'disk 100%_\\full α',
                        2, 1000, 5000, logger='alpha', sample='s1'),
                    row('bbb.quota', 'warn', 'quota exceeded β', 5, 2000, 4000),
                    row('ccc.disk-slow', 'error', 'disk slow γ',
                        2, 3000, 3000, logger='alpha', sample='s3'),
                ],
            }, command=True),
            Step('log_aggregate.query', {}),
            Step('log_aggregate.query', {'level': 'error'}),
            # Full (level, count) tie probes the legacy rowid tie order.
            Step('log_aggregate.query', {'sort': 'level'}),
            Step('log_aggregate.query', {'q': 'DISK'}),
            # LIKE metacharacters must match literally on both sides.
            Step('log_aggregate.query', {'q': '100%_\\'}),
            Step('log_aggregate.query', {'q': 'α'}),
            # The legacy SQLite build links ICU: LIKE folds per code point
            # with Unicode simple case folding, so 'Α' matches 'α' here.
            Step('log_aggregate.query', {'q': 'Α'}),
            Step('log_aggregate.flush', {
                'rows': [
                    # Merge keeps the original level/logger/template/first_seen
                    # and takes count/last_seen/sample from the incoming row.
                    row('aaa.disk-full', 'fatal', 'changed template',
                        3, 500, 6000, logger='changed', sample='s1b'),
                    row('ddd.fresh', 'info', 'fresh δ',
                        1, 7000, 7000, sample='s4'),
                ],
            }, command=True),
            Step('log_aggregate.query', {'sort': 'last_seen'}),
            Step('log_aggregate.query', {'level': 'error', 'sort': 'count'}),
            Step('log_aggregate.flush', {
                'rows': [], 'cutoff_ms': 5500,
            }, command=True),
            Step('log_aggregate.query', {}),
            Step('log_aggregate.flush', {
                'rows': [], 'cutoff_ms': 5500,
            }, command=True),
        ],
        'log aggregate rejects invalid batches and queries identically': [
            drain(), drain(), drain(),
            Step('log_aggregate.flush', {'rows': 'notalist'}, command=True),
            Step('log_aggregate.flush', {
                'rows': [
                    row(f'fp.bulk-{index}', 'info', 'bulk', 1, 1, 1)
                    for index in range(501)
                ],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [valid_row, {'level': 'info'}],
            }, command=True),
            # Both authorities must leave no partial batch behind.
            Step('log_aggregate.query', {}),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'count': 0}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'count': 1_000_000_001}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'count': True}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'count': 1.5}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'fingerprint': 'x' * 65}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'level': 'l' * 33}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'template': 't' * 201}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'sample': 's' * 2001}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'logger': 'g' * 257}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'logger': 123}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'logger': None}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'first_seen': -1}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'first_seen': 1.0}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [{**valid_row, 'last_seen': 'x'}],
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [], 'cutoff_ms': 'soon',
            }, command=True),
            Step('log_aggregate.flush', {
                'rows': [], 'cutoff_ms': -5,
            }, command=True),
            # Cutoff validation fires after row validation on the legacy
            # side; the rolled-back batch must stay invisible everywhere.
            Step('log_aggregate.flush', {
                'rows': [valid_row], 'cutoff_ms': 'soon',
            }, command=True),
            Step('log_aggregate.query', {}),
            Step('log_aggregate.query', {'sort': 'bogus'}),
            Step('log_aggregate.query', {'sort': 's' * 33}),
            Step('log_aggregate.query', {'limit': 0}),
            Step('log_aggregate.query', {'limit': 501}),
            Step('log_aggregate.query', {'limit': 'many'}),
            Step('log_aggregate.query', {'limit': None}),
            Step('log_aggregate.query', {'level': None}),
            Step('log_aggregate.query', {}),
            Step('log_aggregate.flush', {'rows': []}, command=True),
        ],
    }


def _raw_archive_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID
    conversation = f'raw-diff-{suffix}'
    replacement_conversation = f'raw-diff-reused-{suffix}'
    task = f'raw-task-{suffix}'
    archive = f'raw-archive-{suffix}'
    request_body = b'provider request body'
    response_body = b'data: first\n\ndata: second\n\n'

    def encoded(raw: bytes) -> str:
        return base64.b64encode(zlib.compress(raw)).decode('ascii')

    def put_payload(archive_id: str, budget: int) -> dict:
        return {
            'archive_id': archive_id,
            'conversation_id': conversation,
            'turn_id': _Ref(0, ('attempt', 'turnId')),
            'attempt_id': _Ref(0, ('attempt', 'attemptId')),
            'task_id': task,
            'user_id': user,
            'round_num': 1,
            'transport_attempt': 0,
            'request_blob_b64': encoded(request_body),
            'response_blob_b64': encoded(response_body),
            'request_bytes': len(request_body),
            'response_bytes': len(response_body),
            'request_sha256': hashlib.sha256(request_body).hexdigest(),
            'response_sha256': hashlib.sha256(response_body).hexdigest(),
            'integrity': 'complete',
            'truncation_reason': '',
            'summary': {'text': 'Provider request/response'},
            'budget_bytes': budget,
            'min_free_bytes': 0,
            'available_free_bytes': 0,
        }

    ignore_clock = frozenset({'createdAt'})
    return {
        'raw archive preserves parent fencing lazy reads quota and owner scope': [
            Step('turn.create_pair', {
                'conversation_id': conversation,
                'user_id': user,
                'command_id': f'{conversation}-pair',
                'input_projection': {'content': 'capture'},
                'conversation_defaults': {'allowCreate': True},
                'now': _TURN_TS,
            }, command=True),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(0, ('attempt', 'attemptId')),
                'task_id': task,
                'user_id': user,
            }, command=True),
            Step('raw_archive.put', put_payload(archive, 1024 * 1024),
                 command=True, ignore_fields=ignore_clock),
            Step('raw_archive.put', put_payload(archive, 1024 * 1024),
                 command=True, ignore_fields=ignore_clock),
            Step('raw_archive.list', {
                'user_id': user, 'task_id': task, 'round_num': 1,
            }, ignore_fields=ignore_clock),
            Step('raw_archive.read', {
                'user_id': user, 'task_id': task, 'archive_id': archive,
                'part': 'response', 'offset': 0, 'limit': 16,
            }, ignore_fields=ignore_clock),
            Step('raw_archive.put',
                 put_payload(f'{archive}-quota', 1),
                 command=True, ignore_fields=ignore_clock),
            Step('conversation.delete', {
                'conv_id': conversation, 'user_id': user,
            }, command=True),
            Step('raw_archive.list', {
                'user_id': user, 'task_id': task,
            }, ignore_fields=ignore_clock),
            Step('turn.create_pair', {
                'conversation_id': replacement_conversation,
                'user_id': user,
                'command_id': f'{replacement_conversation}-pair',
                'input_projection': {'content': 'capture again'},
                'conversation_defaults': {'allowCreate': True},
                'now': _TURN_TS + 1,
            }, command=True),
            Step('turn.attempt.bind', {
                'attempt_id': _Ref(9, ('attempt', 'attemptId')),
                'task_id': task,
                'user_id': user,
            }, command=True),
            Step('raw_archive.put', {
                **put_payload(archive, 1024 * 1024),
                'conversation_id': replacement_conversation,
                'turn_id': _Ref(9, ('attempt', 'turnId')),
                'attempt_id': _Ref(9, ('attempt', 'attemptId')),
            }, command=True, ignore_fields=ignore_clock),
        ],
        'raw archive validation and missing parent fail identically': [
            Step('raw_archive.put', {
                **put_payload(f'{archive}-missing', 1024 * 1024),
                'attempt_id': f'missing-{suffix}',
                'turn_id': f'missing-{suffix}',
            }, command=True),
            Step('raw_archive.list', {'user_id': user, 'task_id': task, 'limit': 0}),
            Step('raw_archive.read', {
                'user_id': user, 'task_id': task, 'archive_id': archive,
                'part': 'invalid',
            }),
        ],
    }


def _plugin_scripts() -> dict[str, list[Step]]:
    """Declarative plugin storage manifests: register/get and versioning.

    Namespaces are script-unique because the module-scoped legacy fixture
    keeps every registered manifest across scripts.  Error outcomes compare
    by code; the normalized manifest payloads compare exactly, so the
    default-injection and truthiness-coercion rules of
    ``lib/storage/manifest.py`` are exercised end to end.
    """

    def base(namespace: str, version: int = 1) -> dict:
        return {
            'namespace': namespace, 'version': version,
            'tables': [{
                'name': 'items',
                'columns': [
                    {'name': 'id', 'type': 'string'},
                    {'name': 'note', 'type': 'string', 'required': True},
                ],
                'primary_key': ['id'],
                'indexes': [{'name': 'by_note', 'columns': ['note']}],
            }],
            'operations': [
                {'name': 'fetch', 'action': 'get', 'table': 'items'},
                {'name': 'store', 'action': 'put', 'table': 'items',
                 'limit_max': 50},
            ],
        }

    def register(manifest: dict | None) -> Step:
        payload = {} if manifest is None else {'manifest': manifest}
        return Step('plugin.register', payload, command=True)

    def get(namespace) -> Step:
        return Step('plugin.manifest.get', {'namespace': namespace})

    main = 'diffplug.main'
    v1 = base(main)
    # Normalization-equivalent re-register: explicit defaults, reordered
    # keys, and truthy coercions must collapse to the same canonical bytes.
    v1_coerced = {
        'version': 1, 'namespace': main,
        'operations': [
            {'table': 'items', 'action': 'get', 'name': 'fetch',
             'kind': 0, 'limit_max': True},
            {'name': 'store', 'action': 'put', 'table': 'items',
             'limit_max': 50, 'kind': ''},
        ],
        'tables': [{
            'indexes': [{'columns': ['note'], 'name': 'by_note',
                         'unique': 0}],
            'primary_key': ['id'],
            'columns': [
                {'type': 'string', 'name': 'id', 'required': 0},
                {'name': 'note', 'type': 'string', 'required': 'yes'},
            ],
            'name': 'items',
        }],
    }
    v2 = base(main, 2)
    v2['tables'][0]['columns'].append(
        {'name': 'extra', 'type': 'integer'})
    v2['tables'][0]['indexes'].append(
        {'name': 'by_extra', 'columns': ['extra']})
    v2['tables'].append({
        'name': 'logs',
        'columns': [{'name': 'id', 'type': 'string'}],
        'primary_key': ['id'],
        'indexes': [],
    })
    v2['operations'].append(
        {'name': 'listing', 'action': 'list', 'table': 'logs'})

    def v3_with(mutate) -> dict:
        import copy
        candidate = copy.deepcopy(v2)
        candidate['version'] = 3
        mutate(candidate)
        return candidate

    v3 = v3_with(lambda m: m['tables'][0]['columns'].append(
        {'name': 'flag', 'type': 'boolean'}))
    v3['operations'].append(
        {'name': 'purge', 'action': 'delete', 'table': 'items'})

    return {
        'plugin register get roundtrip and append-only versioning': [
            register(v1),
            # Identical re-register under a fresh command id is a no-op.
            register(base(main)),
            register(v1_coerced),
            get(main),
            register(v2),
            get(main),
            # Version moved backwards.
            register(base(main)),
            # Same version, different definition.
            register({**v2, 'operations': v2['operations'][:-1]}),
            # New column marked required.
            register(v3_with(
                lambda m: m['tables'][0]['columns'].append(
                    {'name': 'req', 'type': 'string', 'required': True}))),
            # Dropped table.
            register(v3_with(lambda m: m['tables'].pop())),
            # Dropped operation.
            register(v3_with(lambda m: m['operations'].pop())),
            # Changed primary key.
            register(v3_with(
                lambda m: m['tables'][0].update(primary_key=['note']))),
            # New unique index.
            register(v3_with(lambda m: m['tables'][0]['indexes'].append(
                {'name': 'by_note_uniq', 'columns': ['note'],
                 'unique': True}))),
            # Redefined existing index.
            register(v3_with(
                lambda m: m['tables'][0]['indexes'][0].update(
                    columns=['id']))),
            # Reordered existing columns breaks the prefix rule.
            register(v3_with(
                lambda m: m['tables'][0].update(
                    columns=list(reversed(m['tables'][0]['columns']))))),
            # A compatible v3 finally lands.
            register(v3),
            get(main),
        ],
        'plugin register rejects malformed manifests identically': [
            register(None),
            Step('plugin.register', {'manifest': None}, command=True),
            Step('plugin.register', {'manifest': 'x'}, command=True),
            register({**base('diffplug.bad'), 'namespace': 'AB.cd'}),
            register({**base('diffplug.bad'), 'namespace': 'ab'}),
            register({**base('diffplug.bad'), 'namespace': 'x' * 129}),
            register({**base('diffplug.bad'), 'version': 0}),
            register({**base('diffplug.bad'), 'version': 'v'}),
            register({'namespace': 'diffplug.bad'}),
            register({**base('diffplug.bad'), 'tables': []}),
            register({**base('diffplug.bad'), 'tables': 'x'}),
            register(v3_with(
                lambda m: m['tables'][0].update(name='T'))),
            register(v3_with(
                lambda m: m['tables'][0]['columns'][0].update(
                    type='unknown'))),
            register(v3_with(
                lambda m: m['tables'][0].update(primary_key=[]))),
            register(v3_with(
                lambda m: m['tables'][0].update(primary_key=['nope']))),
            register(v3_with(
                lambda m: m['tables'][0]['indexes'][0].update(
                    columns=['nope']))),
            register(v3_with(
                lambda m: m['tables'].append(m['tables'][0]))),
            register(v3_with(
                lambda m: m['operations'].append(m['operations'][0]))),
            register(v3_with(
                lambda m: m['operations'][0].update(action='explode'))),
            register(v3_with(
                lambda m: m['operations'][0].update(table='ghost'))),
            register(v3_with(
                lambda m: m['operations'][0].update(limit_max=0))),
            register(v3_with(
                lambda m: m['operations'][0].update(limit_max=1001))),
            register(v3_with(
                lambda m: m['operations'][0].update(limit_max='x'))),
            # Nothing may have landed through the rejections.
            get('diffplug.bad'),
            get(main.replace('main', 'bad')),
        ],
        'plugin manifest get validates namespace shape identically': [
            get('diffplug.unregistered'),
            Step('plugin.manifest.get', {}),
            get(''),
            get(123),
            get('x' * 129),
            get('y' * 128),
        ],
    }


def _optimizer_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID
    first = f'proposal-a-{suffix}'
    second = f'proposal-b-{suffix}'
    action = f'action-a-{suffix}'
    return {
        'optimizer proposal and action lifecycle preserves joins expiry and filtering': [
            Step('optimizer.proposal.create', {
                'user_id': user, 'proposal_id': first,
                'created_at': '2026-08-14T10:00:00',
                'title': 'Bound writer queue', 'rationale': 'protect memory',
                'action_type': 'set_limit', 'action_args': '{"limit":200}',
                'severity': 'high', 'confidence': 0.9,
                'evidence': '["metric"]', 'status': 'pending_review',
                'status_reason': '',
            }, command=True),
            Step('optimizer.proposal.create', {
                'user_id': user, 'proposal_id': second,
                'created_at': '2026-08-15T10:00:00',
                'action_type': 'noop', 'action_args': '{}',
                'confidence': 0, 'evidence': '[]',
            }, command=True),
            Step('optimizer.proposal.get', {
                'user_id': user, 'proposal_id': first,
            }),
            Step('optimizer.proposal.list', {
                'user_id': user, 'status': '', 'limit': 10,
            }),
            Step('optimizer.proposal.list', {
                'user_id': user, 'status': 'pending_review', 'limit': 1,
            }),
            Step('optimizer.proposal.update', {
                'user_id': user, 'proposal_id': first,
                'status': 'applied', 'reason': 'verified',
            }, command=True),
            Step('optimizer.proposal.update', {
                'user_id': user, 'proposal_id': f'missing-{suffix}',
                'status': 'rejected', 'reason': '',
            }, command=True),
            Step('optimizer.action.record', {
                'user_id': user, 'log_id': action, 'proposal_id': first,
                'applied_at': '2026-08-14T10:01:00',
                'expires_at': '2026-08-15T10:01:00', 'pre_metric': '{}',
            }, command=True),
            Step('optimizer.action.outcome', {
                'user_id': user, 'log_id': action,
                'outcome_metric': '{"ok":true}',
                'recorded_at': '2026-08-14T11:00:00',
            }, command=True),
            Step('optimizer.action.for_proposal', {
                'user_id': user, 'proposal_id': first,
            }),
            Step('optimizer.action.expired', {
                'user_id': user, 'now_iso': '2026-08-16T00:00:00',
            }),
            Step('optimizer.action.list', {
                'user_id': user, 'include_reverted': False, 'limit': 10,
            }),
            Step('optimizer.action.revert', {
                'user_id': user, 'log_id': action,
                'reverted_at': '2026-08-16T00:00:01', 'reason': 'expired',
            }, command=True),
            Step('optimizer.action.list', {
                'user_id': user, 'include_reverted': False, 'limit': 10,
            }),
            Step('optimizer.action.list', {
                'user_id': user, 'include_reverted': True, 'limit': 10,
            }),
            Step('optimizer.action.expired', {
                'user_id': user, 'now_iso': '2026-08-16T00:00:02',
            }),
            Step('optimizer.proposal.get', {
                'user_id': user, 'proposal_id': f'missing-{suffix}',
            }),
        ],
        'optimizer validation and missing proposal integrity stay exact': [
            Step('optimizer.proposal.create', {
                'user_id': user, 'proposal_id': f'invalid-{suffix}',
                'created_at': '2026-08-14T10:00:00',
                'action_type': 'noop', 'action_args': '{}',
                'confidence': True, 'evidence': '[]',
            }, command=True),
            Step('optimizer.proposal.list', {'user_id': user, 'limit': 0}),
            Step('optimizer.action.list', {
                'user_id': user, 'include_reverted': 'yes',
            }),
            Step('optimizer.action.record', {
                'user_id': user, 'log_id': f'missing-action-{suffix}',
                'proposal_id': f'missing-{suffix}',
                'applied_at': '2026-08-14T10:01:00',
                'expires_at': '2026-08-15T10:01:00', 'pre_metric': '{}',
            }, command=True),
        ],
    }


def _research_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID
    paper = f'research-{suffix}'
    return {
        'research artifacts fold compact directions without report scans': [
            Step('research.artifact.upsert', {
                'user_id': user, 'paper_hash': paper,
                'lang_key': 'survey:en', 'report': '# survey',
                'model': 'research-model', 'created_at': 1000,
                'meta': {'kind': 'survey', 'direction': 'storage architecture',
                         'open_gaps': {'open_gaps': []}},
            }, command=True),
            Step('research.artifact.upsert', {
                'user_id': user, 'paper_hash': paper,
                'lang_key': 'ideate:en', 'report': '# ideas',
                'model': 'research-model', 'created_at': 1001,
                'meta': {'kind': 'ideate', 'direction': 'storage architecture',
                         'accepted': [{'id': 'a'}],
                         'rejected': [{'id': 'r'}],
                         'gate_reached': 'accepted', 'degraded': True},
            }, command=True),
            Step('research.artifacts.get', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
            }),
            Step('research.directions.list', {'user_id': user, 'limit': 50}),
            Step('research.artifacts.get', {
                'user_id': user, 'paper_hash': f'missing-{suffix}', 'lang': 'en',
            }),
        ],
        'research workspace revision CAS preserves zero legacy timestamps': [
            Step('research.workspace.get', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
            }),
            Step('research.workspace.put', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
                'expected_revision': 0, 'updated_at': 0,
                'workspace': {'direction': 'storage architecture', 'notes': []},
            }, command=True),
            Step('research.workspace.get', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
            }),
            Step('research.workspace.put', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
                'expected_revision': 0, 'updated_at': 1, 'workspace': {},
            }, command=True),
            Step('research.workspace.put', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
                'expected_revision': 1, 'updated_at': 2,
                'workspace': {'direction': 'storage architecture', 'notes': ['next']},
            }, command=True),
            Step('research.workspace.get', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
            }),
        ],
        'research validation fails before storage': [
            Step('research.artifact.upsert', {
                'user_id': user, 'paper_hash': paper, 'lang_key': 'report:en',
                'meta': {}, 'created_at': 1,
            }, command=True),
            Step('research.artifacts.get', {
                'user_id': user, 'paper_hash': paper, 'lang': 'survey:en',
            }),
            Step('research.directions.list', {'user_id': user, 'limit': 0}),
            Step('research.workspace.put', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
                'expected_revision': 0, 'workspace': {},
            }, command=True),
        ],
    }


def _paper_note_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID
    paper = f'paper-note-{suffix}'
    first = f'note-a-{suffix}'
    second = f'note-b-{suffix}'
    return {
        'paper notes preserve ordered CRUD and missing-row results': [
            Step('paper.note.create', {
                'user_id': user, 'id': second, 'paper_hash': paper, 'lang': 'en',
                'anchor': {'page': 2}, 'note': 'second',
                'created_at': 11, 'updated_at': 11,
            }, command=True),
            Step('paper.note.create', {
                'user_id': user, 'id': first, 'paper_hash': paper, 'lang': 'en',
                'anchor': {'page': 1}, 'note': 'first',
                'created_at': 10, 'updated_at': 10,
            }, command=True),
            Step('paper.note.list', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
            }),
            Step('paper.note.update', {
                'user_id': user, 'id': second, 'note': 'second-updated',
                'updated_at': 12,
            }, command=True),
            Step('paper.note.update', {
                'user_id': user, 'id': f'missing-{suffix}', 'note': 'missing',
                'updated_at': 12,
            }, command=True),
            Step('paper.note.delete', {
                'user_id': user, 'id': first,
            }, command=True),
            Step('paper.note.delete', {
                'user_id': user, 'id': f'missing-{suffix}',
            }, command=True),
            Step('paper.note.list', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
            }),
        ],
        'paper note validation rejects malformed documents': [
            Step('paper.note.create', {
                'user_id': user, 'id': f'invalid-{suffix}',
                'paper_hash': paper, 'anchor': [], 'note': 'invalid',
                'created_at': 1, 'updated_at': 1,
            }, command=True),
            Step('paper.note.create', {
                'user_id': user, 'id': f'empty-{suffix}',
                'paper_hash': paper, 'note': '',
                'created_at': 1, 'updated_at': 1,
            }, command=True),
            Step('paper.note.list', {'user_id': user, 'paper_hash': paper}),
        ],
    }


def _paper_artifact_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID
    paper = f'paper-report-{suffix}'
    other = f'paper-report-other-{suffix}'
    tied = f'paper-report-tied-{suffix}'
    return {
        'paper reports preserve projections resolution siblings excerpts and accounting': [
            Step('paper.report.upsert', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
                'report': 'English report body', 'model': 'model-a',
                'meta': {'promptTokens': 5, 'costCny': 1.5}, 'created_at': 10,
            }, command=True),
            Step('paper.report.upsert', {
                'user_id': user, 'paper_hash': paper, 'lang': 'zh',
                'report': 'Chinese report body', 'model': 'model-b',
                'meta': {}, 'created_at': 20,
            }, command=True),
            Step('paper.report.upsert', {
                'user_id': user, 'paper_hash': other, 'lang': 'en',
                'report': 'Other report', 'model': '', 'meta': {},
                'created_at': 30,
            }, command=True),
            Step('paper.report.get', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
            }),
            Step('paper.report.get', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
                'max_report_chars': 7,
            }),
            Step('paper.report.resolve', {
                'user_id': user, 'paper_hash': paper,
                'preferred_lang': 'fr', 'fallback_lang': 'en',
            }),
            Step('paper.report.reopen', {
                'user_id': user, 'paper_hash': paper,
                'preferred_lang': 'en', 'fallback_lang': 'zh',
                'sibling_langs_by_base': {'en': [' zh ', 'zh']},
            }),
            Step('paper.report.excerpts', {
                'user_id': user, 'paper_hashes': [other, paper, paper],
                'lang': 'en', 'max_report_chars': 6,
            }),
            Step('paper.report.latest', {
                'user_id': user, 'paper_hash': paper,
            }),
            Step('paper.report.upsert', {
                'user_id': user, 'paper_hash': tied, 'lang': 'z',
                'report': 'z report', 'model': '', 'meta': {},
                'created_at': 50,
            }, command=True),
            Step('paper.report.upsert', {
                'user_id': user, 'paper_hash': tied, 'lang': 'aa',
                'report': 'aa report', 'model': '', 'meta': {},
                'created_at': 50,
            }, command=True),
            Step('paper.report.latest', {
                'user_id': user, 'paper_hash': tied,
            }),
            Step('paper.report.upsert', {
                'user_id': user, 'paper_hash': tied, 'lang': 'z',
                'report': 'z report revised', 'model': '', 'meta': {},
                'created_at': 60,
            }, command=True),
            Step('paper.report.upsert', {
                'user_id': user, 'paper_hash': tied, 'lang': 'z',
                'report': 'z report revised again', 'model': '', 'meta': {},
                'created_at': 40,
            }, command=True),
            Step('paper.report.latest', {
                'user_id': user, 'paper_hash': tied,
            }),
            Step('paper.report.second_pass.merge', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
                'name': 'citations',
                'entry': {'usage': {'prompt_tokens': 2}, 'costCny': 0.25},
            }, command=True),
            Step('paper.report.second_pass.accumulate', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
                'name': 'review',
                'usage': {'prompt_tokens': 3, 'completion_tokens': 4},
                'costCny': 0.5, 'costUsd': 0.1,
            }, command=True),
            Step('paper.report.get', {
                'user_id': user, 'paper_hash': paper, 'lang': 'en',
            }),
            Step('paper.translation.upsert', {
                'user_id': user, 'paper_hash': paper, 'lang': 'zh',
                'text': 'translated text', 'model': 'translator',
                'created_at': 40,
            }, command=True),
            Step('paper.translation.get', {
                'user_id': user, 'paper_hash': paper, 'lang': 'zh',
            }),
            Step('paper.translation.get', {
                'user_id': user, 'paper_hash': paper, 'lang': 'fr',
            }),
        ],
        'paper report and translation validation remains fail closed': [
            Step('paper.report.excerpts', {
                'user_id': user, 'paper_hashes': 'bad', 'lang': 'en',
                'max_report_chars': 10,
            }),
            Step('paper.report.reopen', {
                'user_id': user, 'paper_hash': paper,
                'preferred_lang': 'en',
                'sibling_langs_by_base': {'fr': ['zh']},
            }),
            Step('paper.translation.upsert', {
                'user_id': user, 'paper_hash': paper, 'lang': 'zh',
                'text': {}, 'created_at': 1,
            }, command=True),
        ],
    }


def _paper_podcast_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    key = {
        'user_id': _OWNER_ID,
        'paper_hash': f'paper-podcast-{suffix}',
        'mode': 'short',
        'lang': 'zh',
        'voice': 'alloy',
    }
    return {
        'paper podcast preserves upsert interruption and original creation time': [
            Step('paper.podcast.get', key),
            Step('paper.podcast.upsert', {
                **key, 'status': 'generating', 'script': {},
                'meta': {'task_id': 'pod-1'}, 'duration_sec': 0,
                'created_at': 1000, 'updated_at': 1000,
            }, command=True),
            Step('paper.podcast.mark_interrupted', {
                'updated_at': 2000,
            }, command=True),
            Step('paper.podcast.get', key),
            Step('paper.podcast.upsert', {
                **key, 'status': 'done',
                'script': {'segments': [{'text': 'hello'}]},
                'meta': {'source_kind': 'report_zh'},
                'file_path': 'paper.wav', 'duration_sec': 3.5,
                'model': 'writer', 'tts_model': 'voice',
                'created_at': 9999, 'updated_at': 3000,
            }, command=True),
            Step('paper.podcast.get', key),
        ],
        'paper podcast validation remains fail closed': [
            Step('paper.podcast.upsert', {
                **key, 'status': 'unknown', 'script': {}, 'meta': {},
                'duration_sec': 0, 'created_at': 1, 'updated_at': 1,
            }, command=True),
            Step('paper.podcast.upsert', {
                **key, 'status': 'done', 'script': [], 'meta': {},
                'duration_sec': 0, 'created_at': 1, 'updated_at': 1,
            }, command=True),
        ],
    }


def _paper_library_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID
    shared_hash = f'paper-library-hash-{suffix}'
    first = f'paper-library-a-{suffix}'
    second = f'paper-library-b-{suffix}'
    other = f'paper-library-c-{suffix}'

    def put(
        paper_id: str,
        *,
        title: str,
        arxiv_id: str,
        paper_hash: str,
        text: str,
        created_at: int,
        updated_at: int,
    ) -> Step:
        return Step('paper.library.put', {
            'user_id': user, 'id': paper_id, 'title': title,
            'pdf_url': f'https://example.invalid/{paper_id}.pdf',
            'pdf_filename': f'{paper_id}.pdf', 'arxiv_id': arxiv_id,
            'paper_hash': paper_hash, 'parsed_text': text,
            'parser_version': 'parser-v1',
            'qa_history': json.dumps([{'question': 'why?'}]),
            'images': json.dumps([{'page': 1}]),
            'babel_cache': json.dumps({'zh': 'cached'}),
            'page_count': 3, 'folder_id': 'inbox',
            'created_at': created_at, 'updated_at': updated_at,
        }, command=True)

    return {
        'paper library preserves bounded projections ordering repair and CRUD': [
            put(first, title='arXiv: 2401.00001', arxiv_id='2401.00001',
                paper_hash=shared_hash, text='abcdef', created_at=10,
                updated_at=10),
            put(second, title='Authoritative title', arxiv_id='2401.00002',
                paper_hash=shared_hash, text='second body', created_at=20,
                updated_at=20),
            put(other, title='Other paper', arxiv_id='2401.00003',
                paper_hash=f'other-{shared_hash}', text='third body',
                created_at=30, updated_at=30),
            Step('paper.library.summaries', {'user_id': user}),
            Step('paper.library.list', {'user_id': user}),
            Step('paper.library.get', {'user_id': user, 'id': first}),
            Step('paper.library.reader', {'user_id': user, 'id': first}),
            Step('paper.library.recent', {
                'user_id': user, 'exclude_paper_hash': shared_hash, 'limit': 2,
            }),
            Step('paper.library.inputs', {
                'user_id': user,
                'arxiv_ids': [' 2401.00001 ', '2401.00001', '2401.00003'],
                'max_text_chars': 4,
            }),
            Step('paper.library.identity', {
                'user_id': user, 'paper_hash': shared_hash,
                'max_text_chars': 3, 'include_text_length': True,
            }),
            Step('paper.library.identity', {
                'user_id': user, 'paper_hash': shared_hash,
                'max_text_chars': 0, 'include_text_length': False,
            }),
            Step('paper.library.title.backfill', {
                'user_id': user, 'paper_hash': shared_hash,
                'title': 'Recovered title',
            }, command=True),
            Step('paper.library.identity', {
                'user_id': user, 'paper_hash': shared_hash,
                'max_text_chars': 3, 'include_text_length': True,
            }),
            put(first, title='Updated title', arxiv_id='2401.00001',
                paper_hash=shared_hash, text='replacement', created_at=999,
                updated_at=40),
            Step('paper.library.get', {'user_id': user, 'id': first}),
            Step('paper.library.delete', {'user_id': user, 'id': second},
                 command=True),
            Step('paper.library.delete', {'user_id': user, 'id': second},
                 command=True),
            Step('paper.library.get', {'user_id': user, 'id': second}),
            Step('paper.library.summaries', {'user_id': user}),
        ],
        'paper library rejects invalid bounds': [
            Step('paper.library.inputs', {
                'user_id': user, 'arxiv_ids': '2401.00001',
            }),
            Step('paper.library.identity', {
                'user_id': user, 'paper_hash': shared_hash,
                'max_text_chars': 1, 'include_text_length': False,
            }),
            Step('paper.library.recent', {'user_id': user, 'limit': 0}),
        ],
    }


def _scheduler_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID
    first = f'scheduler-a-{suffix}'
    second = f'scheduler-b-{suffix}'
    system = f'scheduler-system-{suffix}'
    return {
        'scheduler task CRUD ordering and enabled projection': [
            Step('scheduler.task.create', {
                'task_id': first, 'user_id': user, 'name': 'First task',
                'schedule': '*/5 * * * *', 'task_type': 'agent',
                'command': 'status', 'created_at': '2026-09-01T00:00:00',
                'updated_at': '2026-09-01T00:00:00',
                'target_conv_id': 'conv-scheduler', 'tools_config': {},
            }, command=True),
            Step('scheduler.task.create', {
                'task_id': second, 'user_id': user, 'name': 'Second task',
                'schedule': '*/10 * * * *', 'task_type': 'agent',
                'command': 'continue', 'created_at': '2026-09-02T00:00:00',
                'updated_at': '2026-09-02T00:00:00',
                'target_conv_id': 'conv-scheduler',
                'tools_config': {'web': False},
            }, command=True),
            Step('scheduler.task.ensure', {
                'task_id': system, 'user_id': user,
                'system_key': f'builtin-{suffix}', 'name': 'System task',
                'schedule': '0 * * * *', 'task_type': 'agent',
                'command': 'maintain',
                'created_at': '2026-09-03T00:00:00',
                'updated_at': '2026-09-03T00:00:00',
                'tools_config': {'shell': False},
            }, command=True),
            Step('scheduler.task.ensure', {
                'task_id': f'ignored-{suffix}', 'user_id': user,
                'system_key': f'builtin-{suffix}', 'name': 'System task',
                'schedule': '30 * * * *', 'task_type': 'agent',
                'command': 'maintain',
                'description': 'reconciled',
                'created_at': '2026-09-04T00:00:00',
                'updated_at': '2026-09-04T00:00:00',
                'tools_config': {'shell': False},
            }, command=True),
            Step('scheduler.task.get', {
                'task_id': first, 'user_id': user,
            }),
            Step('scheduler.task.list', {
                'user_id': user, 'limit': 1, 'enabled_only': False,
            }),
            Step('scheduler.task.update', {
                'task_id': second, 'user_id': user, 'enabled': 0,
                'description': 'paused', 'now': '2026-09-03T00:00:00',
            }, command=True),
            Step('scheduler.task.list', {
                'user_id': user, 'limit': 20, 'enabled_only': True,
            }),
            Step('scheduler.task.list_all', {
                'limit': 20, 'enabled_only': True,
            }),
            Step('scheduler.task.claim_due', {
                'task_id': first, 'user_id': user, 'lane': 'run',
                'now': '2026-09-05T10:00:00',
                'minimum_interval_seconds': 55,
            }, command=True),
            Step('scheduler.task.claim_due', {
                'task_id': first, 'user_id': user, 'lane': 'run',
                'now': '2026-09-05T10:00:30',
                'minimum_interval_seconds': 55,
            }, command=True),
            Step('scheduler.task.record_result', {
                'task_id': first, 'user_id': user,
                'now': '2026-09-05T10:01:00', 'result': 'failed once',
                'success': False,
            }, command=True),
            Step('scheduler.poll.append', {
                'task_id': first, 'user_id': user,
                'poll_time': '2026-09-05T10:02:00', 'decision': 'run',
                'reason': 'due', 'status_snapshot': 'ready',
                'model': 'model-x', 'tokens_used': 12,
                'execution_task_id': 'execution-1', 'tier': 'llm',
                'predicate_matched': 1, 'llm_agreed': 1,
            }, command=True),
            Step('scheduler.poll.append', {
                'task_id': first, 'user_id': user,
                'poll_time': '2026-09-05T10:03:00', 'decision': 'skip',
                'reason': 'settled',
            }, command=True),
            Step('scheduler.poll.log', {
                'task_id': first, 'user_id': user, 'limit': 20,
            }),
            Step('scheduler.task.delete', {
                'task_id': first, 'user_id': user,
            }, command=True),
            Step('scheduler.task.delete', {
                'task_id': first, 'user_id': user,
            }, command=True),
            Step('scheduler.task.get', {
                'task_id': first, 'user_id': user,
            }),
            Step('scheduler.poll.log', {
                'task_id': first, 'user_id': user, 'limit': 20,
            }),
        ],
    }


def _timer_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID
    first = f'timer-a-{suffix}'
    second = f'timer-b-{suffix}'

    def create(timer_id: str, created_at: str) -> Step:
        return Step('timer.create', {
            'timer_id': timer_id, 'user_id': user,
            'conv_id': f'conversation-{suffix}',
            'check_instruction': 'Check status',
            'continuation_message': 'Continue when ready',
            'poll_interval': 60, 'max_polls': 10,
            'created_at': created_at, 'updated_at': created_at,
            'tools_config': {'web': False},
        }, command=True)

    poll = {
        'timer_id': first, 'user_id': user, 'poll_id': f'poll-{suffix}',
        'poll_time': '2026-09-05T10:00:00', 'decision': 'wait',
        'reason': 'not ready', 'check_output': 'pending',
        'tokens_used': 7, 'model': 'model-x', 'raw_output': 'raw',
        'tier': 'llm', 'predicate_matched': 0, 'llm_agreed': 1,
    }
    return {
        'timer lifecycle, active feed, progress, and idempotent poll ledger': [
            create(first, '2026-09-01T00:00:00'),
            create(second, '2026-09-02T00:00:00'),
            Step('timer.get', {'timer_id': first, 'user_id': user}),
            Step('timer.list', {'user_id': user, 'limit': 20}),
            Step('timer.history', {'user_id': user}),
            Step('timer.active.count', {'user_id': user}),
            Step('timer.active.list_all', {'limit': 20}),
            Step('timer.poll.append', poll, command=True),
            Step('timer.poll.append', poll, command=True),
            Step('timer.poll.commit', {
                **poll, 'poll_id': f'commit-{suffix}',
                'poll_time': '2026-09-05T10:01:00', 'decision': 'run',
                'reason': 'ready',
            }, command=True),
            Step('timer.poll.commit', {
                **poll, 'poll_id': f'commit-{suffix}',
                'poll_time': '2026-09-05T10:01:00', 'decision': 'run',
                'reason': 'ready',
            }, command=True),
            Step('timer.progress', {
                'timer_id': first, 'user_id': user,
                'poll_time': '2026-09-05T10:02:00',
                'decision': 'skipped', 'reason': 'local predicate',
            }, command=True),
            Step('timer.poll.log', {
                'timer_id': first, 'user_id': user, 'limit': 20,
            }),
            Step('timer.update', {
                'timer_id': first, 'user_id': user,
                'expected_status': 'active', 'promotion_streak': 2,
                'condition_kind': 'command',
            }, command=True),
            Step('timer.cancel', {
                'timer_id': first, 'user_id': user,
                'now': '2026-09-05T10:03:00',
            }, command=True),
            Step('timer.cancel', {
                'timer_id': first, 'user_id': user,
                'now': '2026-09-05T10:04:00',
            }, command=True),
            Step('timer.active.count', {'user_id': user}),
            Step('timer.list', {'user_id': user, 'limit': 20}),
        ],
    }


def _queue_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID
    conv = f'queue-conversation-{suffix}'
    goal = f'queue-goal-{suffix}'
    workflow = f'queue-workflow-{suffix}'
    human = f'queue-human-{suffix}'
    return {
        'queue marker ordering supersession leases recovery and cleanup': [
            Step('conversation.create', {
                'conv_id': conv, 'user_id': user, 'created_at': _CONV_TS,
                'updated_at': _CONV_TS,
            }, command=True),
            Step('queue.autopilot.arm', {
                'conv_id': conv, 'user_id': user,
                'queue_id': f'marker-{suffix}', 'config': {'model': 'm'},
            }, command=True, ignore_fields=frozenset({'createdAt'})),
            Step('queue.autopilot.arm', {
                'conv_id': conv, 'user_id': user,
                'queue_id': f'marker-other-{suffix}', 'config': {'model': 'x'},
            }, command=True, ignore_fields=frozenset({'createdAt'})),
            Step('queue.autopilot.get', {
                'conv_id': conv, 'user_id': user,
            }, ignore_fields=frozenset({'createdAt'})),
            Step('queue.autopilot.list_all', {},
                 ignore_fields=frozenset({'createdAt'})),
            Step('queue.enqueue', {
                'conv_id': conv, 'user_id': user, 'queue_id': goal,
                'message': {'text': 'continue'}, 'config': {'goal': True},
                'kind': 'goal_continuation', 'priority': 20,
                'created_at_ms': 2,
            }, command=True),
            Step('queue.enqueue', {
                'conv_id': conv, 'user_id': user,
                'queue_id': f'{goal}-duplicate',
                'message': {'text': 'duplicate'}, 'config': {},
                'kind': 'goal_continuation', 'priority': 20,
                'created_at_ms': 3,
            }, command=True),
            Step('queue.enqueue', {
                'conv_id': conv, 'user_id': user, 'queue_id': workflow,
                'message': {'text': 'workflow'}, 'config': {},
                'kind': 'workflow_step', 'priority': 50,
                'created_at_ms': 4,
            }, command=True),
            Step('queue.enqueue', {
                'conv_id': conv, 'user_id': user, 'queue_id': human,
                'message': {'text': 'human', '_msgId': f'msg-{suffix}'},
                'config': {'model': 'm'}, 'kind': 'real', 'priority': 10,
                'created_at_ms': 5,
            }, command=True),
            Step('queue.list', {'conv_id': conv, 'user_id': user}),
            Step('queue.depth', {'conv_id': conv, 'user_id': user}),
            Step('queue.conversations.list_all', {}),
            Step('queue.conversations.list_all', {'kind': 'real'}),
            Step('queue.conversations.list_all', {
                'reap_probe_contract': 'tofu.queue.reap-probe/v1',
                'now_ms': 100,
            }),
            Step('queue.dequeue', {
                'conv_id': conv, 'user_id': user,
                'now_ms': 100, 'lease_ms': 1000,
            }, command=True),
            Step('queue.lease.bind', {
                'queue_id': human, 'user_id': user, 'task_id': f'task-{suffix}',
                'now_ms': 100, 'lease_ms': 1000,
            }, command=True),
            Step('queue.dequeue', {
                'conv_id': conv, 'user_id': user,
                'now_ms': 100, 'lease_ms': 1000,
            }, command=True),
            Step('queue.conversations.list_all', {
                'reap_probe_contract': 'tofu.queue.reap-probe/v1',
                'now_ms': 1101,
            }),
            Step('queue.reap', {'now_ms': 1101}, command=True),
            Step('queue.lease.release', {
                'queue_id': human, 'user_id': user,
            }, command=True),
            Step('queue.kind.clear', {
                'conv_id': conv, 'user_id': user,
                'kind': 'goal_continuation',
            }, command=True),
            Step('queue.finalize', {
                'conv_id': conv, 'user_id': user, 'queue_id': workflow,
            }, command=True),
            Step('queue.remove', {
                'conv_id': conv, 'user_id': user, 'queue_id': human,
            }, command=True),
            Step('queue.clear', {
                'conv_id': conv, 'user_id': user,
            }, command=True),
            Step('queue.autopilot.clear', {
                'conv_id': conv, 'user_id': user,
            }, command=True),
        ],
    }


def _project_brain_scripts() -> dict[str, list[Step]]:
    """Projection/event lifecycle foundation with exact receipt replay."""
    user = _OWNER_ID
    suffix = uuid.uuid4().hex[:12]
    project = f'/project-brain/{suffix}/'
    task_id = f'project-task-{suffix}'
    work_id = 'pw_' + hashlib.sha256(task_id.encode()).hexdigest()[:24]
    normalized_project = project.rstrip('/')
    start_payload = {
        'owner_user_id': user,
        'project_key': project,
        'work_item': {
            'id': work_id, 'taskId': task_id,
            'conversationId': f'conversation-{suffix}',
            'title': 'Initial title', 'trigger': 'file_write',
            'status': 'active', 'changedPaths': [], 'artifacts': [],
            'resultSummary': '', 'startedAt': 1, 'finishedAt': None,
            '_titlePriority': 100, '_titleRefined': False,
        },
        'timestamp': 1,
    }
    return {
        'project brain work and narrative lifecycle': [
            Step('project_brain.get', {
                'owner_user_id': user, 'project_key': project,
            }),
            Step('project_brain.work.start', start_payload, command=True),
            Step('project_brain.work.start', start_payload, command=True),
            Step('project_brain.active.list', {'owner_user_id': user}),
            Step('project_brain.recovery.snapshot', {}, maintenance=True),
            Step('project_brain.work.refine', {
                'owner_user_id': user, 'project_key': project,
                'work_id': work_id, 'title': 'Refined title',
                'title_priority': 200, 'timestamp': 2,
            }, command=True),
            Step('project_brain.work.change', {
                'owner_user_id': user, 'project_key': project,
                'work_id': work_id,
                'changed_paths': [' src/lib.rs ', 'src/lib.rs', '   '],
                'artifacts': [{
                    'id': f'artifact-{suffix}', 'title': 'Patch',
                    'format': 'diff', 'path': 'change.diff',
                }],
                'timestamp': 3,
            }, command=True),
            Step('project_brain.work.finish', {
                'owner_user_id': user, 'project_key': project,
                'work_id': work_id, 'status': 'completed',
                'result_summary': ' Implemented ', 'timestamp': 4,
            }, command=True),
            Step('project_brain.narrative.add', {
                'owner_user_id': user, 'project_key': project,
                'kind': 'decision', 'text': '验' * 400,
                'conversation_id': f'conversation-{suffix}', 'timestamp': 5,
            }, command=True),
            Step('project_brain.get', {
                'owner_user_id': user,
                'project_key': project.rstrip('/'),
            }),
            Step('project_brain.rebuild', {
                'owner_user_id': user,
                'project_key': project.rstrip('/'),
            }, maintenance=True),
        ],
        'project brain rejects non-deterministic work identity': [
            Step('project_brain.work.start', {
                **start_payload,
                'project_key': f'/project-brain-invalid/{suffix}',
                'work_item': {**start_payload['work_item'], 'id': 'pw_invalid'},
            }, command=True),
        ],
        'project brain 00 native authority is cutover complete': [
            Step('project_brain.cutover.status', {}, expect_divergence=(
                'project brain format-native cutover state')),
            Step('project_brain.cutover', {}, command=True, expect_divergence=(
                'project brain format-native cutover state')),
            Step('project_brain.cutover', {}, command=True),
            Step('project_brain.cutover.status', {}),
        ],
        'project brain checker decision watch and cursor lifecycle': [
            Step('project_brain.checker.register', {
                'owner_user_id': user, 'project_key': project,
                'definition': {
                    'checkerId': f'checker-{suffix}', 'version': 1,
                    'label': 'Project tests', 'argv': ['pytest', '-q'],
                    'cwd': normalized_project, 'pathGlobs': ['src/**'],
                    'timeoutMs': 30000, 'enabled': True,
                }, 'timestamp': 10,
            }, command=True),
            Step('project_brain.checker.register', {
                'owner_user_id': user, 'project_key': project,
                'definition': {
                    'checkerId': f'checker-{suffix}', 'version': 1,
                    'label': 'Project tests', 'argv': ['pytest', '-q'],
                    'cwd': normalized_project, 'pathGlobs': ['src/**'],
                    'timeoutMs': 30000, 'enabled': True,
                }, 'timestamp': 10,
            }, command=True),
            Step('project_brain.decision.promote', {
                'owner_user_id': user, 'project_key': project,
                'decision': {
                    'decisionId': f'decision-{suffix}',
                    'text': 'Keep immutable project events',
                    'sourceConversationId': f'conversation-{suffix}',
                    'sourceTurnId': f'turn-{suffix}',
                    'checkerRef': {'id': f'checker-{suffix}', 'version': 1},
                    'latestVerification': None,
                }, 'timestamp': 11,
            }, command=True),
            Step('project_brain.checker.result', {
                'owner_user_id': user, 'project_key': project,
                'decision_id': f'decision-{suffix}',
                'result': {
                    'checkerRef': {'id': f'checker-{suffix}', 'version': 1},
                    'label': 'Project tests', 'ok': False, 'exitCode': 1,
                    'timedOut': False, 'durationMs': 25,
                    'reason': 'failed', 'summary': 'one test failed',
                    'output': 'failure detail', 'workId': '', 'timestamp': 12,
                }, 'timestamp': 12,
            }, command=True),
            Step('project_brain.watch.add', {
                'owner_user_id': user, 'project_key': project,
                'item': {
                    'id': f'watch-{suffix}', 'kind': 'concern',
                    'text': 'Watch recovery latency', 'status': 'active',
                    'sourceConversationId': f'conversation-{suffix}',
                    'createdAt': 13, 'updatedAt': 13, 'latestResult': None,
                }, 'timestamp': 13,
            }, command=True),
            Step('project_brain.watch.update', {
                'owner_user_id': user, 'project_key': project,
                'item': {
                    'id': f'watch-{suffix}', 'kind': 'concern',
                    'text': 'Recovery latency verified', 'status': 'resolved',
                    'sourceConversationId': f'conversation-{suffix}',
                    'createdAt': 13, 'updatedAt': 14,
                    'latestResult': {
                        'text': 'bounded', 'trigger': 'certification',
                        'timestamp': 14,
                    },
                }, 'timestamp': 14,
            }, command=True),
            Step('project_brain.watch.delete', {
                'owner_user_id': user, 'project_key': project,
                'item_id': f'watch-{suffix}', 'timestamp': 15,
            }, command=True),
            Step('project_brain.cursor.confirm', {
                'owner_user_id': user, 'project_key': project,
                'conversation_id': f'consumer-{suffix}',
                'from_sequence': 0, 'delivered_sequence': 6,
                'delivery_token': hashlib.sha256(
                    f'{user}\0{normalized_project}\0consumer-{suffix}\0{0}\0{6}'.encode()
                ).hexdigest(),
                'timestamp': 16,
            }, command=True),
            Step('project_brain.cursor.prepare', {
                'owner_user_id': user, 'project_key': project,
                'conversation_id': f'feed-{suffix}', 'timestamp': 17,
            }, command=True),
            Step('project_brain.cursor.prepare', {
                'owner_user_id': user, 'project_key': project,
                'conversation_id': f'feed-{suffix}', 'timestamp': 18,
            }, command=True),
            Step('project_brain.narrative.add', {
                'owner_user_id': user, 'project_key': project,
                'kind': 'note', 'text': 'new bounded feed entry',
                'timestamp': 19,
            }, command=True),
            Step('project_brain.cursor.prepare', {
                'owner_user_id': user, 'project_key': project,
                'conversation_id': f'feed-{suffix}', 'limit': 12,
                'token_budget': 900, 'timestamp': 20,
            }, command=True),
            Step('project_brain.get', {
                'owner_user_id': user, 'project_key': normalized_project,
            }),
        ],
    }


def _worker_job_scripts() -> dict[str, list[Step]]:
    """Durable worker-job claims, leases, fencing, cancellation, settlement.

    Every clock rides the pinned ``now_ms`` payload field — both
    authorities derive lease deadlines, heartbeats and terminal timestamps
    from it — so the full 23-field job documents compare exactly.
    ``task_kind`` is namespaced per script because claim_next scans the
    shared queue and the module-scoped legacy authority keeps leftovers
    from earlier scripts (the tofu daemon is function-scoped/fresh).
    Application-error probes stay at script tails, three at a time, per
    the legacy-client suppression note on the conversation search script.
    """
    user = _OWNER_ID
    now0 = int(time.time() * 1000)

    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        return make(f'diff_{uuid.uuid4().hex[:8]}')

    def enqueue(
        ns: str,
        task: str,
        kind: str,
        key: str,
        **extra: Any,
    ) -> Step:
        payload = {
            'task_id': f'{ns}_{task}', 'user_id': user, 'tenant_id': ns,
            'task_kind': f'{ns}_{kind}', 'payload': {'task': task},
            'idempotency_key': f'{ns}_{key}', 'priority': 100,
            'now_ms': now0, 'available_at_ms': now0,
        }
        payload.update(extra)
        return Step('worker_job.enqueue', payload, command=True)

    def claim(ns: str, worker: str, kinds: list[str], now: int) -> Step:
        return Step('worker_job.claim_next', {
            'worker_id': f'{ns}_{worker}',
            'task_kinds': [f'{ns}_{kind}' for kind in kinds],
            'now_ms': now, 'lease_ms': 30_000,
        }, command=True)

    return {
        'worker job enqueue get and idempotent replay': build(lambda ns: [
            Step('worker_job.get', {'task_id': f'{ns}_j1', 'user_id': user}),
            enqueue(ns, 'j1', 'ka', 'k1'),
            Step('worker_job.get', {'task_id': f'{ns}_j1', 'user_id': user}),
            # Same idempotency key + same request replays the stored job.
            enqueue(ns, 'j1', 'ka', 'k1'),
            # Same key + different request conflicts; the same task_id
            # under a fresh key conflicts too.
            enqueue(ns, 'other', 'ka', 'k1', payload={'task': 'changed'}),
            enqueue(ns, 'j1', 'ka', 'k2'),
        ]),
        'worker job enqueue validation bounds': build(lambda ns: [
            Step('worker_job.get', {'task_id': f'{ns}_none', 'user_id': user}),
            enqueue(ns, 'bad_payload', 'ka', 'k1', payload='not-an-object'),
            enqueue(ns, 'bad_priority', 'ka', 'k2', priority=1001),
            # A client clock more than 24h ahead of the authority would
            # strand the job past every reaper horizon; both sides refuse.
            enqueue(ns, 'bad_clock', 'ka', 'k3',
                    now_ms=now0 + 25 * 60 * 60 * 1000),
        ]),
        'worker job claim priority availability and expiry': build(lambda ns: [
            enqueue(ns, 'j1', 'ka', 'k1', priority=100),
            enqueue(ns, 'j2', 'ka', 'k2', priority=5),
            enqueue(ns, 'j3', 'kb', 'k3', priority=5),
            # Not yet available: delayed jobs stay queued behind live work.
            enqueue(ns, 'j4', 'ka', 'k4', priority=1,
                    available_at_ms=now0 + 60_000),
            claim(ns, 'w1', ['kb'], now0),               # j3 (only kb)
            claim(ns, 'w2', ['ka'], now0),               # j2 (priority 5)
            claim(ns, 'w3', ['ka'], now0),               # j1
            # Everything leased or unavailable: the queue answers null.
            claim(ns, 'w4', ['ka', 'kb'], now0),
            # Expired-lease takeover after the 30s deadline: the global
            # (deadline, priority, created, task_id) order picks j2 with
            # fencingToken 2 and attempt 2.
            claim(ns, 'w5', ['ka', 'kb'], now0 + 31_000),
            Step('worker_job.get', {'task_id': f'{ns}_j2', 'user_id': user}),
        ]),
        'worker job claim validation bounds': build(lambda ns: [
            enqueue(ns, 'j1', 'ka', 'k1'),
            Step('worker_job.claim_next', {
                'worker_id': f'{ns}_w1', 'now_ms': now0,
            }, command=True),
            Step('worker_job.claim_next', {
                'worker_id': f'{ns}_w1', 'task_kinds': [], 'now_ms': now0,
            }, command=True),
            Step('worker_job.claim_next', {
                'worker_id': f'{ns}_w1', 'task_kinds': [f'{ns}_ka'],
                'now_ms': now0, 'lease_ms': 5_000,
            }, command=True),
        ]),
        'worker job heartbeat fencing and monotonicity': build(lambda ns: [
            enqueue(ns, 'j1', 'ka', 'k1'),
            # A heartbeat without a live claim is stale, not an error.
            Step('worker_job.heartbeat', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 1, 'now_ms': now0,
            }, command=True),
            claim(ns, 'w1', ['ka'], now0),               # fence 1
            Step('worker_job.heartbeat', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 1, 'now_ms': now0 + 5_000,
                'lease_ms': 30_000, 'replay_cursor': 7,
            }, command=True),
            # Lease/heartbeat/replay cursor are monotone maxima: an older
            # clock and smaller cursor must not rewind the document.
            Step('worker_job.heartbeat', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 1, 'now_ms': now0 + 1_000,
                'lease_ms': 10_000, 'replay_cursor': 3,
            }, command=True),
            Step('worker_job.get', {'task_id': f'{ns}_j1', 'user_id': user}),
            Step('worker_job.heartbeat', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 9, 'now_ms': now0 + 6_000,
            }, command=True),
            Step('worker_job.heartbeat', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w2',
                'fencing_token': 1, 'now_ms': now0 + 6_000,
            }, command=True),
            # Lease deadline (now0+35000) has passed: the fence is stale.
            Step('worker_job.heartbeat', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 1, 'now_ms': now0 + 40_000,
            }, command=True),
        ]),
        'worker job claim_state and cancel settlement': build(lambda ns: [
            enqueue(ns, 'j1', 'ka', 'k1'),
            Step('worker_job.claim_state', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 1, 'now_ms': now0,
            }),
            claim(ns, 'w1', ['ka'], now0),               # fence 1
            Step('worker_job.claim_state', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 1, 'now_ms': now0,
            }),
            Step('worker_job.claim_state', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 2, 'now_ms': now0,
            }),
            Step('worker_job.request_cancel', {
                'task_id': f'{ns}_missing', 'user_id': user, 'now_ms': now0,
            }, command=True),
            Step('worker_job.request_cancel', {
                'task_id': f'{ns}_j1', 'user_id': user,
                'now_ms': now0 + 1_000, 'reason': 'stop',
            }, command=True),
            # A second request reports the in-flight cancellation.
            Step('worker_job.request_cancel', {
                'task_id': f'{ns}_j1', 'user_id': user,
                'now_ms': now0 + 2_000, 'reason': 'stop',
            }, command=True),
            Step('worker_job.claim_state', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 1, 'now_ms': now0 + 3_000,
            }),
            # A cancel-requested job may only settle as cancelled.
            Step('worker_job.complete', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 1, 'now_ms': now0 + 4_000,
                'terminal_status': 'succeeded',
            }, command=True),
            Step('worker_job.complete', {
                'task_id': f'{ns}_j1', 'worker_id': f'{ns}_w1',
                'fencing_token': 1, 'now_ms': now0 + 5_000,
                'terminal_status': 'cancelled', 'result_ref': 'ref-cancel',
            }, command=True),
            Step('worker_job.request_cancel', {
                'task_id': f'{ns}_j1', 'user_id': user,
                'now_ms': now0 + 6_000,
            }, command=True),
        ]),
        'worker job cancel queued and complete lifecycle': build(lambda ns: [
            enqueue(ns, 'j1', 'ka', 'k1'),
            # Cancelling a queued job settles it immediately.
            Step('worker_job.request_cancel', {
                'task_id': f'{ns}_j1', 'user_id': user, 'now_ms': now0,
                'reason': 'not-needed',
            }, command=True),
            # The terminal check precedes the already-requested check.
            Step('worker_job.request_cancel', {
                'task_id': f'{ns}_j1', 'user_id': user, 'now_ms': now0 + 1,
            }, command=True),
            # Cancelled work never re-enters the claimable queue.
            claim(ns, 'w1', ['ka'], now0 + 5_000),
            enqueue(ns, 'j2', 'ka', 'k2'),
            claim(ns, 'w2', ['ka'], now0 + 6_000),       # fence 1
            Step('worker_job.complete', {
                'task_id': f'{ns}_j2', 'worker_id': f'{ns}_w2',
                'fencing_token': 1, 'now_ms': now0 + 7_000,
                'terminal_status': 'failed', 'result_ref': 'ref-1',
                'replay_cursor': 3, 'error': {'code': 'boom'},
            }, command=True),
            # Settlement consumes the fence: replay is stale.
            Step('worker_job.complete', {
                'task_id': f'{ns}_j2', 'worker_id': f'{ns}_w2',
                'fencing_token': 1, 'now_ms': now0 + 8_000,
                'terminal_status': 'failed',
            }, command=True),
            Step('worker_job.get', {'task_id': f'{ns}_j2', 'user_id': user}),
            Step('worker_job.complete', {
                'task_id': f'{ns}_j2', 'worker_id': f'{ns}_w2',
                'fencing_token': 1, 'now_ms': now0 + 9_000,
                'terminal_status': 'bogus',
            }, command=True),
        ]),
        'worker job payload byte budgets': build(lambda ns: [
            enqueue(ns, 'j1', 'ka', 'k1',
                    payload={'blob': 'x' * (1024 * 1024)}),
            enqueue(ns, 'j2', 'ka', 'k2'),
            claim(ns, 'w1', ['ka'], now0),
            Step('worker_job.complete', {
                'task_id': f'{ns}_j2', 'worker_id': f'{ns}_w1',
                'fencing_token': 1, 'now_ms': now0 + 1_000,
                'terminal_status': 'failed',
                'error': {'blob': 'x' * (64 * 1024)},
            }, command=True),
        ]),
    }


def _search_scripts() -> dict[str, list[Step]]:
    """conversation.search deep path: ranking, snippets, visibility.

    Turns seed the canonical settled main-lane fragments on both
    authorities; each side's asynchronous projection worker rebuilds its
    search index, so every search read is an ``eventual`` step that polls
    until the result anchors (``eventual_min_hits``) and stabilizes.
    Negative probes (deleted/edited text must disappear) place a unique
    marker append AFTER the mutation and wait for the marker first, which
    orders the projection pipeline past the mutation on both workers.
    Query words are per-script unique because the module-scoped legacy
    authority keeps every earlier script's conversations.
    """
    user = _OWNER_ID

    def build(make: Callable[[str, str], list[Step]]) -> list[Step]:
        ns = f'diff_{uuid.uuid4().hex[:8]}'
        return make(ns, f'wq{uuid.uuid4().hex[:10]}')

    def append(conv: str, turn: str, at: int, content: str) -> Step:
        return Step('turn.append_settled', {
            'conversation_id': conv, 'user_id': user,
            'actor': 'human', 'status': 'completed',
            'projection': {'content': content},
            'lane_id': 'main', 'command_id': f'{turn}_command',
            'turn_id': turn, 'created_at': at, 'now': at,
            'conversation_defaults': {
                'allowCreate': True, 'title': 'Search scripts',
                'createdAt': _TURN_TS, 'settings': {},
            },
        }, command=True)

    def search(query: str, min_hits: int) -> Step:
        # Divergence-pinning reads spell out Step explicitly; this helper
        # covers the equal-on-both-authorities probes only.
        return Step('conversation.search', {
            'user_id': user, 'query': query,
            'limit': 50, 'snippet_radius': 10,
        }, eventual=True, eventual_min_hits=min_hits)

    return {
        'search ranking updated_at then id descending': build(lambda ns, w: [
            # Padding keeps the match offset past the snippet radius so
            # the equal-window path (not the start-clamp path) is what
            # compares here.
            append(f'{ns}_sra', f'{ns}_sra_t', _TURN_TS + 1_000,
                   f'ranking padding {w} probe alpha'),
            append(f'{ns}_srb', f'{ns}_srb_t', _TURN_TS + 2_000,
                   f'ranking padding {w} probe beta'),
            # Tied updated_at with srb: the id descending tie-break
            # surfaces src before srb on both authorities.
            append(f'{ns}_src', f'{ns}_src_t', _TURN_TS + 2_000,
                   f'ranking padding {w} probe gamma'),
            search(w, 3),
        ]),
        'search snippet truncation boundaries': build(lambda ns, w: [
            # Match at offset 0 with radius 10: both authorities clamp the
            # window start and take the full width from there.
            append(f'{ns}_sta', f'{ns}_sta_t', _TURN_TS + 1_000,
                   f'{w} ' + 'x' * 200),
            Step('conversation.search', {
                'user_id': user, 'query': w,
                'limit': 50, 'snippet_radius': 10,
            }, eventual=True, eventual_min_hits=1),
            # Match past the radius: both windows align exactly.
            append(f'{ns}_stb', f'{ns}_stb_t', _TURN_TS + 2_000,
                   'y' * 30 + f' {w}mid ' + 'z' * 200),
            search(f'{w}mid', 1),
        ]),
        'search multi-word AND and snippet width': build(lambda ns, w: [
            # Non-adjacent words take the AND fallback; both authorities
            # size the snippet window by the located term (words[0]), not
            # the full query.
            append(f'{ns}_sma', f'{ns}_sma_t', _TURN_TS + 1_000,
                   'm' * 30 + f' {w}one ' + 'n' * 30 + f' {w}two ' + 'p' * 30),
            Step('conversation.search', {
                'user_id': user, 'query': f'{w}one {w}two',
                'limit': 50, 'snippet_radius': 10,
            }, eventual=True, eventual_min_hits=1),
            # The same conversation through the single-word phrase path
            # keeps both snippet windows identical.
            search(f'{w}two', 1),
        ]),
        'search case folding ascii and unicode converge': build(lambda ns, w: [
            append(f'{ns}_sua', f'{ns}_sua_t', _TURN_TS + 1_000,
                   f'CAFÉ {w} NOTES{w}'),
            append(f'{ns}_sub', f'{ns}_sub_t', _TURN_TS + 2_000,
                   f'plain ascii padding {w}marker'),
            # The marker orders both projection pipelines past sua.
            search(f'{w}marker', 1),
            # ASCII case folding agrees on both engines.
            search(f'notes{w}', 1),
            # Full-Unicode folding agrees too: both authorities match CAFÉ
            # against café (legacy folds in Python; Tofu-DB to_lowercase).
            Step('conversation.search', {
                'user_id': user, 'query': 'café',
                'limit': 50, 'snippet_radius': 10,
            }, eventual=True),
        ]),
        'search edit and delete visibility': build(lambda ns, w: [
            append(f'{ns}_sda', f'{ns}_sda_t', _TURN_TS + 1_000,
                   f'{w}before edit'),
            search(f'{w}before', 1),
            # Editing the projection re-indexes the turn: the old text
            # must disappear and the new text must surface.
            Step('turn.projection.update', {
                'conversation_id': f'{ns}_sda', 'user_id': user,
                'turn_id': f'{ns}_sda_t', 'expected_projection_revision': 1,
                'projection': {'content': f'{w}after edit'},
            }, command=True, ignore_fields=frozenset({'updatedAt'})),
            append(f'{ns}_sdb', f'{ns}_sdb_t', _TURN_TS + 2_000,
                   f'visibility padding {w}markone tail'),
            search(f'{w}markone', 1),
            search(f'{w}before', 0),
            search(f'{w}after', 1),
            # Deleting the conversation removes it from the index.
            Step('conversation.delete', {
                'conv_id': f'{ns}_sda', 'user_id': user,
            }, command=True),
            append(f'{ns}_sdc', f'{ns}_sdc_t', _TURN_TS + 3_000,
                   f'visibility padding {w}marktwo tail'),
            search(f'{w}marktwo', 1),
            search(f'{w}after', 0),
        ]),
        'search limit bounds': [
            Step('conversation.search', {'user_id': user, 'query': 'x'}),
            # Two consecutive application-error queries are the observed
            # ceiling: the legacy client suppresses every later query
            # with database_unavailable for a window, which is client
            # protection, not search semantics (the radius bound probe
            # therefore lives in its own script below).
            Step('conversation.search', {
                'user_id': user, 'query': 'ab', 'limit': 0,
            }),
            Step('conversation.search', {
                'user_id': user, 'query': 'ab', 'limit': 201,
            }),
        ],
        'search snippet radius bound': [
            Step('conversation.search', {'user_id': user, 'query': 'zzqqx'}),
            Step('conversation.search', {
                'user_id': user, 'query': 'ab', 'snippet_radius': 401,
            }),
        ],
    }


def _task_results_scripts() -> dict[str, list[Step]]:
    """Task-result projection domain (unguarded checkpoint slice).

    Keys are script-unique inside the shared ``task_results`` namespace, so
    point reads only observe rows the script itself wrote.  ``summary_list``
    has no key filter, so it is always scoped by a script-unique ``conv_id``
    (its SQL pre-filter keeps ``scanned`` deterministic on the
    residue-carrying legacy authority).  ``requested_at_ms`` /
    ``abort_requested_at`` are wall-clock fields and sit in ``_DROP_FIELDS``.
    """
    user = _OWNER_ID

    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        return make(f'task_{uuid.uuid4().hex[:8]}')

    return {
        'task_results checkpoint CAS replay and conflict': build(lambda k: [
            # expected_version is mandatory on both authorities.
            Step('task_results.checkpoint', {
                'key': k, 'value': {'user_id': user, 'task_id': k,
                                    'status': 'pending'},
            }, command=True),
            Step('task_results.checkpoint', {
                'key': k, 'value': {'user_id': user, 'task_id': k,
                                    'status': 'pending', 'content': 'a'},
                'expected_version': 0,
            }, command=True),
            # Ambiguous-ACK replay: an identical value with a stale
            # witnessed version succeeds instead of conflicting.
            Step('task_results.checkpoint', {
                'key': k, 'value': {'user_id': user, 'task_id': k,
                                    'status': 'pending', 'content': 'a'},
                'expected_version': 0,
            }, command=True),
            # A stale DIFFERENT snapshot conflicts and never rolls back.
            Step('task_results.checkpoint', {
                'key': k, 'value': {'user_id': user, 'task_id': k,
                                    'status': 'running', 'content': 'b'},
                'expected_version': 0,
            }, command=True),
            # The witnessed version admits the next checkpoint.
            Step('task_results.checkpoint', {
                'key': k, 'value': {'user_id': user, 'task_id': k,
                                    'status': 'running', 'content': 'b'},
                'expected_version': 1,
            }, command=True),
        ]),
        'task_results guarded checkpoint owns parent fences and cache facts': build(
            lambda k: [
                Step('conversation.create', {
                    'conv_id': f'{k}_conv', 'user_id': user,
                    'title': 'Guarded checkpoint',
                    'settings': {'unrelated': 'preserved'},
                    'created_at': _CONV_TS, 'updated_at': _CONV_TS,
                }, command=True),
                Step('task_results.checkpoint', {
                    'key': f'{k}_older',
                    'value': {
                        'user_id': user, 'task_id': f'{k}_older',
                        'conv_id': f'{k}_conv', 'status': 'done',
                        'cache_prefix_hwm': 10,
                        'last_turn_cache_read': 3_000,
                    },
                    'expected_version': 0,
                    'guard_contract': 'tofu.task-results.checkpoint.guard/v1',
                    'require_parent': True,
                    'cache_settings_contract': (
                        'tofu.task-results.checkpoint.cache-settings/v1'),
                }, command=True),
                Step('task_results.checkpoint', {
                    'key': f'{k}_newer',
                    'value': {
                        'user_id': user, 'task_id': f'{k}_newer',
                        'conv_id': f'{k}_conv', 'status': 'done',
                        'cache_prefix_hwm': 20,
                        'last_turn_cache_read': 4_000,
                    },
                    'expected_version': 0,
                    'guard_contract': 'tofu.task-results.checkpoint.guard/v1',
                    'require_parent': True,
                    'cache_settings_contract': (
                        'tofu.task-results.checkpoint.cache-settings/v1'),
                }, command=True),
                # Ambiguous replay repairs monotonic HWM but must retain the
                # newer task's LWW last-read fact.
                Step('task_results.checkpoint', {
                    'key': f'{k}_older',
                    'value': {
                        'user_id': user, 'task_id': f'{k}_older',
                        'conv_id': f'{k}_conv', 'status': 'done',
                        'cache_prefix_hwm': 10,
                        'last_turn_cache_read': 3_000,
                    },
                    'expected_version': 0,
                    'guard_contract': 'tofu.task-results.checkpoint.guard/v1',
                    'require_parent': True,
                    'cache_settings_contract': (
                        'tofu.task-results.checkpoint.cache-settings/v1'),
                }, command=True),
                Step('conversation.get', {
                    'conv_id': f'{k}_conv', 'user_id': user,
                    'derive_messages': False,
                }),
                Step('task_results.checkpoint', {
                    'key': f'{k}_orphan',
                    'value': {
                        'user_id': user, 'task_id': f'{k}_orphan',
                        'conv_id': f'{k}_missing', 'status': 'pending',
                    },
                    'expected_version': 0,
                    'guard_contract': 'tofu.task-results.checkpoint.guard/v1',
                    'require_parent': True,
                }, command=True),
                Step('task_results.checkpoint', {
                    'key': f'{k}_newer',
                    'value': {
                        'user_id': user, 'task_id': f'{k}_newer',
                        'conv_id': f'{k}_conv', 'status': 'running',
                    },
                    'expected_version': 1,
                    'guard_contract': 'tofu.task-results.checkpoint.guard/v1',
                    'require_parent': True,
                }, command=True),
                Step('task_results.checkpoint', {
                    'key': f'{k}_invalid_cache',
                    'value': {
                        'user_id': user, 'task_id': f'{k}_invalid_cache',
                        'conv_id': f'{k}_conv', 'status': 'done',
                        'cache_prefix_hwm': 10,
                    },
                    'expected_version': 0,
                    'guard_contract': 'tofu.task-results.checkpoint.guard/v1',
                    'require_parent': True,
                }, command=True),
                Step('task_results.checkpoint', {
                    'key': f'{k}_invalid_guard',
                    'value': {
                        'user_id': user, 'task_id': f'{k}_invalid_guard',
                        'status': 'pending',
                    },
                    'expected_version': 0,
                    'guard_contract': 'tofu.task-results.checkpoint.guard/v999',
                    'require_parent': False,
                }, command=True),
            ]
        ),
        'task_results abort signals only owned live tasks': build(lambda k: [
            Step('task_results.abort', {
                'task_id': f'{k}_absent', 'user_id': user, 'source': 'test',
            }, command=True),
            Step('task_results.checkpoint', {
                'key': k, 'value': {'user_id': user, 'task_id': k,
                                    'status': 'running'},
                'expected_version': 0,
            }, command=True),
            Step('task_results.abort_requested',
                 {'task_id': k, 'user_id': user}),
            Step('task_results.abort', {
                'task_id': k, 'user_id': user, 'source': 'test',
            }, command=True),
            # Re-signaling is stable: reported, unchanged.
            Step('task_results.abort', {
                'task_id': k, 'user_id': user, 'source': 'test',
            }, command=True),
            Step('task_results.abort_requested',
                 {'task_id': k, 'user_id': user}),
            # A foreign owner is indistinguishable from a missing task.
            Step('task_results.abort', {
                'task_id': k, 'user_id': user + 1, 'source': 'test',
            }, command=True),
            Step('task_results.abort_requested',
                 {'task_id': k, 'user_id': user + 1}),
        ]),
        'task_results replay_get projects compact snapshots': build(
            lambda k: [
                Step('task_results.replay_get',
                     {'key': f'{k}_absent', 'user_id': user}),
                Step('task_results.checkpoint', {
                    'key': k,
                    'value': {'user_id': user, 'task_id': k,
                              'status': 'completed', 'content': 'answer',
                              'thinking': 'trace',
                              'metadata': {'rounds': 2},
                              'started_at': 100, 'completed_at': 200},
                    'expected_version': 0,
                }, command=True),
                # Heavy content/thinking stay inside the store by default.
                Step('task_results.replay_get', {'key': k, 'user_id': user}),
                Step('task_results.replay_get', {
                    'key': k, 'user_id': user,
                    'include_terminal_payload': True,
                    'include_metadata': True,
                }),
                # Flag types are validated on both authorities.
                Step('task_results.replay_get', {
                    'key': k, 'user_id': user,
                    'include_terminal_payload': 'yes',
                }),
                # A foreign owner sees nothing.
                Step('task_results.replay_get',
                     {'key': k, 'user_id': user + 1}),
            ]
        ),
        'task_results summary_list filters by conv and status': build(
            lambda k: [
                Step('task_results.checkpoint', {
                    'key': f'{k}_a',
                    'value': {'user_id': user, 'task_id': f'{k}_a',
                              'status': 'completed', 'conv_id': f'{k}_conv',
                              'content': 'aaa', 'completed_at': 300},
                    'expected_version': 0,
                }, command=True),
                Step('task_results.checkpoint', {
                    'key': f'{k}_b',
                    'value': {'user_id': user, 'task_id': f'{k}_b',
                              'status': 'running', 'conv_id': f'{k}_conv',
                              'content': 'bbb', 'started_at': 100},
                    'expected_version': 0,
                }, command=True),
                # The conv scope only ever matches this script's rows.
                Step('task_results.summary_list',
                     {'user_id': user, 'conv_id': f'{k}_conv'}),
                Step('task_results.summary_list', {
                    'user_id': user, 'conv_id': f'{k}_conv',
                    'status': 'completed',
                }),
                Step('task_results.summary_list', {
                    'user_id': user, 'conv_id': f'{k}_conv',
                    'completed_before_ms': 200,
                }),
                # Status strings are validated on both authorities.
                Step('task_results.summary_list', {'status': ''}),
            ]
        ),
        'task_results cost experiment scan uses compact owner index': build(
            lambda k: [
                Step('conversation.create', {
                    'conv_id': f'{k}_conv', 'user_id': user,
                    'title': 'Cost experiment',
                    'created_at': _CONV_TS, 'updated_at': _CONV_TS,
                }, command=True),
                Step('task_results.checkpoint', {
                    'key': k,
                    'value': {
                        'user_id': user, 'task_id': k,
                        'conv_id': f'{k}_conv', 'status': 'completed',
                        'completed_at': 2_000,
                        'content': 'heavy payload stays out of the scan',
                        'metadata': {'costExperiment': {
                            'experimentId': f'{k}_experiment',
                            'arm': 'control',
                        }},
                    },
                    'expected_version': 0,
                }, command=True),
                Step('task_results.cost_experiment_scan', {
                    'user_id': user,
                    'experiment_id': f'{k}_experiment',
                    'completed_at_gte': 1_000,
                    'limit': 10,
                    'scan_limit': 256,
                }, ignore_fields=frozenset({
                    'scanned', 'exhausted', 'next_cursor',
                })),
            ]
        ),
        'task_results recover running settles compact state': build(
            lambda k: [
                Step('task_results.checkpoint', {
                    'key': k,
                    'value': {
                        'user_id': user, 'task_id': k,
                        'conv_id': f'{k}_conv', 'status': 'running',
                        'completed_at': 1_234,
                        'content': 'payload is not rewritten by recovery',
                    },
                    'expected_version': 0,
                }, command=True),
                # Recovery is owner-wide; earlier scripts intentionally leave
                # live fixtures in the module-scoped legacy authority.
                Step('task_results.recover_running', {
                    'interrupted_reason': 'server_restart',
                    'max_rows': 32,
                    'scan_limit': 10_000,
                }, command=True, compare=False),
                Step('task_results.replay_get', {
                    'key': k, 'user_id': user,
                }),
            ]
        ),
    }


def _compaction_archive_scripts() -> dict[str, list[Step]]:
    """Compaction transcript archives and conversation-visibility coupling.

    ``created_at_ms`` rides the payload so archive metadata compares
    exactly; messages are small public dicts that pass the frozen-message
    codec unchanged on both authorities.  Every script deletes its
    archives and the conversation it creates, so the module-scoped legacy
    authority keeps its ``conversation.count`` baseline.  The visibility
    script pins the coupling the other way: trashing a conversation hides
    its archives (list/get raise not_found, update_summary reports
    updated:false) and restoring it revives them — both authorities must
    agree on every transition.
    """
    user = _OWNER_ID
    ts = 1_800_000_100_000

    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        return make(f'arch_{uuid.uuid4().hex[:8]}')

    def create_conv(c: str) -> Step:
        return Step('conversation.create', {
            'conv_id': c, 'user_id': user, 'title': 'Archives',
            'created_at': _CONV_TS, 'updated_at': _CONV_TS,
        }, command=True)

    def archive(c: str, aid: str, n: int, **extra: Any) -> Step:
        payload = {
            'conversation_id': c, 'user_id': user, 'archive_id': aid,
            'messages': [
                {'role': 'human', 'content': f'{aid} question'},
                {'role': 'assistant', 'content': f'{aid} answer {n}'},
            ],
            'summary': f'{aid} summary', 'receipt': {'status': 'ok'},
            'trigger': 'auto', 'task_id': f'{aid}_task', 'round_num': n,
            'model': 'model-x', 'tokens_before': 100 + n,
            'tokens_after': 50 + n, 'msgs_before': 4, 'msgs_after': 2,
            'reason': 'budget', 'created_at_ms': ts + n,
        }
        payload.update(extra)
        return Step('compaction_archive.create', payload, command=True)

    return {
        'compaction archive create get list round trip': build(lambda ns: [
            create_conv(ns),
            Step('compaction_archive.list',
                 {'conversation_id': ns, 'user_id': user}),
            archive(ns, f'{ns}_a1', 1),
            # Identical replay reports created:false (idempotent identity).
            archive(ns, f'{ns}_a1', 1),
            # The same identity carrying a different transcript conflicts.
            archive(ns, f'{ns}_a1', 1,
                    messages=[{'role': 'human', 'content': 'changed'}]),
            # messagesCount is the hydrated length with messages included
            # and the pinned msgs_before without them.
            Step('compaction_archive.get', {
                'conversation_id': ns, 'user_id': user,
                'archive_id': f'{ns}_a1',
            }),
            Step('compaction_archive.get', {
                'conversation_id': ns, 'user_id': user,
                'archive_id': f'{ns}_a1', 'include_messages': False,
            }),
            Step('compaction_archive.get', {
                'conversation_id': ns, 'user_id': user,
                'archive_id': f'{ns}_absent',
            }),
            Step('compaction_archive.list',
                 {'conversation_id': ns, 'user_id': user}),
            Step('compaction_archive.delete_conversation',
                 {'conversation_id': ns, 'user_id': user}, command=True),
            Step('conversation.delete', {'conv_id': ns, 'user_id': user},
                 command=True),
        ]),
        'compaction archive ownership and validation': build(lambda ns: [
            # Writes against a missing conversation report not_found.
            archive(f'{ns}_missing', f'{ns}_a1', 1),
            create_conv(ns),
            # The transcript must be a list of public message objects;
            # the private codec envelope is rejected at the boundary.
            archive(ns, f'{ns}_a2', 1, messages='not-a-list'),
            archive(ns, f'{ns}_a2', 1, messages=[{
                'role': 'human', 'content': 'x',
                '_tofuArchivedMessageCodec': {'version': 1},
            }]),
            # The receipt must be an object within its 32 KiB budget.
            archive(ns, f'{ns}_a2', 1, receipt='not-an-object'),
            archive(ns, f'{ns}_a2', 1, receipt={'blob': 'x' * 33_000}),
            archive(ns, f'{ns}_a2', 2),
            # Limit bounds validate after ownership (live conversation);
            # two error queries are the tail budget.
            Step('compaction_archive.list', {
                'conversation_id': ns, 'user_id': user, 'limit': 0,
            }),
            Step('compaction_archive.list', {
                'conversation_id': ns, 'user_id': user, 'limit': 1001,
            }),
            Step('compaction_archive.delete_conversation',
                 {'conversation_id': ns, 'user_id': user}, command=True),
            Step('conversation.delete', {'conv_id': ns, 'user_id': user},
                 command=True),
        ]),
        'compaction archive ordering update_summary and prune': build(
            lambda ns: [
                create_conv(ns),
                archive(ns, f'{ns}_a1', 1),
                archive(ns, f'{ns}_a2', 2, created_at_ms=ts + 9),
                archive(ns, f'{ns}_a3', 3, created_at_ms=ts + 9),
                # created_at ties fall back to archive_id order.
                Step('compaction_archive.list',
                     {'conversation_id': ns, 'user_id': user}),
                Step('compaction_archive.list', {
                    'conversation_id': ns, 'user_id': user, 'limit': 2,
                }),
                Step('compaction_archive.update_summary', {
                    'archive_id': f'{ns}_a2', 'user_id': user,
                    'summary': 'rewritten', 'tokens_after': 60,
                    'msgs_after': 3,
                }, command=True),
                Step('compaction_archive.get', {
                    'conversation_id': ns, 'user_id': user,
                    'archive_id': f'{ns}_a2', 'include_messages': False,
                }),
                # A receipt rewrite moves the derived result fields.
                Step('compaction_archive.update_summary', {
                    'archive_id': f'{ns}_a2', 'user_id': user,
                    'summary': 'rewritten', 'tokens_after': 60,
                    'msgs_after': 3,
                    'receipt': {'status': 'done', 'strategy': 'summarize'},
                }, command=True),
                Step('compaction_archive.list',
                     {'conversation_id': ns, 'user_id': user}),
                # Missing archives report updated:false instead of raising.
                Step('compaction_archive.update_summary', {
                    'archive_id': f'{ns}_absent', 'user_id': user,
                    'summary': 'x', 'tokens_after': 1, 'msgs_after': 1,
                }, command=True),
                # tokens_after/msgs_after are required bounded integers.
                Step('compaction_archive.update_summary', {
                    'archive_id': f'{ns}_a2', 'user_id': user,
                    'summary': 'x',
                }, command=True),
                Step('compaction_archive.update_summary', {
                    'archive_id': f'{ns}_a2', 'user_id': user,
                    'summary': 'x', 'tokens_after': -1, 'msgs_after': 1,
                }, command=True),
                # Newest-first retention: keep=2 retires the oldest.
                Step('compaction_archive.prune', {
                    'conversation_id': ns, 'user_id': user, 'keep': 2,
                }, command=True),
                Step('compaction_archive.list',
                     {'conversation_id': ns, 'user_id': user}),
                Step('compaction_archive.prune', {
                    'conversation_id': ns, 'user_id': user, 'keep': 1000,
                }, command=True),
                # keep is required and bounded.
                Step('compaction_archive.prune',
                     {'conversation_id': ns, 'user_id': user}, command=True),
                Step('compaction_archive.prune', {
                    'conversation_id': ns, 'user_id': user, 'keep': 0,
                }, command=True),
                Step('compaction_archive.delete_conversation',
                     {'conversation_id': ns, 'user_id': user}, command=True),
                Step('conversation.delete', {'conv_id': ns, 'user_id': user},
                     command=True),
            ]
        ),
        'compaction archive conversation visibility lifecycle': build(
            lambda ns: [
                create_conv(ns),
                archive(ns, f'{ns}_a1', 1),
                Step('compaction_archive.list',
                     {'conversation_id': ns, 'user_id': user}),
                Step('conversation.delete', {'conv_id': ns, 'user_id': user},
                     command=True),
                # A trashed conversation hides its archives: reads raise
                # not_found and summary updates report updated:false on
                # both authorities (single mid-script error query).
                Step('compaction_archive.list',
                     {'conversation_id': ns, 'user_id': user}),
                Step('compaction_archive.update_summary', {
                    'archive_id': f'{ns}_a1', 'user_id': user,
                    'summary': 'x', 'tokens_after': 1, 'msgs_after': 1,
                }, command=True),
                # Restore revives archive visibility: the archive rows
                # themselves were never touched by the trash cycle.
                Step('conversation.restore', {'conv_id': ns, 'user_id': user},
                     command=True),
                Step('compaction_archive.list',
                     {'conversation_id': ns, 'user_id': user}),
                Step('compaction_archive.get', {
                    'conversation_id': ns, 'user_id': user,
                    'archive_id': f'{ns}_a1',
                }),
                Step('compaction_archive.delete_conversation',
                     {'conversation_id': ns, 'user_id': user}, command=True),
                Step('compaction_archive.delete_conversation',
                     {'conversation_id': ns, 'user_id': user}, command=True),
                Step('compaction_archive.list',
                     {'conversation_id': ns, 'user_id': user}),
                # The archive id is reusable once the rows are gone.
                archive(ns, f'{ns}_a1', 1),
                Step('compaction_archive.delete_conversation',
                     {'conversation_id': ns, 'user_id': user}, command=True),
                Step('conversation.delete', {'conv_id': ns, 'user_id': user},
                     command=True),
                # Tail error-query budget: get/list on the trashed header.
                Step('compaction_archive.get', {
                    'conversation_id': ns, 'user_id': user,
                    'archive_id': f'{ns}_a1',
                }),
                Step('compaction_archive.list',
                     {'conversation_id': ns, 'user_id': user}),
            ]
        ),
    }


def _desktop_scripts() -> dict[str, list[Step]]:
    """Durable desktop egress-agent preference (one row per owner).

    ``updated_at_ms`` is a wall-clock field already in ``_DROP_FIELDS``;
    presence and the agent id compare exactly.  This single script owns
    the session owner's only preference row on the module-scoped legacy
    authority, so no other script may touch this domain.
    """
    user = _OWNER_ID
    return {
        'desktop egress agent initialize set lifecycle': [
            Step('desktop.egress_agent.get', {'owner_user_id': user}),
            Step('desktop.egress_agent.initialize', {
                'owner_user_id': user, 'agent_id': 'agent-alpha',
            }, command=True),
            # Initialization never overwrites: the first writer wins so a
            # late legacy-file import cannot clobber an explicit choice.
            Step('desktop.egress_agent.initialize', {
                'owner_user_id': user, 'agent_id': 'agent-beta',
            }, command=True),
            Step('desktop.egress_agent.get', {'owner_user_id': user}),
            Step('desktop.egress_agent.set', {
                'owner_user_id': user, 'agent_id': 'agent-beta',
            }, command=True),
            # The empty selection is a valid explicit value.
            Step('desktop.egress_agent.set', {
                'owner_user_id': user, 'agent_id': '',
            }, command=True),
            Step('desktop.egress_agent.get', {'owner_user_id': user}),
            Step('desktop.egress_agent.initialize', {
                'owner_user_id': user, 'agent_id': 'agent-gamma',
            }, command=True),
            # agent_id is required text within its bound; owner ids start
            # at 1.  Error commands are unlimited; the single error query
            # stays at the tail.
            Step('desktop.egress_agent.set', {'owner_user_id': user},
                 command=True),
            Step('desktop.egress_agent.set', {
                'owner_user_id': user, 'agent_id': 'x' * 129,
            }, command=True),
            Step('desktop.egress_agent.initialize', {
                'owner_user_id': 0, 'agent_id': 'a',
            }, command=True),
            Step('desktop.egress_agent.get', {'owner_user_id': 0}),
        ],
    }


def _tool_result_artifact_scripts() -> dict[str, list[Step]]:
    """Content-addressed tool-result artifacts: TTL, ranges, search.

    Every clock (``created_at_ms``/``expires_at_ms``/``now_ms``) rides the
    payload, so the SHA-256-addressed documents compare byte-exactly; the
    namespace sits inside the content, making each script's digest unique
    on the module-scoped legacy authority.  Read cursors are byte offsets
    that must snap to UTF-8 code point boundaries — the multibyte probes
    pin that contract on both engines.
    """
    user = _OWNER_ID
    ts = 1_800_000_200_000
    day = 24 * 60 * 60 * 1000

    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        return make(f'artifact_{uuid.uuid4().hex[:8]}')

    return {
        'tool result artifact put read round trip and ranges': build(
            lambda ns: [
                Step('tool_result_artifact.put', {
                    'user_id': user, 'content': f'ab\U0001F680cd{ns}',
                    'created_at_ms': ts, 'expires_at_ms': ts + 1_000,
                    'media_type': 'text/markdown',
                }, command=True),
                # Re-putting the same bytes keeps the later expiry (max).
                Step('tool_result_artifact.put', {
                    'user_id': user, 'content': f'ab\U0001F680cd{ns}',
                    'created_at_ms': ts + 100, 'expires_at_ms': ts + 500,
                }, command=True),
                Step('tool_result_artifact.put', {
                    'user_id': user, 'content': f'ab\U0001F680cd{ns}',
                    'created_at_ms': ts + 200, 'expires_at_ms': ts + 2_000,
                }, command=True),
                Step('tool_result_artifact.read', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'now_ms': ts + 300,
                }),
                # Byte cursors snap to UTF-8 boundaries: the 4-byte rocket
                # at offset 2 must never split across ranges.
                Step('tool_result_artifact.read', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'now_ms': ts + 300, 'limit': 4,
                }),
                Step('tool_result_artifact.read', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'now_ms': ts + 300, 'offset': 2, 'limit': 4,
                }),
                Step('tool_result_artifact.read', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'now_ms': ts + 300, 'offset': 6,
                }),
                # An out-of-range offset clamps to an empty final page.
                Step('tool_result_artifact.read', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'now_ms': ts + 300, 'offset': 10_000,
                }),
                # Past the extended expiry the artifact is gone; unknown
                # well-formed digests read as null.
                Step('tool_result_artifact.read', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'now_ms': ts + 2_001,
                }),
                Step('tool_result_artifact.read', {
                    'user_id': user,
                    'artifact_ref': 'tool-result:' + '0' * 64,
                    'now_ms': ts + 300,
                }),
                # TTL is capped at seven days; expiry must follow creation.
                Step('tool_result_artifact.put', {
                    'user_id': user, 'content': f'{ns}ttl',
                    'created_at_ms': ts, 'expires_at_ms': ts + 8 * day,
                }, command=True),
                Step('tool_result_artifact.put', {
                    'user_id': user, 'content': f'{ns}exp',
                    'created_at_ms': ts, 'expires_at_ms': ts,
                }, command=True),
                # Malformed refs fail validation (tail error query).
                Step('tool_result_artifact.read', {
                    'user_id': user, 'artifact_ref': 'tool-result:xyz',
                    'now_ms': ts + 300,
                }),
            ]
        ),
        'tool result artifact search windows and pagination': build(
            lambda ns: [
                Step('tool_result_artifact.put', {
                    'user_id': user,
                    'content': (
                        ('x' * 200) + 'needle' + ('y' * 400) + 'NEEDLE'
                        + 'z\ufb00' + ns
                    ),
                    'created_at_ms': ts, 'expires_at_ms': ts + day,
                }, command=True),
                # casefold matches needle and NEEDLE; context windows clip
                # at 160/320 characters around each hit.
                Step('tool_result_artifact.search', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'query': 'needle', 'now_ms': ts + 1,
                }),
                Step('tool_result_artifact.search', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'query': 'NEEDLE', 'now_ms': ts + 1, 'limit': 1,
                }),
                # The cursor from the truncated page resumes at hit two.
                Step('tool_result_artifact.search', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'query': 'needle', 'now_ms': ts + 1, 'cursor': 526,
                }),
                Step('tool_result_artifact.search', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'query': 'absent', 'now_ms': ts + 1,
                }),
                # Unicode folding parity: legacy casefolds (U+FB00 -> ff)
                # while a lowercase-only engine cannot match the ligature.
                Step('tool_result_artifact.search', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'query': 'ff', 'now_ms': ts + 1,
                }),
                # Tail error-query budget: limit bound, missing query.
                Step('tool_result_artifact.search', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'query': 'needle', 'now_ms': ts + 1, 'limit': 21,
                }),
                Step('tool_result_artifact.search', {
                    'user_id': user,
                    'artifact_ref': _Ref(0, ('artifactRef',)),
                    'now_ms': ts + 1,
                }),
            ]
        ),
        'tool result artifact prune is bounded and excludes future rows': build(
            lambda ns: [
                Step('tool_result_artifact.put', {
                    'user_id': user, 'content': f'{ns}-expired-a',
                    'created_at_ms': ts - day,
                    'expires_at_ms': ts - day + 100,
                }, command=True),
                Step('tool_result_artifact.put', {
                    'user_id': user, 'content': f'{ns}-expired-b',
                    'created_at_ms': ts - day,
                    'expires_at_ms': ts - day + 100,
                }, command=True),
                Step('tool_result_artifact.put', {
                    'user_id': user, 'content': f'{ns}-future',
                    'created_at_ms': ts - day,
                    'expires_at_ms': ts + day,
                }, command=True),
                Step('tool_result_artifact.prune', {
                    'now_ms': ts - day + 200, 'limit': 1,
                }, maintenance=True),
                Step('tool_result_artifact.prune', {
                    'now_ms': ts - day + 200, 'limit': 5_000,
                }, maintenance=True),
                Step('tool_result_artifact.read', {
                    'user_id': user, 'artifact_ref': _Ref(0, ('artifactRef',)),
                    'now_ms': ts - day + 50,
                }),
                Step('tool_result_artifact.read', {
                    'user_id': user, 'artifact_ref': _Ref(1, ('artifactRef',)),
                    'now_ms': ts - day + 50,
                }),
                Step('tool_result_artifact.read', {
                    'user_id': user, 'artifact_ref': _Ref(2, ('artifactRef',)),
                    'now_ms': ts,
                }),
                Step('tool_result_artifact.prune', {
                    'now_ms': ts - day + 200, 'limit': 5_000,
                }, maintenance=True),
                Step('tool_result_artifact.prune', {
                    'now_ms': 0,
                }, maintenance=True),
                Step('tool_result_artifact.prune', {
                    'now_ms': ts, 'limit': 5_001,
                }, maintenance=True),
            ]
        ),
    }


def _browser_observation_scripts() -> dict[str, list[Step]]:
    """Browser site observations: confidence state machine and expiry.

    ``observed_at_ms`` rides the payload, so every derived timestamp
    (verified/observed/expiry) compares exactly.  The probes walk the
    full confidence ladder: first success 500, same-strategy +100,
    strategy switch 400, mismatch -250, not_observed -100, three
    consecutive failures quarantining the row, and success resetting the
    streak.  ``operation`` is namespaced per script; the owner LRU budget
    (200 rows) is deliberately out of reach.
    """
    user = _OWNER_ID
    ts = 1_800_000_300_000
    retention = 30 * 24 * 60 * 60 * 1000

    def build(make: Callable[[str], list[Step]]) -> list[Step]:
        return make(f'browser_{uuid.uuid4().hex[:8]}')

    def identity(ns: str) -> dict[str, Any]:
        return {
            'owner_user_id': user, 'origin': 'https://example.com',
            'route_family': '/projects/{segment}', 'operation': ns,
        }

    def get(ns: str, now: int, **extra: Any) -> Step:
        payload = {**identity(ns), 'now_ms': now}
        payload.update(extra)
        return Step('browser.site_observation.get', payload)

    def record(ns: str, outcome: str, at: int, **extra: Any) -> Step:
        payload = {**identity(ns), 'observed_at_ms': at, 'outcome': outcome}
        payload.update(extra)
        return Step('browser.site_observation.record', payload, command=True)

    def observation(
        strategy: str, elapsed: int, *,
        hints: list[dict[str, Any]] | None = None,
        used: bool = False, matched: bool = False,
    ) -> dict[str, Any]:
        return {
            'schema_version': 1, 'strategy': strategy,
            'api_hints': hints or [], 'elapsed_ms': elapsed,
            'capture_hint_used': used, 'capture_hint_matched': matched,
            'anti_bot_vendor': '', 'auth_signal': 'none',
        }

    hint = {
        'method': 'GET', 'origin': 'https://example.com',
        'path_template': '/api/{segment}',
        'shape_summary': {'$.items': 'array(3)'},
        'score': 0.5, 'passive_only': True,
    }

    return {
        'browser site observation state machine and expiry': build(
            lambda ns: [
                get(ns, ts),
                # A failure with no existing row creates nothing (null).
                record(ns, 'not_observed', ts),
                get(ns, ts + 1),
                # First success: confidence 500, every counter at one.
                record(ns, 'success', ts + 2, observation=observation(
                    'captured_api', 100, hints=[hint],
                    used=True, matched=True,
                )),
                get(ns, ts + 3),
                # Same strategy: +100 confidence, visits accumulate.
                record(ns, 'success', ts + 4, observation=observation(
                    'captured_api', 200,
                )),
                # Strategy switch: confidence reseeds to 400.
                record(ns, 'success', ts + 5, observation=observation(
                    'rendered_dom', 300,
                )),
                # auth_challenge keeps confidence but flips the signal.
                record(ns, 'auth_challenge', ts + 6),
                # Failure ladder: -250, -250 clamps at 0, then -100; the
                # third consecutive failure quarantines the row.
                record(ns, 'structure_mismatch', ts + 7),
                record(ns, 'not_found', ts + 8),
                record(ns, 'not_observed', ts + 9),
                get(ns, ts + 10),
                # Success resets the streak and reactivates the row.
                record(ns, 'success', ts + 11, observation=observation(
                    'rendered_dom', 400,
                )),
                get(ns, ts + 12),
                # Expiry boundary: live one millisecond before, gone at.
                get(ns, ts + 11 + retention - 1),
                get(ns, ts + 11 + retention),
                # Validation (error commands are unlimited).
                record(ns, 'bogus', ts + 13),
                record(ns, 'success', ts + 13),
                record(ns, 'success', ts + 13, observation=observation(
                    'captured_api', 100, hints=[
                        {**hint, 'passive_only': False},
                    ], used=True,
                )),
                record(ns, 'success', ts + 13, observation=observation(
                    'captured_api', 100, matched=True,
                )),
                record(ns, 'success', ts + 13, observation=observation(
                    'captured_api', 120_001,
                )),
                record(ns, 'success', ts + 13,
                       origin='https://example.com/path',
                       observation=observation('captured_api', 100)),
                # Tail error-query budget (legacy-client breaker).
                get(ns, ts + 14, origin='not-a-url'),
                get(ns, ts + 14, route_family='/users/123'),
            ]
        ),
    }


def _tenant_user_scripts() -> dict[str, list[Step]]:
    account = f'tenant-user-{uuid.uuid4().hex[:12]}'
    email = f'{account}@example.com'
    return {
        'tenant users preserve account control and secret projection': [
            Step('tenant.user.create', {
                'user_id': account, 'email': f'  {email.upper()}  ',
                'password_hash': 'sealed-password', 'display_name': '  Owner  ',
                'role': 'user', 'created_at': 100,
                'metadata': {'plan': 'personal'},
            }, command=True),
            Step('tenant.user.get', {'email': email.upper()}),
            Step('tenant.user.list', {'limit': 10, 'offset': 0}),
            Step('tenant.user.authentication', {'email': email}),
            Step('tenant.user.set_role', {
                'user_id': account, 'role': 'admin',
            }, command=True),
            Step('tenant.user.set_status', {
                'user_id': account, 'status': 'suspended',
            }, command=True),
            Step('tenant.user.record_login', {
                'user_id': account, 'last_login_at': 200,
            }, command=True),
            Step('tenant.user.list', {
                'status': 'suspended', 'limit': 10, 'offset': 0,
            }),
            Step('tenant.user.get', {'user_id': account}),
            Step('tenant.user.get', {'user_id': f'{account}-missing'}),
            Step('tenant.user.authentication', {
                'email': f'missing-{email}',
            }),
            Step('tenant.user.record_login', {
                'user_id': f'{account}-missing', 'last_login_at': 300,
            }, command=True),
        ],
    }


def _credential_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:12]
    credential_id = f'credential-{suffix}'
    bootstrap_id = f'credential-bootstrap-{suffix}'
    secret_hash = uuid.uuid4().hex * 2
    bootstrap_hash = uuid.uuid4().hex * 2
    boundary = {'owner_user_id': _OWNER_ID, 'tenant_id': ' personal '}
    create = {
        **boundary,
        'credential_id': credential_id,
        'account_user_id': '',
        'name': '  Agent key  ',
        'prefix': 'tf_live_',
        'secret_hash': secret_hash,
        'scopes': ['write', 'read', 'write'],
        'rate_limit_rpm': 60,
        'rate_limit_tpd': 1000,
        'created_at': 100.5,
        'expires_at': 1000.5,
        'metadata': {'source': 'differential'},
    }
    return {
        'credentials preserve authority lifecycle and public projection': [
            Step('credential.create', create, command=True),
            Step('credential.get', {**boundary, 'credential_id': credential_id}),
            Step('credential.list', boundary),
            Step('credential.exists', boundary),
            Step('credential.validate', {'secret_hash': secret_hash, 'now': 200}),
            Step('credential.authenticate', {'secret_hash': secret_hash, 'now': 250}, command=True),
            Step('credential.touch', {**boundary, 'credential_id': credential_id, 'used_at': 300, 'touch_if_before': 200}, command=True),
            Step('credential.touch', {**boundary, 'credential_id': credential_id, 'used_at': 350, 'touch_if_before': 300}, command=True),
            Step('credential.update', {**boundary, 'credential_id': credential_id, 'updates': {'name': '  Renamed  ', 'scopes': [], 'rate_limit_rpm': 70, 'rate_limit_tpd': 1100, 'expires_at': None, 'disabled': False, 'metadata': {'rotated': True}}}, command=True),
            Step('credential.create_if_owner_empty', {**create, 'credential_id': bootstrap_id, 'account_user_id': '', 'secret_hash': bootstrap_hash}, command=True),
            Step('credential.validate', {'secret_hash': secret_hash, 'now': 400}),
            Step('credential.identify', {'secret_hash': secret_hash}),
            Step('credential.revoke', {**boundary, 'credential_id': credential_id, 'revoked_at': 450}, command=True),
            Step('credential.get', {**boundary, 'credential_id': credential_id}),
            Step('credential.exists', boundary),
            Step('credential.identify', {'secret_hash': secret_hash}),
            Step('credential.create_if_owner_empty', {**create, 'credential_id': bootstrap_id, 'account_user_id': '', 'secret_hash': bootstrap_hash}, command=True),
            Step('credential.list', boundary),
        ],
    }


def _billing_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:12]
    user = f'billing-{suffix}'
    payment_id = f'payment-{suffix}'
    second_payment_id = f'payment-second-{suffix}'
    provider_id = f'provider-payment-{suffix}'
    return {
        'billing wallet and ledger preserve atomic money invariants': [
            Step('billing.wallet.get', {'user_id': user}),
            Step('billing.wallet.apply', {
                'user_id': user, 'amount_micro': 10000, 'kind': 'topup',
                'ref_type': 'payment', 'ref_id': 'initial', 'note': 'topup',
                'ledger_id': f'ledger-topup-{suffix}', 'occurred_at': 100,
                'allow_negative': False,
            }, command=True),
            Step('billing.wallet.apply', {
                'user_id': user, 'amount_micro': 10000, 'kind': 'topup',
                'ref_type': 'payment', 'ref_id': 'initial', 'note': 'duplicate',
                'ledger_id': f'ledger-duplicate-{suffix}', 'occurred_at': 101,
                'allow_negative': False,
            }, command=True),
            Step('billing.wallet.apply', {
                'user_id': user, 'amount_micro': -1500, 'kind': 'reserve',
                'ref_type': 'reserve', 'ref_id': 'task-a', 'note': 'reserve',
                'ledger_id': f'ledger-reserve-{suffix}', 'occurred_at': 102,
                'allow_negative': False,
            }, command=True),
            Step('billing.reserve.stale', {'cutoff_ts': 102, 'limit': 100}),
            Step('billing.wallet.apply', {
                'user_id': user, 'amount_micro': -20000, 'kind': 'debit',
                'ref_type': 'task', 'ref_id': 'too-large', 'note': '',
                'ledger_id': f'ledger-insufficient-{suffix}', 'occurred_at': 103,
                'allow_negative': False,
            }, command=True),
            Step('billing.wallet.settle', {
                'user_id': user, 'ref_id': 'task-a', 'reserved_micro': 1500,
                'actual_micro': 900, 'note': 'settle',
                'release_id': f'ledger-release-{suffix}',
                'debit_id': f'ledger-debit-{suffix}',
            }, command=True, ignore_fields=frozenset({'updated_at'})),
            Step('billing.reserve.stale', {'cutoff_ts': 102, 'limit': 100}),
            Step('billing.wallet.settle', {
                'user_id': user, 'ref_id': 'task-a', 'reserved_micro': 1500,
                'actual_micro': 900, 'note': 'settle',
                'release_id': f'ledger-release-2-{suffix}',
                'debit_id': f'ledger-debit-2-{suffix}',
            }, command=True, ignore_fields=frozenset({'updated_at'})),
            Step('billing.ledger.find', {
                'user_id': user, 'kind': 'topup',
                'ref_type': 'payment', 'ref_id': 'initial',
            }),
            Step('billing.ledger.append', {
                'id': f'ledger-bonus-{suffix}', 'user_id': user, 'ts': 104,
                'amount_micro': 25, 'kind': 'bonus', 'ref_type': '',
                'ref_id': '', 'balance_after_micro': 9125, 'note': 'audit',
            }, command=True),
            Step('billing.ledger.list', {
                'user_id': user, 'limit': 20, 'offset': 0, 'kinds': [],
            }, ignore_fields=frozenset({'ts'})),
            Step('billing.ledger.recompute', {'user_id': user}),
            Step('billing.wallet.get', {'user_id': user},
                 ignore_fields=frozenset({'updated_at'})),
        ],
        'billing payments preserve provider idempotency and atomic settlement': [
            Step('billing.payment.record', {
                'id': payment_id, 'user_id': user, 'provider': 'stripe',
                'provider_id': provider_id, 'amount_minor': 499,
                'currency': 'USD', 'credit_micro': 500,
                'status': 'pending', 'raw': {'event': 'created'},
            }, command=True, ignore_fields=frozenset({'created_at'})),
            # Legacy returns the existing row before validating all other fields.
            Step('billing.payment.record', {
                'provider': 'stripe', 'provider_id': provider_id,
            }, command=True, ignore_fields=frozenset({'created_at'})),
            Step('billing.payment.find', {
                'provider': 'stripe', 'provider_id': provider_id,
            }, ignore_fields=frozenset({'created_at'})),
            Step('billing.payment.record', {
                'id': second_payment_id, 'user_id': user, 'provider': 'stripe',
                'provider_id': f'{provider_id}-second', 'amount_minor': 10,
                'currency': 'USD', 'credit_micro': 0,
                'status': 'failed', 'raw': {},
            }, command=True, ignore_fields=frozenset({'created_at'})),
            Step('billing.payment.list', {
                'user_id': user, 'provider': 'stripe', 'status': '',
                'limit': 20, 'offset': 0,
            }, ignore_fields=frozenset({'created_at'})),
            Step('billing.payment.settle', {
                'payment_id': payment_id, 'ledger_id': f'payment-ledger-{suffix}',
                'raw': {'event': 'settled'},
            }, command=True, ignore_fields=frozenset({'created_at', 'settled_at'})),
            # Already-settled lookup must not require another ledger ID.
            Step('billing.payment.settle', {
                'payment_id': payment_id,
            }, command=True, ignore_fields=frozenset({'created_at', 'settled_at'})),
            Step('billing.payment.list', {
                'user_id': user, 'provider': '', 'status': 'settled',
                'limit': 20, 'offset': 0,
            }, ignore_fields=frozenset({'created_at', 'settled_at'})),
            Step('billing.wallet.get', {'user_id': user},
                 ignore_fields=frozenset({'updated_at'})),
        ],
        'billing redemption codes preserve packed mint and atomic consumption': [
            Step('billing.redeem_codes.mint', {
                'codes': [f'redeem-z-{suffix}', f'redeem-a-{suffix}'],
                'amount_micro': 250, 'batch': f'batch-{suffix}',
                'created_by': 'admin', 'created_at': 100, 'expires_at': 0,
                'note': 'launch codes',
            }, command=True),
            Step('billing.redeem_codes.mint', {
                'codes': [f'redeem-expired-{suffix}'],
                'amount_micro': 125, 'batch': f'expired-{suffix}',
                'created_by': '', 'created_at': 101, 'expires_at': 100,
                'note': '',
            }, command=True),
            Step('billing.redeem_codes.list', {
                'batch': '', 'status': 'all', 'limit': 20, 'offset': 0,
            }),
            Step('billing.redeem_code.apply', {
                'code': f'redeem-a-{suffix}', 'user_id': user,
                'redeemed_at': 100, 'ledger_id': f'redeem-ledger-{suffix}',
            }, command=True, ignore_fields=frozenset({'updated_at'})),
            Step('billing.redeem_code.apply', {
                'code': f'redeem-a-{suffix}', 'user_id': user,
                'redeemed_at': 102, 'ledger_id': f'redeem-repeat-{suffix}',
            }, command=True),
            Step('billing.redeem_code.apply', {
                'code': f'redeem-missing-{suffix}', 'user_id': user,
                'redeemed_at': 102, 'ledger_id': f'redeem-missing-ledger-{suffix}',
            }, command=True),
            Step('billing.redeem_code.apply', {
                'code': f'redeem-expired-{suffix}', 'user_id': user,
                'redeemed_at': 101, 'ledger_id': f'redeem-expired-ledger-{suffix}',
            }, command=True),
            Step('billing.redeem_codes.list', {
                'batch': f'batch-{suffix}', 'status': 'redeemed',
                'limit': 20, 'offset': 0,
            }),
            Step('billing.redeem_codes.list', {
                'batch': f'batch-{suffix}', 'status': 'unredeemed',
                'limit': 20, 'offset': 0,
            }),
            Step('billing.wallet.get', {'user_id': user},
                 ignore_fields=frozenset({'updated_at'})),
        ],
    }


def _orchestration_scripts() -> dict[str, list[Step]]:
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID
    label = f'orch-{suffix}'
    definition_id = f'definition-{suffix}'
    run_id = f'run-{suffix}'
    clock_fields = frozenset({'created_at', 'updated_at', 'finished_at'})
    goal_clock_fields = frozenset({'createdAt', 'updatedAt', 'finishedAt'})
    goal_policy = {
        'solutionHorizon': 'long_term',
        'rootCauseRequired': True,
        'verificationEvidenceRequired': True,
        'temporaryPatchPolicy': 'reject_when_robust_solution_is_in_scope',
        'iterationBudget': {'default': 40, 'hardCeiling': 64},
        'directive': (
            'Pursue the stated objective for durable long-term benefit. '
            'Diagnose and fix root causes, require concrete verification '
            'evidence, and do not substitute a temporary patch when a robust '
            'maintainable solution is within the delegated scope.'
        ),
    }
    owner = {'user_id': user, 'tenant_id': label}
    return {
        'orchestration definitions preserve global identity and monotonic CAS': [
            Step('orchestration.definition.create', {
                **owner, 'orchestration_id': definition_id, 'now_ms': 100,
                'definition': {'name': 'First', 'nodes': [{'id': 'a'}]},
            }, command=True),
            Step('orchestration.definition.get', {
                **owner, 'orchestration_id': definition_id,
            }),
            Step('orchestration.definition.list', owner),
            Step('orchestration.definition.update', {
                **owner, 'orchestration_id': definition_id,
                'expected_updated_at': 99, 'now_ms': 101,
                'definition': {'name': 'Conflict', 'nodes': []},
            }, command=True),
            Step('orchestration.definition.update', {
                **owner, 'orchestration_id': definition_id,
                'expected_updated_at': 100, 'now_ms': 90,
                'definition': {'name': 'Second', 'nodes': [{'id': 'b'}]},
            }, command=True),
            Step('orchestration.definition.delete', {
                **owner, 'orchestration_id': definition_id,
                'expected_updated_at': 100,
            }, command=True),
            Step('orchestration.definition.delete', {
                **owner, 'orchestration_id': definition_id,
                'expected_updated_at': 101,
            }, command=True),
            Step('orchestration.definition.get', {
                **owner, 'orchestration_id': definition_id,
            }),
        ],
        'orchestration run events projection terminal fencing and deletion': [
            Step('orchestration.run.create', {
                **owner, 'run_id': run_id, 'orch_id': definition_id,
                'name': 'Differential', 'definition': {'nodes': []},
                'input': 'go', 'created_by': 'test',
            }, command=True),
            Step('orchestration.run.get', {
                **owner, 'run_id': run_id,
            }, ignore_fields=clock_fields),
            Step('orchestration.run.list', {
                **owner, 'orch_id': definition_id, 'limit': 20,
            }, ignore_fields=clock_fields),
            Step('orchestration.event.append', {
                **owner, 'run_id': run_id, 'sequence': 0,
                'event': {'type': 'created', 'node_id': 'root'},
            }, command=True),
            Step('orchestration.event.project', {
                **owner, 'run_id': run_id, 'sequence': 1,
                'event': {'type': 'running', 'node_id': 'root'},
                'status': 'running',
            }, command=True),
            Step('orchestration.event.project', {
                **owner, 'run_id': run_id, 'sequence': 1,
                'event': {'type': 'running', 'node_id': 'root'},
                'status': 'running',
            }, command=True),
            Step('orchestration.event.append', {
                **owner, 'run_id': run_id, 'sequence': 1,
                'event': {'type': 'conflict'},
            }, command=True),
            Step('orchestration.event.page', {
                **owner, 'run_id': run_id, 'cursor': 0,
            }),
            Step('orchestration.event.page', {
                **owner, 'run_id': run_id, 'cursor': 99,
            }),
            Step('orchestration.run.update_status', {
                **owner, 'run_id': run_id, 'status': 'done',
                'final': 'complete', 'error': None,
            }, command=True),
            Step('orchestration.event.project', {
                **owner, 'run_id': run_id, 'sequence': 2,
                'event': {'type': 'late'}, 'status': 'running',
            }, command=True),
            Step('orchestration.run.list', {
                **owner, 'status': 'done', 'limit': 20,
            }, ignore_fields=clock_fields),
            Step('orchestration.run.delete', {
                **owner, 'run_id': run_id,
            }, command=True),
            Step('orchestration.run.get', {**owner, 'run_id': run_id}),
            Step('orchestration.event.page', {
                **owner, 'run_id': run_id, 'cursor': 0,
            }),
        ],
        'orchestration owner and startup retirement settle bounded active runs': [
            Step('orchestration.run.create', {
                **owner, 'run_id': f'{run_id}-owner',
                'definition': {'nodes': []},
            }, command=True),
            Step('orchestration.run.retire_interrupted', {
                **owner, 'error': {'kind': 'worker_lost'},
            }, command=True),
            Step('orchestration.run.get', {
                **owner, 'run_id': f'{run_id}-owner',
            }, ignore_fields=clock_fields),
            Step('orchestration.run.create', {
                **owner, 'run_id': f'{run_id}-global',
                'definition': {'nodes': []},
            }, command=True),
            Step('orchestration.run.retire_interrupted_all', {
                'error': {'kind': 'restart'},
            }, maintenance=True, compare=False),
            Step('orchestration.run.get', {
                **owner, 'run_id': f'{run_id}-global',
            }, ignore_fields=clock_fields),
        ],
        'goal runs atomically supersede and freeze terminal meaning': [
            Step('goal.run.start', {
                **owner, 'run_id': f'goal-a-{suffix}',
                'conversation_id': f'conversation-{suffix}',
                'objective': 'Ship the durable system',
                'definition': {'nodes': [{'id': 'work'}]},
                'policy': goal_policy,
            }, command=True, ignore_fields=goal_clock_fields),
            Step('goal.run.get', {
                **owner, 'run_id': f'goal-a-{suffix}',
            }, ignore_fields=goal_clock_fields),
            Step('goal.run.start', {
                **owner, 'run_id': f'goal-b-{suffix}',
                'conversation_id': f'conversation-{suffix}',
                'objective': 'Verify the release',
                'definition': {'nodes': [{'id': 'verify'}]},
                'policy': goal_policy,
            }, command=True, ignore_fields=goal_clock_fields),
            Step('goal.run.latest', {
                **owner, 'conversation_id': f'conversation-{suffix}',
            }, ignore_fields=goal_clock_fields),
            Step('goal.run.transition', {
                **owner, 'run_id': f'goal-b-{suffix}',
                'status': 'completed', 'reason': 'objective_verified',
                'final': 'verified', 'outcome': {'tests': 'passed'},
            }, command=True, ignore_fields=goal_clock_fields),
            Step('goal.run.transition', {
                **owner, 'run_id': f'goal-b-{suffix}',
                'status': 'completed', 'reason': 'objective_verified',
                'final': 'verified', 'outcome': {'tests': 'passed'},
            }, command=True, ignore_fields=goal_clock_fields),
            Step('goal.run.transition', {
                **owner, 'run_id': f'goal-b-{suffix}',
                'status': 'failed', 'reason': 'runtime_failure',
                'final': '', 'outcome': {},
            }, command=True),
        ],
    }


def _swarm_scripts() -> dict[str, list[Step]]:
    swarm_key = f'swarm-{uuid.uuid4().hex[:8]}'
    return {
        'swarm checkpoints delivery quarantine repair and lifecycle': [
            Step('swarm.session.save', {
                'swarm_key': swarm_key, 'conv_id': 'conv-first',
                'task_id': 'task-first', 'status': 'running',
                'specs': [{'id': 'b'}, {'id': 'a'}],
                'config': {'model': 'test'}, 'now_ms': 100,
            }, command=True),
            Step('swarm.session.save', {
                'swarm_key': swarm_key, 'conv_id': 'conv-second',
                'task_id': 'task-second', 'status': 'running',
                'specs': [{'id': 'b'}, {'id': 'a'}],
                'config': {'model': 'test-2'}, 'now_ms': 200,
            }, command=True),
            Step('swarm.agent.save', {
                'swarm_key': swarm_key, 'agent_id': 'b', 'role': 'researcher',
                'objective': 'completed result', 'status': 'completed',
                'messages': [{'role': 'assistant', 'content': 'done'}],
                'result': {'final_answer': 'done'}, 'rounds_used': 1,
                'delivered': False, 'now_ms': 300,
            }, command=True),
            Step('swarm.agent.save', {
                'swarm_key': swarm_key, 'agent_id': 'a', 'role': 'coder',
                'objective': 'resume safely', 'status': 'running',
                'messages': [{'role': 'user', 'content': 'continue'}],
                'result': {}, 'rounds_used': 2, 'delivered': True,
                'now_ms': 400,
            }, command=True),
            Step('swarm.agent.save', {
                'swarm_key': swarm_key, 'agent_id': 'a', 'role': 'coder',
                'objective': 'resume safely', 'status': 'running',
                'messages': [{'role': 'user', 'content': 'continue safely'}],
                'result': {}, 'rounds_used': 3, 'delivered': None,
                'now_ms': 500,
            }, command=True),
            Step('swarm.session.get', {'swarm_key': swarm_key}),
            Step('swarm.resumable.list', {}),
            Step('swarm.agents.mark_delivered', {
                'swarm_key': swarm_key, 'agent_ids': ['b', 'b', 'missing'],
            }, command=True),
            Step('swarm.session.get', {'swarm_key': swarm_key}),
            Step('swarm.session.quarantine_ownerless', {
                'swarm_key': swarm_key, 'now_ms': 600,
            }, command=True),
            Step('swarm.resumable.list', {}),
            Step('swarm.session.terminate', {
                'swarm_key': swarm_key, 'now_ms': 700,
            }, command=True),
            Step('swarm.session.save', {
                'swarm_key': swarm_key, 'conv_id': 'conv-second',
                'task_id': 'task-second', 'status': 'running',
                'specs': [{'id': 'b'}, {'id': 'a'}],
                'config': {'model': 'test-2', 'user_id': _OWNER_ID},
                'now_ms': 800,
            }, command=True),
            Step('swarm.session.quarantine_ownerless', {
                'swarm_key': swarm_key, 'now_ms': 900,
            }, command=True),
            Step('swarm.resumable.list', {}),
            Step('swarm.session.delete', {'swarm_key': swarm_key}, command=True),
            Step('swarm.session.delete', {'swarm_key': swarm_key}, command=True),
            Step('swarm.session.get', {'swarm_key': swarm_key}),
        ],
    }


def _integration_scripts() -> dict[str, list[Step]]:
    project_root = f'/integration-{uuid.uuid4().hex[:12]}'
    owner = {'user_id': _OWNER_ID, 'project_root': project_root}
    ignore_ids = frozenset({'id'})
    def row(step: int) -> _Ref:
        return _Ref(step, ('id',))
    return {
        'integration workspace queue metadata events and worker CAS': [
            Step('integration.workspace.register', {
                **owner, 'task_id': 'alpha', 'title': 'Alpha',
                'workspace_path': '/workspace/alpha', 'managed': False,
                'base_sha': 'a' * 40,
                'origin_json': '{"source":"agent"}', 'now': 1.0,
            }, command=True),
            Step('integration.workspace.get', {
                **owner, 'task_id': 'alpha',
            }, ignore_fields=ignore_ids),
            Step('integration.workspace.set_meta', {
                **owner, 'task_id': 'alpha',
                'patch_json': '{"attempt":2}', 'now': 1.1,
            }, command=True),
            Step('integration.workspace.save_checkpoint', {
                **owner, 'task_id': 'alpha',
                'checkpoint_sha': 'b' * 40, 'now': 2.0,
            }, command=True),
            Step('integration.workspace.submit', {
                **owner, 'task_id': 'alpha', 'now': 3.0,
            }, command=True),
            Step('integration.workspace.peek_ready', {'now': 4.0},
                 ignore_fields=ignore_ids),
            Step('integration.workspace.claim_next', {'now': 4.0},
                 command=True, ignore_fields=ignore_ids),
            Step('integration.workspace.get_integrating', {'row_id': row(6)},
                 ignore_fields=ignore_ids),
            Step('integration.workspace.quarantine', {
                'row_id': row(6), 'error': 'conflict', 'now': 5.0,
            }, command=True),
            Step('integration.workspace.retry', {
                **owner, 'task_id': 'alpha', 'now': 6.0,
            }, command=True),
            Step('integration.workspace.claim_next', {'now': 7.0},
                 command=True, ignore_fields=ignore_ids),
            Step('integration.workspace.requeue', {
                'row_id': row(10), 'error': 'returned', 'now': 8.0,
            }, command=True),
            Step('integration.workspace.claim_next', {'now': 9.0},
                 command=True, ignore_fields=ignore_ids),
            Step('integration.workspace.mark_failed', {
                'row_id': row(12), 'error': 'worker failed', 'now': 10.0,
            }, command=True),
            Step('integration.workspace.retry', {
                **owner, 'task_id': 'alpha', 'now': 11.0,
            }, command=True),
            Step('integration.workspace.claim_next', {'now': 12.0},
                 command=True, ignore_fields=ignore_ids),
            Step('integration.workspace.mark_merged', {
                'row_id': row(15), 'candidate_sha': 'c' * 40,
                'now': 13.0,
            }, command=True),
            Step('integration.workspace.mark_merged', {
                'row_id': row(15), 'candidate_sha': 'c' * 40,
                'now': 14.0,
            }, command=True),
            Step('integration.workspace.register', {
                **owner, 'task_id': 'beta', 'title': 'Beta',
                'workspace_path': '/workspace/beta', 'managed': True,
                'base_sha': 'd' * 40, 'now': 15.0,
            }, command=True),
            Step('integration.workspace.save_checkpoint', {
                **owner, 'task_id': 'beta',
                'checkpoint_sha': 'e' * 40, 'now': 16.0,
            }, command=True),
            Step('integration.workspace.submit', {
                **owner, 'task_id': 'beta', 'now': 17.0,
            }, command=True),
            Step('integration.workspace.discard', {
                **owner, 'task_id': 'beta', 'now': 18.0,
            }, command=True),
            Step('integration.event.record', {
                **owner, 'task_id': '', 'kind': 'worker_idle',
                'message': 'queue drained', 'detail': '', 'now': 19.0,
            }, command=True),
            Step('integration.status', owner, ignore_fields=ignore_ids),
        ],
    }


def _knowledge_scripts() -> dict[str, list[Step]]:
    """Owner-scoped knowledge corpus: documents, catalog, search, enrichment.

    The legacy fixture persists across scripts within a process while the
    tofu-db fixture is function-scoped, so every settings-dependent step is
    sequenced to converge from either starting state: script one leaves the
    auto-enabled settings row behind, which script two's first library create
    would have produced anyway.  Wall-clock fields written by patch/claim/
    update (``updated_at``) are ignored recursively; document timestamps are
    caller-prescribed and compared exactly.
    """
    suffix = uuid.uuid4().hex[:8]
    user = _OWNER_ID

    def asset(asset_id: str, ordinal: int, kind: str, *, created: float,
              status: str = 'pending') -> dict:
        return {
            'id': asset_id, 'ordinal': ordinal, 'kind': kind,
            'stored_name': f'{asset_id}.png', 'mime_type': 'image/png',
            'sha256': hashlib.sha256(asset_id.encode()).hexdigest(),
            'size_bytes': 5, 'width': 1, 'height': 1, 'page': 0,
            'pages_json': '[]', 'bbox_json': '[]',
            'caption': '', 'ocr_text': '', 'description': '',
            'enrichment_status': status,
            'enrichment_model': '', 'enrichment_error': '',
            'metadata_json': '{}', 'created_at': created, 'updated_at': created,
        }

    def document(doc_id: str, digest: str, *, name: str = 'Spec.PDF',
                 kind: str = '.PDF', scope: str = 'library',
                 chunks: list | None = None, assets: list | None = None,
                 created: float = 100.0, updated: float = 200.0) -> dict:
        if assets is None:
            assets = [asset(f'{doc_id}-asset-1', 0, 'image', created=50.0)]
        if chunks is None:
            chunks = [
                {'ordinal': 0, 'section': 'Intro', 'location': 'page 1',
                 'content': 'Hello World',
                 'search_text': 'Hello WORLD sigma \u0391\u03b2',
                 'assets': [{'id': assets[0]['id'], 'relation': 'evidence'}]
                 if assets else []},
                {'ordinal': 1, 'section': 'Body', 'location': 'page 2',
                 'content': 'Second passage', 'search_text': 'second passage',
                 'assets': []},
            ]
        return {
            'id': doc_id, 'sha256': digest, 'name': name,
            'stored_name': f'{doc_id}.bin', 'kind': kind, 'size_bytes': 12,
            'method': 'text', 'warnings_json': '[]', 'text_chars': 26,
            'chunk_count': len(chunks), 'pages': 2, 'scope': scope,
            'media_metadata_json': '{}',
            'created_at': created, 'updated_at': updated,
            'chunks': chunks, 'assets': assets,
        }

    def create(doc: dict) -> Step:
        return Step('knowledge.document.create', {
            'user_id': user, 'document_id': doc['id'], 'document': doc,
        }, command=True)

    ignore_clock = frozenset({'updated_at'})
    doc_a = f'kn-a-{suffix}'
    digest_a = hashlib.sha256(doc_a.encode()).hexdigest()
    digest_a2 = hashlib.sha256(f'{doc_a}-v2'.encode()).hexdigest()
    digest_missing = hashlib.sha256(f'kn-missing-{suffix}'.encode()).hexdigest()

    doc_b = f'kn-b-{suffix}'
    digest_b = hashlib.sha256(doc_b.encode()).hexdigest()
    assets_b = [
        asset(f'{doc_b}-img', 0, 'image', created=999.0),
        asset(f'{doc_b}-fig', 1, 'figure', created=50.0),
        asset(f'{doc_b}-tbl', 2, 'table', created=10.0),
    ]
    chunks_b = [
        {'ordinal': 0, 'section': 'Charts', 'location': 'page 1',
         'content': 'chart chunk', 'search_text': 'chart chunk',
         'assets': [{'id': f'{doc_b}-img', 'relation': 'evidence'},
                    {'id': f'{doc_b}-fig', 'relation': 'evidence'}]},
        {'ordinal': 1, 'section': 'Tables', 'location': 'page 2',
         'content': 'table chunk', 'search_text': 'table chunk',
         'assets': [{'id': f'{doc_b}-tbl', 'relation': 'evidence'}]},
    ]

    doc_c = f'kn-c-{suffix}'
    digest_c = hashlib.sha256(doc_c.encode()).hexdigest()

    return {
        'knowledge document lifecycle digest dedupe and scoped reads': [
            Step('knowledge.settings.get', {'user_id': user}),
            Step('knowledge.availability', {'user_id': user}),
            create(document(doc_a, digest_a)),
            # Same digest under a new id dedupes to the existing metadata.
            create(document(f'{doc_a}-dupe', digest_a)),
            # A first library create auto-enables the owner settings row.
            Step('knowledge.settings.get', {'user_id': user}),
            Step('knowledge.availability', {'user_id': user}),
            Step('knowledge.document.list', {'user_id': user}),
            Step('knowledge.document.get',
                 {'user_id': user, 'document_id': doc_a}),
            Step('knowledge.document.metadata',
                 {'user_id': user, 'document_id': doc_a}),
            Step('knowledge.document.assets', {
                'user_id': user, 'document_id': doc_a, 'offset': 0, 'limit': 1,
            }),
            Step('knowledge.document.content', {
                'user_id': user, 'document_id': doc_a, 'offset': 1, 'limit': 1,
            }),
            Step('knowledge.document.find_digest',
                 {'user_id': user, 'sha256': digest_a}),
            Step('knowledge.document.find_digest',
                 {'user_id': user, 'sha256': digest_missing}),
            Step('knowledge.catalog', {'user_id': user}),
            Step('knowledge.catalog',
                 {'user_id': user, 'query': 'spec', 'sort': 'name_asc'}),
            Step('knowledge.catalog', {'user_id': user, 'category': 'pdf'}),
            Step('knowledge.catalog', {'user_id': user, 'category': 'image'}),
            Step('knowledge.catalog',
                 {'user_id': user, 'sort': 'size_desc', 'page_size': 1,
                  'page': 2}),
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': ['Hello', 'WORLD', 'hello']}),
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': ['hello'], 'document_id': doc_a}),
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': ['\u03b1\u03b2']}),
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': ['missing']}),
            Step('knowledge.document.patch', {
                'user_id': user, 'document_id': doc_a,
                'updates': {'scope': 'attachment'},
            }, command=True, ignore_fields=ignore_clock),
            # Attachment scope drops out of the library catalog and search.
            Step('knowledge.catalog', {'user_id': user}),
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': ['hello']}),
            Step('knowledge.availability', {'user_id': user}),
            Step('knowledge.document.patch', {
                'user_id': user, 'document_id': doc_a,
                'updates': {'scope': 'library',
                            'media_metadata_json': '{"pages": 2}'},
            }, command=True, ignore_fields=ignore_clock),
            Step('knowledge.document.replace', {
                'user_id': user, 'document_id': doc_a,
                'document': document(doc_a, digest_a2, updated=300.0),
            }, command=True),
            Step('knowledge.document.get',
                 {'user_id': user, 'document_id': doc_a}),
            Step('knowledge.document.find_digest',
                 {'user_id': user, 'sha256': digest_a}),
            Step('knowledge.document.find_digest',
                 {'user_id': user, 'sha256': digest_a2}),
            Step('knowledge.document.delete',
                 {'user_id': user, 'document_id': doc_a}, command=True),
            Step('knowledge.document.get',
                 {'user_id': user, 'document_id': doc_a}),
            Step('knowledge.document.delete',
                 {'user_id': user, 'document_id': doc_a}, command=True),
        ],
        'knowledge enrichment consent claim order update and owner clear': [
            create(document(doc_b, digest_b, chunks=chunks_b, assets=assets_b)),
            Step('knowledge.enrichment.activity', {'user_id': user}),
            Step('knowledge.enrichment.owners', {'user_id': user}),
            Step('knowledge.settings.patch', {
                'user_id': user, 'visual_enrichment': True,
            }, command=True),
            Step('knowledge.enrichment.owners', {'user_id': user}),
            # Kind rank claims the image before the earlier-created figure.
            Step('knowledge.asset.claim', {'user_id': user},
                 command=True, ignore_fields=ignore_clock),
            Step('knowledge.asset.claim', {'user_id': user},
                 command=True, ignore_fields=ignore_clock),
            Step('knowledge.asset.get',
                 {'user_id': user, 'asset_id': f'{doc_b}-img'},
                 ignore_fields=ignore_clock),
            Step('knowledge.asset.update', {
                'user_id': user, 'asset_id': f'{doc_b}-img',
                'updates': {'enrichment_status': 'ready', 'caption': 'chart'},
                'chunk_content': 'enriched body',
                'chunk_search_text': 'enriched body tokens',
            }, command=True, ignore_fields=ignore_clock),
            # The rewritten search_text re-projects the term index.
            # Readbacks embed assets whose updated_at the clock-driven
            # claim/update steps assigned, so the field is waived here too.
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': ['enriched']},
                 ignore_fields=ignore_clock),
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': ['chart']},
                 ignore_fields=ignore_clock),
            Step('knowledge.document.content',
                 {'user_id': user, 'document_id': doc_b},
                 ignore_fields=ignore_clock),
            # Only the never-claimed table asset is still pending.
            Step('knowledge.assets.mark_no_vision', {'user_id': user},
                 command=True),
            Step('knowledge.enrichment.activity', {'user_id': user}),
            Step('knowledge.catalog', {'user_id': user}),
            Step('knowledge.settings.patch', {
                'user_id': user, 'visual_enrichment': False,
            }, command=True),
            Step('knowledge.enrichment.owners', {'user_id': user}),
            Step('knowledge.owner.clear', {'user_id': user}, command=True),
            Step('knowledge.document.list', {'user_id': user}),
            Step('knowledge.settings.get', {'user_id': user}),
            Step('knowledge.availability', {'user_id': user}),
        ],
        'knowledge validation bounds and immutable fields fail identically': [
            # Payload/document identity mismatch.
            Step('knowledge.document.create', {
                'user_id': user, 'document_id': f'{doc_c}-other',
                'document': document(doc_c, digest_c),
            }, command=True),
            Step('knowledge.document.create', {
                'user_id': user, 'document_id': doc_c,
                'document': document(doc_c, 'not-a-digest'),
            }, command=True),
            Step('knowledge.document.create', {
                'user_id': user, 'document_id': doc_c,
                'document': {'id': doc_c},
            }, command=True),
            Step('knowledge.document.patch', {
                'user_id': user, 'document_id': doc_c, 'updates': {},
            }, command=True),
            Step('knowledge.document.patch', {
                'user_id': user, 'document_id': doc_c,
                'updates': {'name': 'immutable'},
            }, command=True),
            Step('knowledge.document.patch', {
                'user_id': user, 'document_id': doc_c,
                'updates': {'scope': 'everywhere'},
            }, command=True),
            Step('knowledge.catalog', {'user_id': user, 'category': 'bogus'}),
            Step('knowledge.catalog', {'user_id': user, 'sort': 'bogus'}),
            Step('knowledge.catalog', {'user_id': user, 'query': 'x' * 201}),
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': 'hello'}),
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': []}),
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': ['x' * 129]}),
            Step('knowledge.search.candidates',
                 {'user_id': user, 'tokens': ['ok'], 'limit': 0}),
            Step('knowledge.settings.patch',
                 {'user_id': user, 'enabled': 'yes'}, command=True),
            Step('knowledge.asset.update', {
                'user_id': user, 'asset_id': f'missing-{suffix}',
                'updates': {'sha256': 'immutable'},
            }, command=True),
            Step('knowledge.asset.update', {
                'user_id': user, 'asset_id': f'missing-{suffix}',
                'updates': {'enrichment_status': 'exploded'},
            }, command=True),
            Step('knowledge.asset.update', {
                'user_id': user, 'asset_id': f'missing-{suffix}',
                'updates': {'caption': 'ghost'},
            }, command=True),
            Step('knowledge.document.find_digest',
                 {'user_id': user, 'sha256': 'XYZ'}),
            Step('knowledge.document.replace', {
                'user_id': user, 'document_id': f'missing-{suffix}',
                'document': document(f'missing-{suffix}', digest_missing),
            }, command=True),
        ],
    }


def _all_scripts() -> dict[str, list[Step]]:
    scripts = _record_scripts()
    scripts.update(_project_recent_scripts())
    scripts.update(_project_relink_scripts())
    scripts.update(_event_scripts())
    scripts.update(_conversation_scripts())
    scripts.update(_artifact_scripts())
    scripts.update(_turn_scripts())
    scripts.update(_provider_scripts())
    scripts.update(_model_routing_scripts())
    scripts.update(_system_scripts())
    scripts.update(_rate_limit_scripts())
    scripts.update(_daily_cost_scripts())
    scripts.update(_log_aggregate_scripts())
    scripts.update(_raw_archive_scripts())
    scripts.update(_plugin_scripts())
    scripts.update(_optimizer_scripts())
    scripts.update(_research_scripts())
    scripts.update(_knowledge_scripts())
    scripts.update(_paper_artifact_scripts())
    scripts.update(_paper_podcast_scripts())
    scripts.update(_paper_library_scripts())
    scripts.update(_paper_note_scripts())
    scripts.update(_scheduler_scripts())
    scripts.update(_timer_scripts())
    scripts.update(_queue_scripts())
    scripts.update(_orchestration_scripts())
    scripts.update(_integration_scripts())
    scripts.update(_swarm_scripts())
    scripts.update(_project_brain_scripts())
    scripts.update(_worker_job_scripts())
    scripts.update(_search_scripts())
    scripts.update(_compaction_archive_scripts())
    scripts.update(_desktop_scripts())
    scripts.update(_tool_result_artifact_scripts())
    scripts.update(_browser_observation_scripts())
    scripts.update(_tenant_user_scripts())
    scripts.update(_credential_scripts())
    scripts.update(_billing_scripts())
    # task_results is landing piecemeal: activate each drafted script once
    # the executor serves every op it touches.  The port ratchet proves
    # activated ops stay implemented and flags the remaining staged ops
    # the moment they land.  Scripts whose ops still answer
    # operation_not_implemented stay staged in _task_results_scripts().
    staged = _task_results_scripts()
    for name in _ACTIVE_TASK_RESULTS_SCRIPTS:
        scripts[name] = staged[name]
    return scripts


# Drafted task_results scripts whose operations the tofu-db executor
# already serves (ratchet-verified).  Extend as the port lands.
_ACTIVE_TASK_RESULTS_SCRIPTS = (
    'task_results checkpoint CAS replay and conflict',
    'task_results guarded checkpoint owns parent fences and cache facts',
    'task_results abort signals only owned live tasks',
    'task_results replay_get projects compact snapshots',
    'task_results summary_list filters by conv and status',
    'task_results cost experiment scan uses compact owner index',
    'task_results recover running settles compact state',
)


# Documented, intentional semantic differences.  Each entry names the
# rationale; the suite asserts the divergence still exists so this manifest
# ratchets to zero as the engines converge.
KNOWN_DIVERGENCES: dict[str, str] = {
    'project brain format-native cutover state': (
        'a new SQLite/PostgreSQL sidecar starts before its one-time legacy '
        'Project Brain table cutover, while a Tofu-DB authority cannot contain '
        'those SQL tables and is born cutover-complete. The first legacy '
        'status/cutover therefore reports incomplete/newly-completed while '
        'Tofu-DB truthfully reports complete/already-complete; subsequent '
        'status and cutover calls converge exactly.'),
    'system.reclaim backend physical metrics': (
        'the operation preserves bounded-success and legacy input semantics, '
        'but its result necessarily describes each backend physical format: '
        'SQLite reports freelist pages and auto_vacuum while Tofu-DB reports '
        'content-addressed blocks, immutable segments, exact reclaimed bytes '
        'and whether another bounded round has candidates.'),
}


# ── The differential gate ────────────────────────────────────────────────
def _execute_step(
    driver: Any, step: Step, outcomes: list[tuple[Step, Outcome]],
) -> Outcome:
    payload = _resolve_payload_refs(step.payload, outcomes)
    command_id = _cid() if step.command or step.maintenance else None
    if not step.eventual:
        return driver.execute(
            step.operation, payload, command_id, maintenance=step.maintenance)
    # Asynchronously-derived projections (conversation.search) converge
    # through each authority's own background worker.  Poll until the
    # observed value anchors at eventual_min_hits and then stays identical
    # across the stability window; the settled value is what compares.
    deadline = time.monotonic() + _EVENTUAL_TIMEOUT_S
    outcome = driver.execute(
        step.operation, payload, command_id, maintenance=step.maintenance)
    anchored_value: Any = None
    stable_since = 0.0
    while time.monotonic() < deadline:
        candidate = driver.execute(
            step.operation, payload, command_id, maintenance=step.maintenance)
        if candidate.ok:
            outcome = candidate
            anchored = step.eventual_min_hits <= 0 or (
                isinstance(candidate.value, list)
                and len(candidate.value) >= step.eventual_min_hits
            )
            if anchored:
                now = time.monotonic()
                if stable_since and candidate.value == anchored_value:
                    if now - stable_since >= _EVENTUAL_STABLE_WINDOW_S:
                        return candidate
                else:
                    anchored_value = candidate.value
                    stable_since = now
        time.sleep(_EVENTUAL_POLL_INTERVAL_S)
    return outcome


def _run_script(
    script: list[Step], driver: Any,
) -> list[tuple[Step, Outcome]]:
    outcomes = []
    for step in script:
        outcomes.append((step, _execute_step(driver, step, outcomes)))
    return outcomes


def _format_divergence(
    name: str, index: int, step: Step, legacy: Outcome, tofu: Outcome,
) -> str:
    return (
        f'script {name!r} step {index} ({step.operation} '
        f'{json.dumps(step.payload, ensure_ascii=False, sort_keys=True, default=str)}):\n'
        f'  legacy: {"ok " if legacy.ok else "ERR "}'
        f'{json.dumps(legacy.value, ensure_ascii=False, default=str) if legacy.ok else legacy.error_code}\n'
        f'  tofu-db: {"ok " if tofu.ok else "ERR "}'
        f'{json.dumps(tofu.value, ensure_ascii=False, default=str) if tofu.ok else tofu.error_code}'
        f'{" (" + tofu.error_message + ")" if not tofu.ok and tofu.error_message else ""}'
    )


@pytest.mark.parametrize(
    'script_name',
    sorted(_all_scripts()),
    ids=lambda name: name,
)
def test_operation_matches_legacy_authority(
    script_name: str, legacy_driver: LegacyDriver, tofudb_daemon: TofuDbDriver,
) -> None:
    script = _all_scripts()[script_name]
    legacy_outcomes = _run_script(script, legacy_driver)
    tofu_outcomes = _run_script(script, tofudb_daemon)

    unexpected = []
    missing_documented = []
    legacy_tokens: dict[str, str] = {}
    tofu_tokens: dict[str, str] = {}
    for index, ((step, legacy), (_, tofu)) in enumerate(
        zip(legacy_outcomes, tofu_outcomes)
    ):
        if not step.compare:
            assert legacy.ok and tofu.ok, (
                f'baseline reset failed in script {script_name!r} step '
                f'{index}: legacy={legacy!r} tofu-db={tofu!r}')
            continue
        same = legacy.ok == tofu.ok and (
            _tokenize_server_ids(
                _drop_comparison_fields(legacy.value, step.ignore_fields),
                legacy_tokens,
            )
            == _tokenize_server_ids(
                _drop_comparison_fields(tofu.value, step.ignore_fields),
                tofu_tokens,
            )
            if legacy.ok
            else legacy.error_code == tofu.error_code
        )
        if same and step.expect_divergence:
            missing_documented.append((index, step))
        elif not same and not step.expect_divergence:
            unexpected.append(
                _format_divergence(script_name, index, step, legacy, tofu))
        elif not same and step.expect_divergence not in KNOWN_DIVERGENCES:
            unexpected.append(
                _format_divergence(script_name, index, step, legacy, tofu)
                + f'\n  (undocumented divergence {step.expect_divergence!r})')

    assert not unexpected, (
        'semantic divergence between Tofu-DB and the legacy authority:\n'
        + '\n'.join(unexpected)
    )
    assert not missing_documented, (
        'documented divergences no longer occur; remove them from '
        'KNOWN_DIVERGENCES and the script markers: '
        + repr([(i, s.operation) for i, s in missing_documented])
    )


# ── Port ratchet ─────────────────────────────────────────────────────────
def _catalog_operations() -> set[str]:
    """Legacy authority operation names (the Python OperationSpec registry)."""
    pattern = re.compile(
        r"""["']([a-z_]+(?:\.[a-z_0-9]+)+)["']:\s*(?:ops\.)?OperationSpec""")
    names: set[str] = set()
    domains = _PROJECT_ROOT / 'lib/storage_sidecar/operation_domains'
    for path in domains.glob('*.py'):
        names.update(pattern.findall(path.read_text()))
    return names


def _compiled_operations() -> set[str]:
    """Operation names compiled into tofu-db's storage.v2 registry."""
    generated = (
        _PROJECT_ROOT
        / 'packages/tofu-db/src/generated_storage_operations.rs'
    ).read_text()
    return set(re.findall(r'name: "([a-z_][a-z_.0-9]*)"', generated))


# Catalog ops tofu-db already executes but no differential script covers
# yet (seeded from a behavioral probe: every entry answers validation
# errors, not operation_not_implemented).  Each entry is coverage debt
# with a domain note; the ratchet below refuses stale entries and forces
# entries out as scripts land, so this mapping ratchets to zero.
_PORT_BACKLOG: dict[str, str] = {
    # Ratcheted to zero: every operation the tofu-db executor serves is
    # covered by a differential script.  The ratchet below keeps the
    # denominator honest — any newly executable op flips red here first.
}


def _probe_implementation(driver: TofuDbDriver, operation: str) -> bool:
    """True when tofu-db executes the op (any answer except the stub)."""
    for command in (False, True):
        outcome = driver.execute(
            operation, {}, _cid() if command else None)
        if (not outcome.ok
                and outcome.error_code == 'operation_not_implemented'):
            return False
    return True


def test_tofudb_port_ratchet(tofudb_daemon: TofuDbDriver) -> None:
    """Keep the coverage denominator honest while the Rust port grows.

    * The legacy registry and the compiled storage.v2 registry must name
      exactly the same operations (catalog parity).
    * Every covered op must exist in the catalog and stay out of the
      backlog (coverage forces backlog entries out — ratchet to zero).
    * Every uncovered, un-backlogged op must still answer
      ``operation_not_implemented``; port progress flips this red with a
      coverage-work message.
    * Staged domains (``_task_results_scripts``) activate piecemeal:
      unactivated staged ops must stay unimplemented (a landing flips
      red with the activation work item), while activated ops
      (``_ACTIVE_TASK_RESULTS_SCRIPTS``) must stay implemented.
    * Backlog entries must stay executable; a stale entry flips red.
    """
    catalog = _catalog_operations()
    compiled = _compiled_operations()
    assert catalog == compiled, (
        'operation catalog parity drift:\n'
        f'  legacy-only: {sorted(catalog - compiled)}\n'
        f'  compiled-only: {sorted(compiled - catalog)}'
    )
    covered = {
        step.operation
        for script in _all_scripts().values()
        for step in script
    }
    staged = {
        step.operation
        for script in _task_results_scripts().values()
        for step in script
    }
    assert covered <= compiled, (
        f'scripts reference unknown operations: {sorted(covered - compiled)}')
    assert staged <= compiled, (
        'staged scripts reference unknown operations: '
        f'{sorted(staged - compiled)}')
    active_staged = {
        step.operation
        for name in _ACTIVE_TASK_RESULTS_SCRIPTS
        for step in _task_results_scripts()[name]
    }
    assert active_staged <= covered, (
        'activated task_results scripts must be registered in '
        f'_all_scripts(): {sorted(active_staged - covered)}')
    unknown_backlog = set(_PORT_BACKLOG) - compiled
    assert not unknown_backlog, (
        'backlog entries no longer in the catalog: '
        f'{sorted(unknown_backlog)}')
    covered_backlog = covered & set(_PORT_BACKLOG)
    assert not covered_backlog, (
        'covered operations must leave _PORT_BACKLOG: '
        f'{sorted(covered_backlog)}')

    newly_executable = []
    stale_backlog = []
    staged_landed = []
    for operation in sorted(compiled - covered):
        implemented = _probe_implementation(tofudb_daemon, operation)
        if operation in _PORT_BACKLOG:
            if not implemented:
                stale_backlog.append(operation)
        elif operation in staged:
            if implemented:
                staged_landed.append(operation)
        elif implemented:
            newly_executable.append(operation)
    # An activated staged op must never regress to the stub: its scripts
    # would silently compare against operation_not_implemented.
    regressed_staged = [
        operation
        for operation in sorted(active_staged)
        if not _probe_implementation(tofudb_daemon, operation)
    ]
    assert not newly_executable, (
        'tofu-db now executes these operations; add differential scripts '
        f'(or a _PORT_BACKLOG entry): {newly_executable}')
    assert not staged_landed, (
        'tofu-db now executes more of the staged task_results domain; '
        'activate the drafted scripts that only touch landed ops in '
        f'_ACTIVE_TASK_RESULTS_SCRIPTS: {staged_landed}')
    assert not regressed_staged, (
        'activated task_results operations regressed to '
        f'operation_not_implemented: {regressed_staged}')
    assert not stale_backlog, (
        'backlog entries now answer operation_not_implemented; remove '
        f'them from _PORT_BACKLOG: {stale_backlog}')
