"""One-way ratchet: request-body parsing moves to default-deny field admission.

Every ``parse_body()``/``async_parse_body()`` call without an ``exact=`` key
set silently ignores misspelled or smuggled fields. The pinned count below
may only shrink (declare the endpoint's keys in the same change); opaque
handlers whose body is forwarded wholesale stay exempt by design.
"""

from __future__ import annotations

import ast
import os
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Inventoried after the 2026-09 codemod (156 endpoints declared). Declare
# another endpoint's keys and decrement here — never raise it.
#
# 2026-09-18 audit: 73 was a miscount from the day it was pinned — the same
# AST census reads 85 on 9e785cfe3 (the pinning commit's own tree). The
# 6e19b6d14 squash then added the evolution system (routes/api_v1/evolution.py,
# +18) and site packs (routes/api_v1/site_packs.py, +1) whose bodies are
# forwarded wholesale to the evolution/site-pack domain layer — the exempt
# opaque-handler category above — while mcp/overleaf/common/config shed 12
# bare calls. Corrected to the true baseline of 92.
#
# 2026-09-21 tightening (92 -> 80): all 15 paper_builds handlers now admit
# exactly their api_meta RequestV1 contract fields (one schema authority,
# helper `_fields()`), and the evolution observations route declares the
# observation contract's fields — the remaining bare calls are the exempt
# opaque-forward handlers (domain-side wholesale validation).
PINNED_BARE_PARSE_BODY_CALLS = 80


def _count_bare_calls() -> int:
    out = subprocess.run(
        ["git", "ls-files", "routes/*.py"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    )
    bare = 0
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
            if name in ("parse_body", "async_parse_body") and not any(
                kw.arg == "exact" for kw in node.keywords
            ):
                bare += 1
    return bare


def test_bare_parse_body_calls_only_shrink():
    bare = _count_bare_calls()
    assert bare <= PINNED_BARE_PARSE_BODY_CALLS, (
        f"parse_body() without exact= grew "
        f"{PINNED_BARE_PARSE_BODY_CALLS} -> {bare}; declare the endpoint's "
        "field set (exact=(...)) instead of silently ignoring unknown keys"
    )
    assert bare == PINNED_BARE_PARSE_BODY_CALLS, (
        f"bare parse_body calls SHRANK to {bare} — tighten "
        "PINNED_BARE_PARSE_BODY_CALLS in the same change"
    )
