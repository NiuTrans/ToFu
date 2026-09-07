"""Architecture validation remains deterministic under concurrent file churn."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from scripts import check_architecture as architecture


pytestmark = pytest.mark.unit


def _isolate_main(monkeypatch, paths: list[Path]) -> None:
    monkeypatch.setattr(architecture, "ROOT", paths[0].parent)
    monkeypatch.setattr(architecture, "authored_sources", lambda: paths)
    monkeypatch.setattr(
        architecture, "frontend_legacy_debt_violations", lambda: ([], [])
    )
    monkeypatch.setattr(architecture, "RETIRED_PATHS", ())


def test_disappearing_authored_source_is_skipped(monkeypatch, tmp_path, capsys):
    live = tmp_path / "live.py"
    live.write_text("VALUE = 1\n", encoding="utf-8")
    missing = tmp_path / "removed.py"
    _isolate_main(monkeypatch, [missing, live])

    assert architecture.main() == 0
    assert "architecture-check: OK (1 authored files)" in capsys.readouterr().out


def test_existing_authored_source_is_still_enforced(monkeypatch, tmp_path, capsys):
    live = tmp_path / "live.py"
    live.write_text("RETIRED_TOKEN = True\n", encoding="utf-8")
    _isolate_main(monkeypatch, [live])
    monkeypatch.setattr(
        architecture,
        "FORBIDDEN_TEXT",
        ((re.compile(r"RETIRED_TOKEN"), "test violation"),),
    )

    assert architecture.main() == 1
    output = capsys.readouterr().out
    assert "architecture-check: FAILED" in output
    assert "test violation: 'RETIRED_TOKEN'" in output


def test_authored_source_read_error_is_reported(monkeypatch, tmp_path, capsys):
    unreadable = tmp_path / "unreadable.py"
    unreadable.write_text("VALUE = 1\n", encoding="utf-8")
    _isolate_main(monkeypatch, [unreadable])
    original_read_text = Path.read_text

    def denied_read_text(path: Path, *args, **kwargs) -> str:
        if path == unreadable:
            raise PermissionError("permission denied by test")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied_read_text)

    assert architecture.main() == 1
    output = capsys.readouterr().out
    assert "architecture-check: FAILED" in output
    assert "unreadable.py: cannot read authored source" in output
    assert "permission denied by test" in output


@pytest.mark.parametrize("metric", ["file_count", "byte_count", "regex_count"])
def test_disappearing_debt_source_does_not_inflate_metric(
    monkeypatch, tmp_path, metric
):
    missing = tmp_path / "removed.ts"
    monkeypatch.setattr(
        architecture, "_debt_source_paths", lambda _debt: [missing]
    )
    debt = {"metric": metric, "pattern": "legacy"}

    assert architecture.measure_frontend_legacy_debt(debt) == (0, [])
