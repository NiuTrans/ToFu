#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  vendor_mcp.sh — refresh the in-repo snapshots of internal MCP servers
# ═══════════════════════════════════════════════════════════════
#
#  Internal MCP servers (hope-mcp, …) are private and not on PyPI. Tofu ships
#  a source snapshot under tools/<name>/ so a fresh checkout can pip-install
#  them on first connect (see lib/mcp/client.py _try_autoinstall_launcher).
#
#  Those snapshots drift from the live dev checkout. Run this at release time
#  (or whenever you cut a deploy) to re-sync each snapshot from its sibling
#  source repo:
#
#      make vendor-mcp
#      # or directly:  scripts/vendor_mcp.sh [name ...]
#
#  With no args it vendors every server in the registry. Pass one or more
#  names to vendor a subset.
#
#  SINGLE SOURCE OF TRUTH: the list of servers + their source/dest dirs is
#  read directly from `VENDORED_LAUNCHERS` in lib/mcp/vendored.py (a tiny,
#  stdlib-only module — no heavy imports) — there is no duplicated manifest
#  here. For each server we sync FROM the first registered source dir OUTSIDE
#  tools/ (the sibling dev checkout) INTO the first source dir under tools/
#  (the vendored snapshot).
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# Repo root = parent of this script's dir.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Pick a Python that can import the repo (override with TOFU_PYTHON).
PY="${TOFU_PYTHON:-}"
if [[ -z "$PY" ]]; then
    if command -v python3 >/dev/null 2>&1; then PY=python3
    elif command -v python >/dev/null 2>&1; then PY=python
    else echo "✗ no python interpreter found (set TOFU_PYTHON)" >&2; exit 1; fi
fi

# Files/dirs never copied into a vendored snapshot.
EXCLUDES=(
    --exclude='.git'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='.pytest_cache'
    --exclude='.ruff_cache'
    --exclude='build'
    --exclude='dist'
    --exclude='*.egg-info'
    --exclude='*.egg-link'
    --exclude='.tofu'
    --exclude='.tofu_trash'
    --exclude='.chatui'
    --exclude='.oa-skills-ref'
    --exclude='.venv'
    --exclude='venv'
    --exclude='JOURNAL.md'
    # Local archive in the xuecheng development checkout; never a runtime
    # dependency and not part of the distributable source tree.
    --exclude='citadel (1).zip'
)

# Derive the manifest from VENDORED_LAUNCHERS. Emits TAB-separated rows:
#   <name>\t<sibling-src-rel>\t<vendored-dest-rel>
# A field is empty when that source kind isn't registered for the server.
read_registry() {
    # Load vendored.py BY FILE PATH (not `import lib.mcp.vendored`) so we
    # bypass lib/mcp/__init__.py, which eagerly imports the heavy `client`
    # module. This keeps the script's read of the registry truly cheap and
    # dependency-free regardless of what the package __init__ pulls in.
    REPO_ROOT="$REPO_ROOT" "$PY" - <<'PYEOF'
import importlib.util
import os
import sys

path = os.path.join(os.environ["REPO_ROOT"], "lib", "mcp", "vendored.py")
spec = importlib.util.spec_from_file_location("_tofu_vendored", path)
try:
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    VENDORED_LAUNCHERS = mod.VENDORED_LAUNCHERS
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"failed to load VENDORED_LAUNCHERS from {path}: {e}\n")
    sys.exit(3)
for name, spec in VENDORED_LAUNCHERS.items():
    srcs = spec.get("sources", [])
    sibling = next((s for s in srcs if not s.startswith("tools/")), "")
    dest = next((s for s in srcs if s.startswith("tools/")), "")
    print("\t".join([name, sibling, dest]))
PYEOF
}

vendor_one() {
    local name="$1" src_rel="$2" dest_rel="$3"

    if [[ -z "$src_rel" ]]; then
        echo "✗ $name: no sibling dev-checkout source registered (only a tools/ snapshot) — nothing to vendor FROM" >&2
        return 1
    fi
    if [[ -z "$dest_rel" ]]; then
        echo "✗ $name: no tools/ snapshot path registered — nothing to vendor INTO" >&2
        return 1
    fi

    local src dest
    src="$(cd "$REPO_ROOT" && cd "$src_rel" 2>/dev/null && pwd || true)"
    dest="$REPO_ROOT/$dest_rel"

    if [[ -z "$src" || ! -f "$src/pyproject.toml" ]]; then
        echo "✗ $name: source '$src_rel' not found or has no pyproject.toml (looked at: ${src:-$REPO_ROOT/$src_rel})" >&2
        return 1
    fi

    echo "→ vendoring $name from $src"
    mkdir -p "$dest"
    # Keep the snapshot a faithful mirror of the source. --delete-excluded
    # also removes stale caches/build metadata that an earlier local run may
    # have left below tools/<name>.
    rsync -a --delete --delete-excluded "${EXCLUDES[@]}" "$src/" "$dest/"
    local size
    size="$(du -sh "$dest" 2>/dev/null | cut -f1)"
    echo "  ✓ $name → $dest_rel ($size)"
}

main() {
    local want=("$@")
    local rows
    rows="$(read_registry)" || { echo "✗ could not read MCP registry from lib/mcp/client.py" >&2; exit 1; }
    if [[ -z "$rows" ]]; then
        echo "✗ _VENDORED_LAUNCHERS is empty — nothing to vendor" >&2
        exit 1
    fi

    local rc=0 done=0
    while IFS=$'\t' read -r name src_rel dest_rel; do
        [[ -z "$name" ]] && continue
        if [[ ${#want[@]} -gt 0 ]]; then
            local match=0
            for w in "${want[@]}"; do [[ "$w" == "$name" ]] && match=1; done
            [[ $match -eq 1 ]] || continue
        fi
        vendor_one "$name" "$src_rel" "$dest_rel" || rc=1
        done=$((done + 1))
    done <<< "$rows"

    if [[ $done -eq 0 ]]; then
        echo "No matching MCP server to vendor. Known:" >&2
        echo "$rows" | cut -f1 | sed 's/^/  - /' >&2
        return 1
    fi
    [[ $rc -eq 0 ]] && echo "Done. Review & commit the tools/ changes."
    return $rc
}

main "$@"
