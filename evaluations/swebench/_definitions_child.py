"""Private definitions I/O phase; launched only by images.prepare_definitions.

The parent applies runtime_environment before Python starts. Reuse the download
and validation authority in images, not a second runner or retry loop.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Direct script execution works from the private cache cwd, in the active venv.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluations.catalog import BenchmarkDefinition
from evaluations.swebench.images import _download_definitions_async


def main() -> None:
    request_path = Path(sys.argv[1])
    if request_path.stat().st_size > 256 * 1024:
        raise ValueError("definition download request exceeds 256 KiB")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    values = request["definition"]
    if values["task_digest_lock"] is not None:
        values["task_digest_lock"] = Path(values["task_digest_lock"])
    workers, attempts = request["workers"], request["attempts"]
    if not 1 <= workers <= 16 or not 1 <= attempts <= 10:
        raise ValueError("definition download worker/retry bounds are invalid")
    asyncio.run(_download_definitions_async(
        BenchmarkDefinition(**values), request_path.parent,
        workers=workers, attempts=attempts,
    ))


if __name__ == "__main__":
    main()
