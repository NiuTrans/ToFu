#!/usr/bin/env python3
"""Current Python local-counter oracle using hash-verified Cargo vocabulary.

Unchanged counter/base declarations execute in an isolated stdlib namespace;
only the caller-owned cache capacity is supplied. Requires tiktoken==0.14.0.
No application startup, network vocabulary fetch, stores or model requests.
"""

import ast
import base64
from collections import OrderedDict
from dataclasses import dataclass
from abc import ABC, abstractmethod
import hashlib
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import threading
import types
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]


def canonical_counter():
    import tiktoken_ext.openai_public as public

    metadata = subprocess.run(
        ["cargo", "metadata", "--locked", "--offline", "--format-version", "1"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=60,
    )
    packages = json.loads(metadata.stdout)["packages"]
    package = next(p for p in packages if p["name"] == "tiktoken-rs")
    assets = Path(package["manifest_path"]).parent / "assets"

    def load(url, expected_hash=None):
        name = url.rsplit("/", 1)[-1]
        if name not in {"cl100k_base.tiktoken", "o200k_base.tiktoken"}:
            raise RuntimeError("unsupported oracle vocabulary")
        path = assets / name
        if path.stat().st_size > 8 * 1024 * 1024:
            raise RuntimeError("oracle vocabulary exceeds bound")
        raw = path.read_bytes()
        if expected_hash is None or hashlib.sha256(raw).hexdigest() != expected_hash:
            raise RuntimeError("Cargo vocabulary differs from canonical Python hash")
        return {
            base64.b64decode(token): int(rank)
            for token, rank in (line.split() for line in raw.splitlines())
        }

    public.load_tiktoken_bpe = load
    module = types.ModuleType("_tofu_token_oracle")
    sys.modules[module.__name__] = module
    scope = module.__dict__
    scope.update(
        ABC=ABC,
        abstractmethod=abstractmethod,
        dataclass=dataclass,
        hashlib=hashlib,
        re=re,
        threading=threading,
        OrderedDict=OrderedDict,
        Any=Any,
        Optional=Optional,
        logger=logging.getLogger(__name__),
        get_logger=logging.getLogger,
        _TEXT_COUNT_CACHE_CAPACITY=4096,
    )
    base = ast.parse((ROOT / "lib/token_counter/base.py").read_text())
    names = {
        "TokenCounter",
        "CountResult",
        "iter_message_texts",
        "count_images",
        "_IMAGE_TOKENS_DEFAULT",
        "_STRUCTURAL_OVERHEAD_TOKENS",
        "REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY",
        "REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_MAX",
    }
    nodes = [
        n
        for n in base.body
        if (isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names)
        or (
            isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in names
        )
    ]
    exec(
        compile(
            ast.Module(body=nodes, type_ignores=[]), "canonical_token_base", "exec"
        ),
        scope,
    )
    source = ast.parse((ROOT / "lib/token_counter/tiktoken_counter.py").read_text())
    nodes = [
        n
        for n in source.body
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AnnAssign))
        or (
            isinstance(n, ast.Assign)
            and n.targets[0].id != "_TEXT_COUNT_CACHE_CAPACITY"
        )
    ]
    exec(
        compile(
            ast.Module(body=nodes, type_ignores=[]), "canonical_local_counter", "exec"
        ),
        scope,
    )
    return scope["TiktokenCounter"]()


def main():
    path = ROOT / "packages/tofu-runtime/tests/fixtures/python_token_ingress.json"
    if path.stat().st_size > 2 * 1024 * 1024:
        raise SystemExit("token oracle exceeds fixture byte bound")
    counter = canonical_counter()
    rows = json.loads(path.read_text())
    for index, row in enumerate(rows):
        actual = counter.count(
            row["messages"],
            model=row["model"],
            system=row["template"].get("system"),
            tools=row["template"].get("tools"),
        )
        if actual != row["tokens"]:
            raise SystemExit(f"current Python token counter differs at case {index}")
    print(
        f"{len(rows)} current canonical local-token counts match without vocabulary network access"
    )


if __name__ == "__main__":
    main()
