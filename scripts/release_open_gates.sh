#!/usr/bin/env bash
# Execute release checks against one explicit exported candidate directory.
# Shared by release_open.sh and behavioral fault-injection tests.
set -euo pipefail
GATED_DIR="${1:?expected exported candidate directory}"

echo ">> exported-tree gates: pinned ruff + pytest collection"
echo "   (the public CI lints the whole tree and collects every shipped test;"
echo "    a failure here is a red GitHub lane discovered BEFORE push, not after)"
# set -e is suspended inside a subshell whose status is tested by `||`, so
# each step must fail the subshell itself or only the last step would count.
(
    cd "$GATED_DIR" || exit 1
    echo "  [1/2] uv run --frozen --extra dev ruff check ."
    uv run --frozen --extra dev ruff check . || exit 1
    # Collection must mirror the public unit lane: its Makefile ignores the
    # node/jsdom harness files (no npm ci on that lane), so a bare pytest
    # collect over tests/ would report false collection errors.
    echo "  [2/2] make test-collect (CI-visible unit surface, collection only)"
    uv run --frozen --extra test make test-collect || exit 1
) || {
    echo ">> exported-tree gates FAILED — not pushing." >&2
    echo "   Fix the source or the export keep-list, then re-run." >&2
    exit 1
}

echo ">> exported-tree gates green"
