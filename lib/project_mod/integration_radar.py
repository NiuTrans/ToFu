"""Integration radar: which other unlanded work changes the files a writer changes.

Responsibility
    Cross-conversation change awareness derived from ground truth, never from
    self-reports: the repository-relative paths each conversation's successful
    writes touched in each Git checkout, checked against the shared checkout's
    current branch ("trunk") when they matter. It is notify-only: it takes no
    lease, never waits and never refuses a write (owner decision 2026-10-28,
    docs/runbooks/GIT_WORKSPACE_GUARD.md).

Entry points
    ``note_writes(task, project_path, paths)``: after a successful write,
        record the writer's paths and return advice about other unlanded work
        on the same files and about trunk landings since the writer's branch
        point.
    ``take_notices(task, project_path)``: at a safe round boundary, the
        notices other writers and landings left for this conversation.
    ``notify_landing(top, paths, ...)``: after ``scripts/git_land.py``
        published a merge, leave a notice for every writer whose unlanded
        work changes a landed path, and retire the landed checkout's records.

Storage
    ``<git-common-dir>/tofu-radar/`` is shared by every checkout of the
    repository and every process: ``writers/<writer>--<checkout>.json`` is
    written only by its conversation; ``notices/<writer>.jsonl`` is appended by
    other writers and landers and consumed by the conversation. The data is advice, never
    authority: losing a file loses advice, not work. A record leaves with its
    checkout, and a path stops counting once that checkout's copy no longer
    differs from its merge base with trunk (landed or reverted).

Dependencies: standard library and the ``git`` executable.
Contract: docs/modules/conversations_project_brain.md, "Integration radar".
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable

from lib.log import get_logger
from lib.project_mod.generated_integration import output_family

logger = get_logger(__name__)

RADAR_DIR = 'tofu-radar'
_SCHEMA_VERSION = 1
_SAFE_NAME = re.compile(r'[^A-Za-z0-9._-]')
# One process's writers of one record never interleave their read-modify-write;
# a record has a single conversation, so no cross-process writer exists.
_record_lock = threading.Lock()
# Latest (HEAD, trunk) -> merge base per checkout root. Exact: a moved HEAD or
# trunk misses the entry and replaces it; one entry per live checkout.
_base_cache: dict[str, tuple[str, str, str]] = {}
_base_cache_lock = threading.Lock()


@dataclasses.dataclass(frozen=True)
class Checkout:
    """One Git working tree: the shared checkout or a linked worktree."""
    root: Path
    git_dir: Path
    common_dir: Path


@dataclasses.dataclass(frozen=True)
class WriterRecord:
    file: Path
    writer: str
    title: str
    checkout: Path
    paths: dict[str, int]


# ─── Repository facts read from files (no Git process) ─────────────────────

def find_checkout(directory) -> Checkout | None:
    """The checkout whose working tree contains DIRECTORY, from its ``.git`` entry."""
    try:
        start = Path(os.path.abspath(os.path.expanduser(str(directory))))
    except (TypeError, ValueError):
        return None
    for candidate in (start, *start.parents):
        marker = candidate / '.git'
        try:
            if marker.is_dir():
                git_dir = marker
            elif marker.is_file():
                text = marker.read_text(encoding='utf-8', errors='replace').strip()
                if not text.startswith('gitdir:'):
                    return None
                git_dir = Path(text[len('gitdir:'):].strip())
                if not git_dir.is_absolute():
                    git_dir = candidate / git_dir
            else:
                continue
            git_dir = Path(os.path.realpath(git_dir))
            common_dir = git_dir
            commondir = git_dir / 'commondir'
            if commondir.is_file():
                value = commondir.read_text(encoding='utf-8').strip()
                common_dir = Path(os.path.realpath(
                    value if os.path.isabs(value) else git_dir / value))
        except OSError:
            return None
        return Checkout(Path(os.path.realpath(candidate)), git_dir, common_dir)
    return None


def locate(path) -> tuple[Checkout, str] | None:
    """The checkout containing PATH and PATH relative to its working-tree root."""
    try:
        target = Path(os.path.abspath(os.path.expanduser(str(path))))
    except (TypeError, ValueError):
        return None
    checkout = find_checkout(target.parent)
    if checkout is None:
        return None
    try:
        relative = Path(os.path.realpath(target.parent)).relative_to(checkout.root)
    except ValueError:
        return None
    relative = (relative / target.name).as_posix()
    if relative.split('/', 1)[0] in ('.git', '') or relative == '.':
        return None
    return checkout, relative


def trunk_ref(common_dir: Path) -> str:
    """The shared checkout's current branch: the main working tree's HEAD."""
    try:
        head = (Path(common_dir) / 'HEAD').read_text(encoding='utf-8').strip()
    except OSError:
        return ''
    return head[len('ref:'):].strip() if head.startswith('ref:') else ''


def _resolve_ref(common_dir: Path, ref: str) -> str:
    try:
        return (Path(common_dir) / ref).read_text(encoding='utf-8').strip()
    except OSError:
        pass
    try:
        packed = (Path(common_dir) / 'packed-refs').read_text(encoding='utf-8')
    except OSError:
        return ''
    for line in packed.splitlines():
        if line.endswith(' ' + ref) and not line.startswith(('#', '^')):
            return line.split(' ', 1)[0]
    return ''


def _head_of(checkout: Checkout) -> str:
    try:
        head = (checkout.git_dir / 'HEAD').read_text(encoding='utf-8').strip()
    except OSError:
        return ''
    if head.startswith('ref:'):
        return _resolve_ref(checkout.common_dir, head[len('ref:'):].strip())
    return head


def _branch_of(checkout: Checkout) -> str:
    try:
        head = (checkout.git_dir / 'HEAD').read_text(encoding='utf-8').strip()
    except OSError:
        return ''
    return head[len('ref: refs/heads/'):] if head.startswith('ref: refs/heads/') else ''


# ─── Git queries (only for a candidate overlap or a first touch) ───────────

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_')}
    # Recorded paths are file names, never pathspec patterns.
    return subprocess.run(['git', '-C', str(root), '--no-optional-locks',
                           '--literal-pathspecs', *args],
                          capture_output=True, text=True, env=env)


def _merge_base(checkout: Checkout, trunk: str) -> tuple[str, str]:
    """(merge base of the checkout's HEAD with trunk, trunk commit) or ('', '')."""
    head = _head_of(checkout)
    trunk_sha = _resolve_ref(checkout.common_dir, trunk)
    if not head or not trunk_sha:
        return '', ''
    key = str(checkout.root)
    with _base_cache_lock:
        cached = _base_cache.get(key)
    if cached and cached[0] == head and cached[1] == trunk_sha:
        return cached[2], trunk_sha
    if head == trunk_sha:
        base = head
    else:
        result = _git(checkout.root, 'merge-base', head, trunk_sha)
        base = result.stdout.strip() if result.returncode == 0 else ''
    if base:
        with _base_cache_lock:
            _base_cache[key] = (head, trunk_sha, base)
    return base, trunk_sha


def live_paths(checkout: Checkout, trunk: str, paths: Iterable[str]) -> set[str] | None:
    """PATHS this checkout still changes relative to its merge base with trunk.

    Counts committed and uncommitted edits, deletions and untracked new files;
    None when Git cannot answer, so callers report nothing rather than guess.
    """
    wanted = sorted(set(paths))
    if not wanted:
        return set()
    base, _trunk_sha = _merge_base(checkout, trunk)
    if not base:
        return None
    changed = _git(checkout.root, 'diff', '--name-only', '--no-renames', '-z', base,
                   '--', *wanted)
    untracked = _git(checkout.root, 'ls-files', '--others', '--exclude-standard', '-z',
                     '--', *wanted)
    if changed.returncode or untracked.returncode:
        return None
    found = {item for item in (changed.stdout + untracked.stdout).split('\0') if item}
    return found & set(wanted)


# ─── Records ───────────────────────────────────────────────────────────────

def _writer_of(task: dict) -> tuple[str, str]:
    writer = str(task.get('convId') or '').strip()
    if not writer and task.get('id'):
        writer = f"task-{task['id']}"
    title = str((task.get('config') or {}).get('convTitle') or '').strip()
    return writer, title


def _safe(text: str) -> str:
    cleaned = _SAFE_NAME.sub('_', text)
    if cleaned != text or len(cleaned) > 96:
        cleaned = cleaned[:64] + '-' + hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
    return cleaned


def _checkout_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:16]


