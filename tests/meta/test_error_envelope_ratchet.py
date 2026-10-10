"""One-way ratchet on legacy string errors at the HTTP boundary.

``lib/api_response.api_error`` still accepts a verbatim ``str`` error for
legacy callers; every such call returns a shape the frontend cannot classify
(no envelope ``kind``/``hint``). The typed-envelope taxonomy is the only new
shape: new string-error call sites are rejected, and the pinned inventory
below may only shrink (fix one → decrement the constant in the same change).
"""

from __future__ import annotations

import ast
import os
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Inventoried 2026-09 by AST over routes/ (88 call sites, f-strings
# included). Fix one and decrement here — never raise it to make the
# ratchet green.
#
# 2026-09-18 audit: 88 was a miscount from the day it was pinned — the same
# AST census reads 89 on 33ae2e9b1 (the pinning commit's own tree) and 89
# today, with zero growth in between. Corrected to the true baseline.
PINNED_LEGACY_STRING_ERROR_CALLS = 89


def _legacy_string_error_calls() -> dict[str, list[int]]:
    out = subprocess.run(
        ["git", "ls-files", "routes/*.py"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    )
    hits: dict[str, list[int]] = {}
    for rel in sorted(out.stdout.split()):
        full = os.path.join(ROOT, rel)
        if not os.path.isfile(full):
            continue
        tree = ast.parse(open(full, encoding="utf-8").read(), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name != "api_error" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, (ast.Constant, ast.JoinedStr)):
                hits.setdefault(rel, []).append(node.lineno)
    return hits


def test_legacy_string_error_inventory_only_shrinks():
    hits = _legacy_string_error_calls()
    total = sum(len(lines) for lines in hits.values())
    assert total <= PINNED_LEGACY_STRING_ERROR_CALLS, (
        f"legacy api_error(str) call sites grew "
        f"{PINNED_LEGACY_STRING_ERROR_CALLS} -> {total}; return a typed "
        f"envelope instead (see lib/error_envelope/). Offenders: "
        + ", ".join(f"{rel}:{line}" for rel, lines in sorted(hits.items())
                    for line in lines[:5])
    )


def test_ratchet_constant_is_not_loose():
    hits = _legacy_string_error_calls()
    total = sum(len(lines) for lines in hits.values())
    assert total == PINNED_LEGACY_STRING_ERROR_CALLS, (
        f"api_error(str) sites SHRANK to {total} — tighten "
        "PINNED_LEGACY_STRING_ERROR_CALLS in the same change"
    )
