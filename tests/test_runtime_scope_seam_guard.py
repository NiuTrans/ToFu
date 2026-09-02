"""Guard: no bare cross-section references to runtimeScope-only runtime names.

2026-08-14 root cause (owner incident): the static/js → Vite/ESM migration
wrapped every migrated module in its own IIFE and moved file-scope exports to
``const runtimeScope = Object.create(null)`` (frontend/src/runtime/
app-runtime.js:368). The old classic scripts shared ``window`` scope, so code
in file B referenced file A's export as a bare identifier. After the
migration those identifiers resolve against the ESM module scope →
``globalThis`` → ``undefined``: typeof-guarded call sites became SILENT
no-ops (Project Brain could not be opened from the collab bar; context-bar
never refreshed; voice input never initialized; ConversationTurnStore abort threw at
click time …) and unguarded ones threw ``ReferenceError``.

This test pins the seam dead for the names fixed in that incident. A bare
reference outside an owner section is a regression: route it through
``runtimeScope.<name>`` (or the action registry for inline handlers).
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SECTIONS = REPO / "frontend" / "src" / "runtime" / "sections"
MANIFEST = SECTIONS / "manifest.json"

# Names whose cross-section bare references were rewritten to runtimeScope.X.
PINNED_NAMES = [
    "openProjectBrain", "projectBrainRefresh", "presenceRefresh",
    "updateContextBar", "ConversationTurnStore",
    "renderTurnCtxNote", "buildTurnCtxSnapshot", "reconcileTurnCtxCapsule",
    "initVoiceInput", "ChipInput", "openCompactionViewer",
    "resolveOrchestrationApiClient", "isChatModel", "modelGroupKey",
    "modelGroupLabel", "modelGroupBrandNames", "effectiveProbeStatus",
    "foldProbeHealth", "modelHealthLevelClass", "refreshMcpRailState",
    "detectLogNoise",
]

# Transitional globalThis bridges added while consumers migrated (search
# core/model_group.js "Keep until every consumer reads runtimeScope"). New
# bridges must be a conscious addition here — the end state is runtimeScope
# routing for every consumer, then the bridge AND this entry are deleted.
ALLOWED_GLOBAL_BRIDGES = ("modelGroupKey", "modelGroupLabel", "modelGroupBrandNames")

MARKER_RE = re.compile(r"^/\*\s*=====\s*migrated source:\s*(.+?)\s*=====\s*\*/\s*$")


def _runtime_source() -> str:
    """Compose the retained source graph without reading generated output."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    paths = [manifest["prelude"]]
    paths.extend(row["path"] for row in manifest["sections"])
    paths.append(manifest["epilogue"])
    return "\n".join(
        (SECTIONS / path).read_text(encoding="utf-8")
        for path in paths
    )


def _sections():
    lines = _runtime_source().split("\n")
    out = []  # (section_name, start, end)
    cur_name, cur_start = "(prelude)", 0
    for i, ln in enumerate(lines):
        m = MARKER_RE.match(ln)
        if m:
            out.append((cur_name, cur_start, i))
            cur_name, cur_start = m.group(1), i + 1
    out.append((cur_name, cur_start, len(lines)))
    return lines, out


def _mask_line(line: str) -> str:
    """Blank comment prose and string-literal contents. Single-line
    approximation; over-masking a line can only hide a violation (ratchet
    still catches the unmasked sites), never invent one."""
    if line.lstrip().startswith(("*", "/*", "//")):
        return ""
    out = re.sub(r"'(?:\\.|[^'\\])*'", lambda m: "'" + " " * max(0, len(m.group(0)) - 2) + "'", line)
    out = re.sub(r'"(?:\\.|[^"\\])*"', lambda m: '"' + " " * max(0, len(m.group(0)) - 2) + '"', out)
    return out


@pytest.mark.unit
def test_no_bare_cross_section_refs_to_runtimescope_names():
    lines, sections = _sections()

    # owner sections per name (EVERY section that assigns runtimeScope.<name>;
    # a name may be assigned by its defining section and re-bridged elsewhere)
    owners = {name: set() for name in PINNED_NAMES}
    for name in PINNED_NAMES:
        pat = re.compile(r"\bruntimeScope\.%s\s*=(?!=)" % re.escape(name))
        for sec_name, start, end in sections:
            if pat.search("\n".join(lines[start:end])):
                owners[name].add(sec_name)
    missing = [n for n in PINNED_NAMES if not owners[n]]
    assert not missing, f"pinned names with no runtimeScope owner: {missing}"

    violations = []
    for name in PINNED_NAMES:
        ref_re = re.compile(r"(?<![.\w$])\b%s\b" % re.escape(name))
        for sec_name, start, end in sections:
            if sec_name in owners[name]:
                continue
            for i in range(start, end):
                code = _mask_line(lines[i])
                if not code or not ref_re.search(code):
                    continue
                violations.append(
                    f"{name}: bare ref in [{sec_name}] L{i + 1}: "
                    f"{lines[i].strip()[:110]}"
                )
    assert not violations, (
        "runtimeScope seam regression — bare cross-section reference(s):\n  "
        + "\n  ".join(violations)
        + "\nRoute them through runtimeScope.<name> (see 2026-08-14 incident)."
    )


@pytest.mark.unit
def test_runtimescope_is_not_window_backed():
    """The migration contract: runtimeScope must NOT be window/globalThis,
    and global bridges for pinned names stay limited to the transitional
    allowlist above."""
    text = _runtime_source()
    assert "const runtimeScope = Object.create(null);" in text, (
        "runtimeScope is no longer a null-prototype object — if it became "
        "window-backed, the bare-ref guard above is moot and this incident's "
        "design decision was reversed; update both tests together."
    )
    for name in PINNED_NAMES:
        if name in ALLOWED_GLOBAL_BRIDGES:
            continue
        assert not re.search(
            r"\b(?:window|globalThis)\.%s\s*=" % re.escape(name), text
        ), f"{name} is installed on window/globalThis — seam design reversed"