def _radar(common_dir: Path, *parts: str) -> Path:
    return Path(common_dir, RADAR_DIR, *parts)


def _read_json(path: Path):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _record_file(checkout: Checkout, writer: str) -> Path:
    return _radar(checkout.common_dir, 'writers',
                  f'{_safe(writer)}--{_checkout_key(checkout.root)}.json')


def _records(common_dir: Path) -> list[WriterRecord]:
    directory = _radar(common_dir, 'writers')
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    records = []
    for name in names:
        if not name.endswith('.json'):
            continue
        data = _read_json(directory / name)
        if not isinstance(data, dict) or data.get('version') != _SCHEMA_VERSION:
            continue
        paths = data.get('paths')
        if not isinstance(paths, dict) or not data.get('checkout'):
            continue
        records.append(WriterRecord(
            directory / name, str(data.get('conversation') or ''),
            str(data.get('title') or ''), Path(str(data['checkout'])),
            {str(key): int(value) for key, value in paths.items()
             if isinstance(value, (int, float))}))
    return records


def _checkout_exists(root: Path) -> bool:
    return (root / '.git').exists()


def _retire(record: WriterRecord) -> None:
    try:
        record.file.unlink()
    except FileNotFoundError:
        pass
    with _base_cache_lock:
        _base_cache.pop(str(record.checkout), None)


