from __future__ import annotations

import re
from pathlib import Path


def dockerfile_base_image(environment_dir: Path) -> str:
    """Return the single literal base image used by a task Dockerfile.

    Rootless QEMU builds the complete Dockerfile inside a disposable guest, but
    the host-side image store must first resolve its immutable base payload.
    Variable-expanded and multi-stage Dockerfiles are rejected instead of being
    guessed at; a future dataset can publish a prebuilt image for those shapes.
    """

    dockerfile = environment_dir / "Dockerfile"
    if dockerfile.is_symlink() or not dockerfile.is_file():
        raise ValueError("rootless-qemu requires docker_image or environment/Dockerfile")
    logical: list[str] = []
    pending = ""
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending += stripped
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip() + " "
            continue
        logical.append(pending)
        pending = ""
    if pending:
        logical.append(pending)
    bases: list[str] = []
    for line in logical:
        match = re.fullmatch(
            r"FROM(?:\s+--platform=\S+)?\s+(\S+)(?:\s+AS\s+\S+)?",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            bases.append(match.group(1))
    if len(bases) != 1 or "$" in bases[0]:
        raise ValueError(
            "rootless-qemu supports exactly one literal Dockerfile FROM image"
        )
    return bases[0]

