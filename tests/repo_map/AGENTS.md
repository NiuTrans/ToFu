# Repository map semantic tests

This directory verifies `lib/repo_map/` through scan and public generation:
ordered multilingual tags, file-local grammar failures, deep valid syntax and
concurrent project isolation. Run `pytest -q tests/repo_map tests/meta/test_repo_map.py`
and the real tool adapter in `tests/tasks_pkg/test_repo_map_handler.py`.