def _known_paths(path: Path, checkout: Checkout) -> tuple[dict[str, int], str]:
    data = _read_json(path)
    if (isinstance(data, dict) and data.get('version') == _SCHEMA_VERSION
            and data.get('checkout') == str(checkout.root)
            and isinstance(data.get('paths'), dict)):
        return ({str(key): int(value) for key, value in data['paths'].items()
                 if isinstance(value, (int, float))}, str(data.get('title') or ''))
    return {}, ''


def _update_record(checkout: Checkout, writer: str, title: str,
                   paths: list[str], now_ms: int, *, prune: bool, trunk: str) -> None:
    path = _record_file(checkout, writer)
    landed: set[str] = set()
    if prune:
        # A path from an earlier task counts only while it is still unlanded.
        # Git runs outside the record lock; only the merge below is serialized.
        earlier = [key for key in _known_paths(path, checkout)[0] if key not in paths]
        live = live_paths(checkout, trunk, earlier)
        if live is not None:
            landed = set(earlier) - live
    with _record_lock:
        known, recorded_title = _known_paths(path, checkout)
        title = title or recorded_title
        known = {key: value for key, value in known.items() if key not in landed}
        for relative in paths:
            known[relative] = now_ms
        _write_json(path, {
            'version': _SCHEMA_VERSION, 'conversation': writer, 'title': title,
            'checkout': str(checkout.root), 'branch': _branch_of(checkout),
            'updatedAt': now_ms, 'paths': known,
        })


# ─── Presentation ──────────────────────────────────────────────────────────

