"""Ratchet — frontend modules must never call a bare free ``t(`` (2026-08-20).

THE INCIDENT. tofu-pet.js ships as a NATIVE ES module chunk: it shares no
lexical scope with the boot bundle, where ``t`` is an imported module
binding. An unqualified ``t('…')`` in such a chunk is a ReferenceError at
module evaluation. Vite surfaced it as ``vite:preloadError``, the boot guard
answered with ``location.reload()``, the one-shot retry key was cleared on
app-ready, and the page reload-looped forever. During the storm, in-flight
subresource loads (the brand SVG on the welcome screen, pet frames) were
cancelled mid-fetch — the user-visible symptom was "SVG icons can no longer
be loaded" (broken-image icon on the welcome logo).

THE FIX being pinned. Every module that wants i18n strings must either
import ``t`` as a module binding, or resolve it defensively
(``globalThis.t`` → ``runtimeScope.t`` → pass-through), so a decorative
feature can never crash the page. tofu-pet.js carries that guard; this test
holds the whole ``frontend/src`` tree to the same rule so the next migrated
module cannot reintroduce the hazard.

The neuter proves the detector is load-bearing.
"""

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"
PET = SRC / "runtime" / "scene" / "tofu-pet.js"
RETAINED_SECTIONS = SRC / "runtime" / "sections"
RETAINED_MANIFEST = RETAINED_SECTIONS / "manifest.json"

#: A free `t(…)` call — literal and computed keys both need a lexical binding.
#: This is NOT a method call (`.t(`), longer identifier (`format(`), or
#: Vue-style `$t` property (excluded by the `$` lookbehind).
FREE_T_CALL_RE = re.compile(r"(?<![\w$.])t\s*\(")

#: Ways a module legitimately obtains `t`:
#:  - ES module import binding:  import { t } from ... / import t from ...
#:  - the defensive runtime guard: globalThis.t / runtimeScope.t / window.t
#:  - a local definition: function t(…) / var|let|const t = …
#:  - a function PARAMETER with a default, e.g. recCorrectionHtml's
#:    `t = translate` in features/paper/recommend.ts — the parameter is the
#:    binding, and its default is the defensive resolver.
SAFE_BINDING_RE = re.compile(
    r"\bimport\b[^;]*\bt\b[^;]*\bfrom\b"
    r"|globalThis\.t"
    r"|runtimeScope\.t"
    r"|window\.t"
    r"|\bfunction\s+t\s*\("
    r"|\b(?:var|let|const)\s+t\s*="
    r"|[(,]\s*t\s*=",
    re.S,
)


def _offenders() -> list[str]:
    bad = []
    for path in sorted(SRC.rglob("*")):
        if path.suffix not in (".js", ".ts") or not path.is_file():
            continue
        # These files are lexical sections, not separately executable chunks.
        # The generator composes all of them between _prelude/_epilogue into
        # app-runtime.js, whose prelude imports `t` once for the whole module.
        if RETAINED_SECTIONS in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if FREE_T_CALL_RE.search(text) and not SAFE_BINDING_RE.search(text):
            bad.append(str(path.relative_to(ROOT)))
    return bad


def test_no_frontend_module_calls_a_bare_free_t():
    """A free `t(` in a lazily-loaded chunk = ReferenceError at eval time."""
    bad = _offenders()
    assert not bad, (
        "these modules call a bare `t('…')` without importing it or resolving "
        "it defensively (globalThis.t / runtimeScope.t / pass-through). In a "
        "native ES module chunk that is a ReferenceError at evaluation — the "
        "2026-08-20 infinite-reload incident that broke the welcome-screen "
        "icons. Offenders: " + ", ".join(bad)
    )


def test_retained_sections_are_one_manifest_owned_translation_scope():
    """Every skipped section must belong to one declared lexical bundle."""
    payload = json.loads(RETAINED_MANIFEST.read_text(encoding="utf-8"))
    rows = payload.get("sections")
    lazy_bundles = payload.get("lazyBundles")
    assert payload.get("version") == 2 and isinstance(rows, list)
    assert isinstance(lazy_bundles, list)
    all_rows = list(rows)
    for bundle in lazy_bundles:
        all_rows.extend(bundle["sections"])
    declared = {Path(row["path"]).as_posix() for row in all_rows}
    actual = {
        path.relative_to(RETAINED_SECTIONS).as_posix()
        for path in RETAINED_SECTIONS.rglob("*.js")
    }
    assert actual == declared | {"_prelude.js", "_epilogue.js"}

    composed = "\n".join(
        [
            (RETAINED_SECTIONS / "_prelude.js").read_text(encoding="utf-8"),
            *[
                (RETAINED_SECTIONS / row["path"]).read_text(encoding="utf-8")
                for row in rows
            ],
            (RETAINED_SECTIONS / "_epilogue.js").read_text(encoding="utf-8"),
        ]
    )
    assert FREE_T_CALL_RE.search(composed), "fixture has no translated call"
    assert SAFE_BINDING_RE.search(composed), (
        "the composed retained runtime lost its module-level t binding"
    )
    for bundle in lazy_bundles:
        generated = (ROOT / bundle["output"]).read_text(encoding="utf-8")
        # Translation-free bundles (for example Image Generation) need no t
        # import. Requiring every lazy owner to manufacture a call merely to
        # satisfy this guard would add the exact empty dependency the guard is
        # supposed to prevent.
        if FREE_T_CALL_RE.search(generated):
            assert SAFE_BINDING_RE.search(generated), (
                f"lazy runtime {bundle['name']} lost its module-level t binding"
            )


def test_pet_chunk_keeps_its_defensive_t_guard():
    """Anchor the incident file itself: the guard must stay in tofu-pet.js."""
    text = PET.read_text(encoding="utf-8")
    assert "globalThis.t" in text and "runtimeScope.t" in text, (
        "tofu-pet.js lost its defensive t() binding — without it the pet "
        "chunk crashes at module eval and the boot guard reload-loops"
    )


def test_NC_stripping_the_guard_is_caught():
    """Neuter: remove the safe binding from tofu-pet.js → detector must fire."""
    text = PET.read_text(encoding="utf-8")
    assert SAFE_BINDING_RE.search(text), "fixture assumption broken"
    poisoned = SAFE_BINDING_RE.sub("", text)
    assert poisoned != text, "neuter did not apply — re-anchor it"
    assert FREE_T_CALL_RE.search(poisoned) and not SAFE_BINDING_RE.search(poisoned), (
        "NEUTER must leak: with the guard stripped, the free-t detector has "
        "to flag tofu-pet.js"
    )
