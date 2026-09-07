"""Frozen SWE-bench Multilingual dataset loading and stratified splits."""

from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests


DATASET_ID = 'SWE-bench/SWE-bench_Multilingual'
DATASET_REVISION = '78aa2877b9f56271239c29d440d26e337414fc39'
DATASET_SHA256 = (
    '0a3ee2cac501424b566e107dfff6f90c91e200218630040d5ecb0776f2a515e6')
DATASET_URL = (
    'https://huggingface.co/datasets/SWE-bench/'
    f'SWE-bench_Multilingual/resolve/{DATASET_REVISION}/'
    'data/test-00000-of-00001.parquet'
)
DATASET_CACHE = Path(os.environ.get(
    'CONTEXT_BENCH_DATASET_CACHE',
    f'/tmp/tofu-context-efficiency-{DATASET_REVISION[:12]}.parquet'))
SPLIT_SIZES = {
    'calibration': 5,
    'ablation': 10,
    'pilot': 30,
    'confirmation': 100,
}


_LANGUAGE_BY_REPO = {
    # C
    'jqlang/jq': 'C',
    'micropython/micropython': 'C',
    'redis/redis': 'C',
    'valkey-io/valkey': 'C',
    # C++
    'fmtlib/fmt': 'C++',
    'nlohmann/json': 'C++',
    # Go
    'caddyserver/caddy': 'Go',
    'gin-gonic/gin': 'Go',
    'gohugoio/hugo': 'Go',
    'prometheus/prometheus': 'Go',
    'hashicorp/terraform': 'Go',
    # Java
    'apache/druid': 'Java',
    'apache/lucene': 'Java',
    'google/gson': 'Java',
    'javaparser/javaparser': 'Java',
    'projectlombok/lombok': 'Java',
    'reactivex/rxjava': 'Java',
    # JavaScript
    'axios/axios': 'JavaScript',
    'babel/babel': 'JavaScript',
    'facebook/docusaurus': 'TypeScript',
    'immutable-js/immutable-js': 'TypeScript',
    'mrdoob/three.js': 'JavaScript',
    'preactjs/preact': 'JavaScript',
    'vuejs/core': 'TypeScript',
    # PHP
    'briannesbitt/carbon': 'PHP',
    'laravel/framework': 'PHP',
    'php-cs-fixer/php-cs-fixer': 'PHP',
    'phpoffice/phpspreadsheet': 'PHP',
    # Ruby
    'faker-ruby/faker': 'Ruby',
    'fastlane/fastlane': 'Ruby',
    'fluent/fluentd': 'Ruby',
    'jekyll/jekyll': 'Ruby',
    'jordansissel/fpm': 'Ruby',
    'rubocop/rubocop': 'Ruby',
    # Rust
    'astral-sh/ruff': 'Rust',
    'burntsushi/ripgrep': 'Rust',
    'nushell/nushell': 'Rust',
    'sharkdp/bat': 'Rust',
    'tokio-rs/axum': 'Rust',
    'tokio-rs/tokio': 'Rust',
    'uutils/coreutils': 'Rust',
}


@dataclass(frozen=True)
class BenchmarkTask:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str
    patch: str
    test_patch: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    version: str
    image: str
    eval_script: str
    log_parser: str
    eval_type: str
    language: str
    difficulty: str = ''

    def manifest_metadata(self) -> dict:
        return {
            'taskId': self.instance_id,
            'repo': self.repo,
            'language': self.language,
            'difficulty': self.difficulty,
            'baseCommit': self.base_commit,
            'image': self.image,
        }