def _age(now_ms: int, then_ms: int) -> str:
    minutes = max(0, (now_ms - then_ms) // 60000)
    if minutes < 1:
        return 'just now'
    if minutes < 120:
        return f'{minutes} min ago'
    return f'{minutes // 60} h ago'


def _who(record: WriterRecord) -> str:
    return f'"{record.title}" ({record.writer})' if record.title else f'conversation {record.writer}'


def _where_root(root: Path, main_root: Path) -> str:
    try:
        shown = root.relative_to(main_root).as_posix() or '.'
    except ValueError:
        shown = str(root)
    return 'the shared checkout' if shown == '.' else shown


def _where(record: WriterRecord, main_root: Path) -> str:
    return _where_root(record.checkout, main_root)


# ─── Entry points ──────────────────────────────────────────────────────────

def note_writes(task: dict, project_path: str, paths: Iterable[str]) -> list[tuple[str, str]]:
    """Record PATHS written by TASK and return ``(dedup key, advice)`` pairs.

    Relative paths resolve against PROJECT_PATH, the task's bound root. Advice
    names other conversations whose unlanded work changes the same files and
    trunk commits that changed them since this checkout's branch point.
    """
    writer, title = _writer_of(task)
    if not writer:
        return []
    base_dir = Path(os.path.abspath(os.path.expanduser(str(project_path or '.'))))
    by_checkout: dict[Path, tuple[Checkout, list[str]]] = {}
    for raw in paths:
        candidate = Path(os.path.expanduser(str(raw)))
        located = locate(candidate if candidate.is_absolute() else base_dir / candidate)
        if located is None:
            continue
        checkout, relative = located
        if output_family(relative):
            continue  # git_land rebuilds generated outputs from merged sources
        entry = by_checkout.setdefault(checkout.root, (checkout, []))
        if relative not in entry[1]:
            entry[1].append(relative)
    advice: list[tuple[str, str]] = []
    now_ms = int(time.time() * 1000)
    pruned = task.setdefault('_radarPrunedCheckouts', [])
    for checkout, written in by_checkout.values():
        trunk = trunk_ref(checkout.common_dir)
        if not trunk:
            continue
        first_this_task = str(checkout.root) not in pruned
        _update_record(checkout, writer, title, written, now_ms,
                       prune=first_this_task, trunk=trunk)
        if first_this_task:
            pruned.append(str(checkout.root))
            # Settlement saves the uncommitted work of every lane a task wrote in.
            task.setdefault('_radarCheckouts', []).append(str(checkout.root))
        roots = task.setdefault('_radarCommonDirs', [])
        if str(checkout.common_dir) not in roots:
            roots.append(str(checkout.common_dir))
        advice.extend(_overlap_advice(task, checkout, writer, title, written, trunk, now_ms))
        advice.extend(_landed_advice(task, checkout, written, trunk, now_ms))
    return advice


def _append_notice(common_dir: Path, writer: str, text: str, now_ms: int) -> None:
    path = _radar(common_dir, 'notices', f'{_safe(writer)}.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps({'at': now_ms, 'text': text}, ensure_ascii=False) + '\n')


def _overlap_advice(task: dict, checkout: Checkout, writer: str, title: str,
                    written: list[str], trunk: str, now_ms: int) -> list[tuple[str, str]]:
    main_root = find_checkout(checkout.common_dir.parent)
    main_root = main_root.root if main_root else checkout.common_dir.parent
    advice = []
    for record in _records(checkout.common_dir):
        if record.writer == writer and record.checkout == checkout.root:
            continue
        shared = sorted(set(written) & set(record.paths))
        if not shared:
            continue
        if not _checkout_exists(record.checkout):
            _retire(record)
            continue
        other = find_checkout(record.checkout)
        if other is None:
            continue
        live = live_paths(other, trunk, shared)
        if not live:
            continue
        live = sorted(live)
        newest = max(record.paths[path] for path in live)
        lines = [
            f'[Concurrent change] {_who(record)} also has unlanded changes to '
            f'{", ".join(live)} in {_where(record, main_root)}'
            + (f' (branch {_branch_of(other)})' if _branch_of(other) else '')
            + f', last edited {_age(now_ms, newest)}.',
            f'Their version: git -C {other.root} diff {trunk.removeprefix("refs/heads/")}'
            f' -- {" ".join(live)}',
        ]
        if record.checkout == checkout.root:
            lines.append('You both edit the same working tree: re-read each file '
                         'before editing it, and keep the other edit intact.')
        else:
            lines.append('Keep both intents: read their change before relying on '
                         'these files, and whoever lands second merges the other.')
        key = hashlib.sha256('\0'.join(
            ['overlap', record.writer, str(record.checkout), *live]).encode('utf-8')).hexdigest()[:24]
        logger.info('[IntegrationRadar] overlap writer=%s other=%s checkout=%s paths=%s',
                    writer, record.writer, record.checkout, ','.join(live))
        advice.append((key, '\n'.join(lines)))
        notified = task.setdefault('_radarNotified', [])
        if key not in notified:
            # The earlier writer learns that this work now builds on its files too.
            notified.append(key)
            mine = f'"{title}" ({writer})' if title else f'conversation {writer}'
            branch = _branch_of(checkout)
            _append_notice(checkout.common_dir, record.writer, '\n'.join([
                f'[Concurrent change] {mine} started changing {", ".join(live)}, which your '
                f'unlanded work also changes, in {_where_root(checkout.root, main_root)}'
                + (f' (branch {branch})' if branch else '') + '.',
                f'Their version: git -C {checkout.root} diff '
                f'{trunk.removeprefix("refs/heads/")} -- {" ".join(live)}',
                'Keep both intents: whoever lands second merges the other.',
            ]), now_ms)
    return advice


def _landed_advice(task: dict, checkout: Checkout, written: list[str], trunk: str,
                   now_ms: int) -> list[tuple[str, str]]:
    if checkout.git_dir == checkout.common_dir:
        return []  # the shared checkout is trunk; landings merge into it directly
    base, trunk_sha = _merge_base(checkout, trunk)
    if not base or base == trunk_sha:
        return []
    seen = task.setdefault('_radarLandedChecked', [])
    advice = []
    for relative in written:
        marker = f'{checkout.root}\0{relative}\0{trunk_sha}'
        if marker in seen:
            continue
        seen.append(marker)
        log = _git(checkout.root, 'log', '--no-merges', '--format=%h%x1f%ct%x1f%s',
                   f'{base}..{trunk_sha}', '--', relative)
        commits = [line.split('\x1f', 2) for line in log.stdout.splitlines()
                   if log.returncode == 0 and line.count('\x1f') == 2]
        if not commits:
            continue
        name = trunk.removeprefix('refs/heads/')
        shown = [f'- {sha} {_age(now_ms, int(stamp) * 1000)} {subject}'
                 for sha, stamp, subject in commits[:3]]
        if len(commits) > 3:
            shown.append(f'- and {len(commits) - 3} earlier commits')
        key = hashlib.sha256(marker.encode('utf-8')).hexdigest()[:24]
        advice.append((key, '\n'.join([
            f'[Concurrent change] {name} has landed changes to {relative} since your '
            f'branch point ({base[:10]}):', *shown,
            f'Merge {name} into your branch before you verify; git_land verifies the '
            'merged result.'])))
    return advice


def take_notices(task: dict, project_path: str) -> list[str]:
    """Consume the notices other writers and landings left for this conversation."""
    writer, _title = _writer_of(task)
    if not writer:
        return []
    roots = list(task.get('_radarCommonDirs') or ())
    if not roots and project_path:
        checkout = find_checkout(project_path)
        if checkout is not None:
            roots.append(str(checkout.common_dir))
            task['_radarCommonDirs'] = list(roots)
    notices = []
    for common_dir in roots:
        path = _radar(Path(common_dir), 'notices', f'{_safe(writer)}.jsonl')
        claimed = path.with_name(f'{path.name}.{os.getpid()}.{uuid.uuid4().hex}')
        try:
            os.rename(path, claimed)
        except OSError:
            continue
        try:
            with open(claimed, encoding='utf-8') as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict) and item.get('text'):
                        notices.append(str(item['text']))
        except OSError:
            pass
        finally:
            try:
                claimed.unlink()
            except OSError:
                pass
    return notices


def notify_landing(top, landed_paths: Iterable[str], *, commit: str, subject: str,
                   branch: str, landed_root=None) -> list[dict]:
    """Leave notices for writers whose unlanded work changes a landed path.

    TOP is the checkout that received the merge. The landed checkout's own
    records are retired: its work is on trunk now. Returns one entry per
    notified writer for the landing report.
    """
    checkout = find_checkout(top)
    if checkout is None:
        return []
    trunk = trunk_ref(checkout.common_dir)
    if not trunk:
        return []
    landed = {path for path in landed_paths if not output_family(path)}
    landed_root = Path(os.path.realpath(landed_root)) if landed_root else None
    now_ms = int(time.time() * 1000)
    name = trunk.removeprefix('refs/heads/')
    notified = []
    for record in _records(checkout.common_dir):
        if landed_root is not None and record.checkout == landed_root:
            _retire(record)
            continue
        if not _checkout_exists(record.checkout):
            _retire(record)
            continue
        shared = sorted(landed & set(record.paths))
        if not shared:
            continue
        other = find_checkout(record.checkout)
        live = live_paths(other, trunk, shared) if other else None
        if not live:
            continue
        live = sorted(live)
        if other.git_dir == other.common_dir:
            advice = ('Your uncommitted edits in the shared checkout were merged on top '
                      'of it; re-read these files before editing them again.')
        else:
            advice = (f'Merge {name} into your branch and rerun your tests before '
                      'landing; git_land verifies the merged result.')
        text = (f'[Concurrent change] {branch} landed on {name} (merge {commit[:10]}: '
                f'"{subject}"). It changes files your unlanded work also changes: '
                f'{", ".join(live)}.\n{advice}')
        _append_notice(checkout.common_dir, record.writer, text, now_ms)
        logger.info('[IntegrationRadar] landing notice branch=%s writer=%s paths=%s',
                    branch, record.writer, ','.join(live))
        notified.append({'writer': record.writer, 'title': record.title,
                         'checkout': str(record.checkout), 'paths': live})
    return notified
