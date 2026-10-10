"""Benchmark identity authority; no runner, agent, sandbox, or provider imports.

Add reviewed, pinned datasets to benchmark_catalog.json and a task digest lock.
Harbor is the current execution adapter, not part of the benchmark definition.
Unknown or incomplete definitions fail closed; future benchmark names are not
aliases for an existing suite. Importing the catalog performs bounded local I/O.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

CATALOG_PATH = Path(__file__).with_name("benchmark_catalog.json")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\Z")


@dataclass(frozen=True)
class BenchmarkDefinition:
    key: str
    dataset: str
    task_count: int
    dataset_source_revision: str
    default_attempts: int
    official_min_attempts: int
    source_url: str
    source_commit: str
    task_digest_lock: Path | None = None
    environment_defaults: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def dataset_name(self) -> str:
        return self.dataset.rsplit("@", 1)[0]

    def task_digests(self) -> dict[str, str]:
        if self.task_digest_lock is None:
            raise ValueError(f"{self.key} requires a pinned task digest lock")
        return load_task_digest_lock(
            self.task_digest_lock, label=self.key, expected_count=self.task_count
        )

    def environment_kwargs(self, backend: str) -> dict[str, str]:
        return dict(self.environment_defaults.get(backend, {}))

    def to_dict(self) -> dict:
        value = asdict(self)
        value["task_digest_lock"] = str(self.task_digest_lock)
        return value


def _read_json(path: Path, limit: int) -> object:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"catalog input must be a bounded regular file: {path}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"catalog input exceeds byte budget: {path}")
    return json.loads(raw)


def load_task_digest_lock(
    path: Path, *, label: str, expected_count: int
) -> dict[str, str]:
    value = _read_json(path, 4 * 1024 * 1024)
    if not (
        isinstance(value, dict)
        and len(value) == expected_count
        and list(value) == sorted(value)
        and all(
            isinstance(name, str) and bool(name) and len(name) <= 512
            and isinstance(ref, str) and _DIGEST.fullmatch(ref)
            for name, ref in value.items()
        )
    ):
        raise ValueError(f"invalid {label} task digest lock")
    return value


def load_catalog(path: Path = CATALOG_PATH) -> Mapping[str, BenchmarkDefinition]:
    payload = _read_json(path, 256 * 1024)
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "benchmarks"} \
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported benchmark catalog schema")
    rows = payload["benchmarks"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 256:
        raise ValueError("catalog must contain 1..256 benchmark definitions")
    root = path.resolve().parent
    definitions = {}
    fields = set(BenchmarkDefinition.__dataclass_fields__)
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("benchmark catalog has missing or unknown fields")
        key = row["key"]
        if not isinstance(key, str) or not _KEY.fullmatch(key) or key in definitions:
            raise ValueError("benchmark catalog keys must be unique stable identifiers")
        ref = row["dataset_source_revision"]
        dataset = row["dataset"]
        if not isinstance(ref, str) or not _DIGEST.fullmatch(ref) \
                or not isinstance(dataset, str) or len(dataset) > 512 \
                or not dataset.endswith("@" + ref) or dataset.count("@") != 1 \
                or not dataset.rsplit("@", 1)[0]:
            raise ValueError(f"{key} requires an immutable dataset digest")
        if any(type(row[field]) is not int or not 1 <= row[field] <= maximum
               for field, maximum in (("task_count", 100_000), ("default_attempts", 100),
                                      ("official_min_attempts", 100))):
            raise ValueError(f"{key} has invalid task/attempt budgets")
        if row["default_attempts"] < row["official_min_attempts"]:
            raise ValueError(f"{key} default attempts violate the publication minimum")
        commit = row["source_commit"]
        url = row["source_url"]
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"{key} requires a pinned source commit")
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError(f"{key} requires an HTTPS source URL")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"{key} requires a credential-free HTTPS source URL")
        relative = row["task_digest_lock"]
        if not isinstance(relative, str) or Path(relative).is_absolute() \
                or ".." in Path(relative).parts:
            raise ValueError(f"{key} task lock must be catalog-relative")
        lock = root / relative
        if lock.is_symlink() or not lock.resolve().is_relative_to(root):
            raise ValueError(f"{key} task lock escapes the catalog")
        defaults = row["environment_defaults"]
        if not isinstance(defaults, dict) or any(
            not isinstance(backend, str) or not _KEY.fullmatch(backend)
            or not isinstance(values, dict)
            or any(not isinstance(name, str) or not isinstance(value, str)
                   or len(name) > 96 or len(value) > 512 for name, value in values.items())
            for backend, values in defaults.items()
        ):
            raise ValueError(f"{key} has invalid backend defaults")
        definition = BenchmarkDefinition(**{**row, "task_digest_lock": lock})
        definition.task_digests()
        definitions[key] = definition
    return MappingProxyType(definitions)


BENCHMARKS = load_catalog()
