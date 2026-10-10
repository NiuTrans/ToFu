"""Offline subprocess specifications for definitions preparation storage."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from evaluations.catalog import BenchmarkDefinition
from evaluations.paths import runtime_environment
from evaluations.swebench import images

pytestmark = pytest.mark.unit


@pytest.fixture
def registry_fixture(host_tmp_path, monkeypatch):
    """A real imported package allocates temps at import and during download."""
    packages = host_tmp_path / "packages"
    for relative in ("harbor", "harbor/registry", "harbor/registry/client"):
        directory = packages / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("")
    (packages / "harbor/registry/client/package.py").write_text('''
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path.cwd()
def record(phase):
    with tempfile.TemporaryDirectory() as directory:
        cache = Path.home() / '.cache' / 'harbor'
        cache.mkdir(parents=True, exist_ok=True)
        (cache / 'fixture').write_text('offline')
        with (ROOT / 'observations.jsonl').open('a') as stream:
            stream.write(json.dumps({'phase': phase, 'tmp': directory,
                'home': str(Path.home()), 'pid': os.getpid()}) + '\\n')
record('import')

class TaskId:
    ref = 'sha256:' + 'a' * 64
    def get_name(self):
        return 'fixture-task'

class PackageDatasetClient:
    def __init__(self):
        self._task_client = self
    async def get_dataset_metadata(self, dataset):
        return SimpleNamespace(version='fixture-revision', task_ids=[TaskId()])
    async def download_tasks(self, ids, output_dir):
        record('download')
        if os.environ.get('FIXTURE_DOWNLOAD_FAIL'):
            raise RuntimeError('fixture registry unavailable')
        task = output_dir / ids[0].get_name() / ('a' * 64)
        task.mkdir(parents=True, exist_ok=True)
        (task / 'task.toml').write_text('[task]\\nname="fixture-task"\\n'
            '[environment]\\ndocker_image="fixture:offline"\\n')
''')
    monkeypatch.setenv("PYTHONPATH", str(packages))
    lock = host_tmp_path / "lock.json"
    lock.write_text(json.dumps({"fixture-task": "sha256:" + "a" * 64}))
    definition = BenchmarkDefinition(
        key="fixture", dataset="fixture@revision", task_count=1,
        dataset_source_revision="fixture-revision", default_attempts=1,
        official_min_attempts=1, source_url="", source_commit="",
        task_digest_lock=lock,
    )
    return definition, host_tmp_path / "definitions"


def test_real_child_isolates_import_and_download_and_cache_hit(
    registry_fixture, monkeypatch,
):
    definition, root = registry_fixture
    monkeypatch.setattr(tempfile, "tempdir", "/tmp")
    monkeypatch.setenv("HOME", "/tmp/ambient-home-must-not-be-used")
    monkeypatch.setenv("TMPDIR", "/tmp")
    before = dict(os.environ)
    calls = []
    original = images.run_streaming

    def recording(command, **kwargs):
        calls.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(images, "run_streaming", recording)
    tasks = images.prepare_definitions(definition, root, workers=2, attempts=1)
    assert [task.name for task in tasks] == ["fixture-task"]
    assert len(calls) == 1 and calls[0][0] == sys.executable
    assert len(calls[0]) == 3  # argv carries only script and private request path
    observations = [json.loads(line) for line in
                    (root / "observations.jsonl").read_text().splitlines()]
    expected = runtime_environment(root)
    assert [row["phase"] for row in observations] == ["import", "download"]
    for row in observations:
        assert Path(row["tmp"]).parent == Path(expected["TMPDIR"])
        assert row["home"] == expected["HOME"]
        assert row["pid"] != os.getpid()
        assert not Path(row["tmp"]).exists()
    assert dict(os.environ) == before
    assert tempfile.tempdir == "/tmp"
    assert not list(root.glob(".definitions-request.*"))
    assert images.prepare_definitions(definition, root) == tasks
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["download", "lock", "revision"])
def test_child_failure_does_not_publish_manifest(registry_fixture, monkeypatch, failure):
    from dataclasses import replace

    definition, root = registry_fixture
    if failure == "download":
        monkeypatch.setenv("FIXTURE_DOWNLOAD_FAIL", "1")
        detail = "fixture registry unavailable"
    elif failure == "lock":
        definition.task_digest_lock.write_text(json.dumps({"other": "sha256:" + "b" * 64}))
        detail = "task digests differ from the pinned lock"
    else:
        definition = replace(definition, dataset_source_revision="wrong")
        detail = "registry dataset digest changed"
    with pytest.raises(RuntimeError, match="definition download child exited.*definitions-download.log"):
        images.prepare_definitions(definition, root, workers=1, attempts=1)
    assert detail in (root / "definitions-download.log").read_text()
    assert not (root / "definitions.json").exists()
    assert not list(root.glob(".definitions-request.*"))


def test_interrupted_phase_cleans_request_without_publishing(registry_fixture, monkeypatch):
    definition, root = registry_fixture

    def interrupted(command, **kwargs):
        assert Path(command[-1]).is_file()
        raise KeyboardInterrupt

    monkeypatch.setattr(images, "run_streaming", interrupted)
    with pytest.raises(KeyboardInterrupt):
        images.prepare_definitions(definition, root)
    assert not list(root.glob(".definitions-request.*"))
    assert not (root / "definitions.json").exists()


@pytest.mark.parametrize("options", [{"workers": 0}, {"workers": 17},
                                          {"attempts": 0}, {"attempts": 11}])
def test_invalid_budget_never_starts_child(host_tmp_path, monkeypatch, options):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid budget must not launch a process")
    monkeypatch.setattr(images, "run_streaming", forbidden)
    with pytest.raises(ValueError):
        images.prepare_definitions(None, host_tmp_path / "definitions", **options)