def _list_field(value) -> tuple[str, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    return tuple(str(item) for item in (value or []))


def _difficulty_scores(rows: list[dict]) -> dict[str, int]:
    def changed_lines(patch: str) -> int:
        return sum(
            1 for line in patch.splitlines()
            if line.startswith(('+', '-'))
            and not line.startswith(('+++', '---'))
        )

    return {
        row['instance_id']: changed_lines(row.get('patch') or '')
        for row in rows
    }


def _difficulty_buckets(rows: list[dict]) -> dict[str, str]:
    scores = _difficulty_scores(rows)
    ordered = sorted(scores.values())
    low = ordered[len(ordered) // 3]
    high = ordered[(2 * len(ordered)) // 3]
    out = {}
    for task_id, score in scores.items():
        out[task_id] = 'easy' if score <= low else 'hard' if score > high else 'medium'
    return out


def load_dataset_snapshot(*, timeout: int = 120) -> tuple[list[BenchmarkTask], str]:
    # Parquet is needed only by the opt-in dataset I/O path.  Keep the pure
    # split/manifest contract importable in the frozen default/test runtime,
    # which deliberately does not carry the heavyweight pyarrow wheel.
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            'context-efficiency dataset loading requires pyarrow') from exc

    raw = DATASET_CACHE.read_bytes() if DATASET_CACHE.is_file() else b''
    cache_valid = hashlib.sha256(raw).hexdigest() == DATASET_SHA256
    if not cache_valid:
        response = requests.get(DATASET_URL, timeout=timeout)
        response.raise_for_status()
        raw = response.content
    fingerprint = hashlib.sha256(raw).hexdigest()
    if fingerprint != DATASET_SHA256:
        raise ValueError(
            f'dataset snapshot SHA mismatch: {fingerprint} != {DATASET_SHA256}')
    if not cache_valid:
        DATASET_CACHE.write_bytes(raw)
    rows = pq.read_table(pa.BufferReader(raw)).to_pylist()
    difficulty = _difficulty_buckets(rows)
    tasks = []
    for row in rows:
        repo = str(row['repo'])
        if repo not in _LANGUAGE_BY_REPO:
            raise ValueError(f'programming language is not mapped for {repo}')
        tasks.append(BenchmarkTask(
            instance_id=str(row['instance_id']),
            repo=repo,
            base_commit=str(row['base_commit']),
            problem_statement=str(row.get('problem_statement') or ''),
            hints_text=str(row.get('hints_text') or ''),
            patch=str(row.get('patch') or ''),
            test_patch=str(row.get('test_patch') or ''),
            fail_to_pass=_list_field(row.get('FAIL_TO_PASS')),
            pass_to_pass=_list_field(row.get('PASS_TO_PASS')),
            version=str(row.get('version') or ''),
            image=str(row['image']),
            eval_script=str(row['eval_script']),
            log_parser=str(row['log_parser']),
            eval_type=str(row.get('eval_type') or ''),
            language=_LANGUAGE_BY_REPO[repo],
            difficulty=difficulty[str(row['instance_id'])],
        ))
    tasks.sort(key=lambda task: task.instance_id)
    return tasks, fingerprint


def _balanced_order(tasks: Iterable[BenchmarkTask], *, seed: int) -> list[BenchmarkTask]:
    """Return a deterministic order balancing language, difficulty and repo."""
    rng = random.Random(seed)
    remaining = sorted(tasks, key=lambda task: task.instance_id)
    random_rank = {task.instance_id: rng.random() for task in remaining}
    selected: list[BenchmarkTask] = []
    language_counts: dict[str, int] = {}
    difficulty_counts: dict[str, int] = {}
    repo_counts: dict[str, int] = {}
    while remaining:
        best_index = min(
            range(len(remaining)),
            key=lambda index: (
                language_counts.get(remaining[index].language, 0),
                difficulty_counts.get(remaining[index].difficulty, 0),
                repo_counts.get(remaining[index].repo, 0),
                random_rank[remaining[index].instance_id],
                remaining[index].instance_id,
            ),
        )
        task = remaining.pop(best_index)
        selected.append(task)
        language_counts[task.language] = (
            language_counts.get(task.language, 0) + 1)
        difficulty_counts[task.difficulty] = (
            difficulty_counts.get(task.difficulty, 0) + 1)
        repo_counts[task.repo] = repo_counts.get(task.repo, 0) + 1
    return selected


def build_frozen_manifest(tasks: list[BenchmarkTask], fingerprint: str, *,
                          seed: int = 20260810) -> dict:
    remaining = list(tasks)
    splits = {}
    for split_index, (name, size) in enumerate(SPLIT_SIZES.items()):
        order = _balanced_order(remaining, seed=seed + split_index)
        chosen = order[:size]
        if len(chosen) != size:
            raise ValueError(f'not enough tasks for {name}')
        splits[name] = [task.manifest_metadata() for task in chosen]
        chosen_ids = {task.instance_id for task in chosen}
        remaining = [task for task in remaining
                     if task.instance_id not in chosen_ids]
    return {
        'contractVersion': 'tofu-context-efficiency-splits/v1',
        'dataset': DATASET_ID,
        'datasetRevision': DATASET_REVISION,
        'datasetSha256': fingerprint,
        'seed': seed,
        'selection': (
            'each disjoint split independently greedily balances language, '
            'difficulty, then repo; deterministic seeded tie-break'),
        'difficultyDefinition': (
            'dataset-wide terciles of non-test gold-patch changed lines; '
            'used only for sampling, following the benchmark difficulty proxy'),
        'taskCount': len(tasks),
        'repoCount': len({task.repo for task in tasks}),
        'languageCount': len({task.language for task in tasks}),
        'splits': splits,
    }


def write_frozen_manifest(path: Path, *, seed: int = 20260810) -> dict:
    tasks, fingerprint = load_dataset_snapshot()
    manifest = build_frozen_manifest(tasks, fingerprint, seed=seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


def load_manifest_tasks(path: Path, stage: str) -> tuple[list[BenchmarkTask], dict]:
    manifest = json.loads(path.read_text())
    if stage not in SPLIT_SIZES:
        raise ValueError(f'unknown stage: {stage}')
    tasks, fingerprint = load_dataset_snapshot()
    if fingerprint != manifest.get('datasetSha256'):
        raise ValueError(
            'dataset fingerprint changed; do not silently reuse frozen splits')
    by_id = {task.instance_id: task for task in tasks}
    ids = [row['taskId'] for row in manifest['splits'][stage]]
    missing = [task_id for task_id in ids if task_id not in by_id]
    if missing:
        raise ValueError(f'frozen tasks missing from dataset: {missing}')
    return [by_id[task_id] for task_id in ids], manifest


__all__ = [
    'BenchmarkTask', 'DATASET_ID', 'DATASET_REVISION', 'DATASET_SHA256',
    'SPLIT_SIZES', 'build_frozen_manifest', 'load_dataset_snapshot',
    'load_manifest_tasks', 'write_frozen_manifest',
]
