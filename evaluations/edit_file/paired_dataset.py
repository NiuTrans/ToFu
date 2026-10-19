"""Deterministic tool-semantics fixtures, not model answers or accuracy samples.

The original three selection cases remain owned by dataset.py. Additional cases
are synthetic: no incident transcript, private paths, or provider calls. Native
Codex expectations are MANUALLY SOURCE-REVIEWED fixtures, never a second matcher.
Entry point: PAIRED_CASES; consumer: comparison.py.
"""
from __future__ import annotations

from .dataset import CASES_BY_ID

CODEX_REVISION = "5c5308fc9a9ee789049d646ef11e5400384b9c6f"
CODEX_REFERENCES = {
    "matching": {"path": "codex-rs/apply-patch/src/seek_sequence.rs", "lines": [12, 132],
                 "claim": "First match from cursor; exact, trim_end, trim, then Unicode punctuation normalization."},
    "chunks": {"path": "codex-rs/apply-patch/src/file_update.rs", "lines": [87, 243],
               "claim": "All chunks search original_lines with a forward cursor, then replacements apply in reverse."},
    "standalone": {"path": "codex-rs/apply-patch/src/lib.rs", "lines": [471, 650],
                   "claim": "File hunks write sequentially; an error stops later files without rolling back prior writes."},
    "verification": {"path": "codex-rs/apply-patch/src/invocation.rs", "lines": [213, 297],
                     "claim": "Read-only verification computes all changes first and rejects duplicate target headers; not a write transaction guarantee."},
    "boundaries": {"path": "codex-rs/apply-patch/src/parser.rs", "lines": [213, 274],
                   "claim": "Native parsing requires Begin Patch and End Patch boundaries."},
}


def _edit(path, anchor, content, operation="replace", **kwargs):
    return {"path": path, "operation": operation, "anchor": anchor,
            "content": content, **kwargs}


def _patch(path, body):
    return f"*** Begin Patch\n*** Update File: {path}\n{body}*** End Patch\n"


def _native(status, files, reason, references=("matching", "chunks"), *, verified=None):
    """Literal expectations only; this helper performs no matching or patching."""
    return {"evidence_kind": "static_source_review_not_executed",
            "standalone_expected": {"status": status, "files": files},
            "verified_expected": verified or {"status": status, "files": files},
            "reason": reason, "references": list(references)}


def _case(case_id, category, files, edits, patch, intended, native):
    return {"id": case_id, "category": category, "files": files, "edits": edits,
            "patch": patch, "intended_files": intended, "native_codex": native}


def _existing(case_id, edit, body):
    case = CASES_BY_ID[case_id]
    path = next(iter(case["files"]))
    return _case(case_id, "valid_existing_corpus", dict(case["files"]), [edit],
                 _patch(path, body), dict(case["expected"]),
                 _native("applied", dict(case["expected"]), "Exact source lines and unambiguous context."))


PAIRED_CASES = [
    _existing("insert_between_blocks", _edit("sample.txt", "A", "B", "insert_after"),
              "@@\n A\n+B\n C\n"),
    _existing("add_import", _edit("app.py", "import os", "import json", "insert_after"),
              "@@\n import os\n+import json\n \n VALUE = 1\n"),
    _existing("replace_value", _edit("config.py", "TIMEOUT = 10", "TIMEOUT = 30"),
              "@@\n-TIMEOUT = 10\n+TIMEOUT = 30\n"),
]

# Semantic near-misses must not be repaired by a similarity heuristic.
for case_id, before, anchor, replacement in [
    ("nonexistent_guard", 'def render():\n    if enabled:\n        return "ready"\n    return "idle"\n',
     '    if authorized:\n        return "ready"', '    if authorized:\n        return "done"'),
    ("nonexistent_call", 'def render():\n    return render_compact("panel")\n',
     '    return render("panel")', '    return render("expanded")'),
    ("nonexistent_identifier", "count = total_users\n", "count = active_users", "count = 0"),
    ("significant_string_space", 'label = "a b"\n', 'label = "ab"', 'label = "changed"'),
]:
    files = {"sample.py": before}
    body = "@@\n" + "".join("-" + line + "\n" for line in anchor.splitlines())
    body += "".join("+" + line + "\n" for line in replacement.splitlines())
    PAIRED_CASES.append(_case(case_id, "nonexistent_semantic_anchor", files,
        [_edit("sample.py", anchor, replacement)], _patch("sample.py", body), files,
        _native("rejected", files, "No normalization pass changes these semantic tokens.")))

PAIRED_CASES.extend([
    _case("trailing_whitespace", "whitespace", {"sample.py": "value = 1  \n"},
          [_edit("sample.py", "value = 1\n", "value = 2\n")],
          _patch("sample.py", "@@\n-value = 1\n+value = 2\n"),
          {"sample.py": "value = 2\n"},
          _native("applied", {"sample.py": "value = 2\n"}, "trim_end accepts trailing spaces.")),
    _case("uniform_indentation_offset", "indentation", {"sample.py": "def run():\n    begin()\n    finish()\n"},
          [_edit("sample.py", "  begin()\n  finish()", "    begin()\n    report()")],
          _patch("sample.py", "@@\n-  begin()\n-  finish()\n+    begin()\n+    report()\n"),
          {"sample.py": "def run():\n    begin()\n    report()\n"},
          _native("applied", {"sample.py": "def run():\n    begin()\n    report()\n"}, "trim ignores a uniform indentation offset.")),
    _case("changed_relative_indentation", "indentation", {"sample.py": "if enabled:\n    run()\n    finish()\n"},
          [_edit("sample.py", "if enabled:\n    run()\n        finish()", "if enabled:\n    run()\n    report()")],
          _patch("sample.py", "@@\n-if enabled:\n-    run()\n-        finish()\n+if enabled:\n+    run()\n+    report()\n"),
          {"sample.py": "if enabled:\n    run()\n    finish()\n"},
          _native("applied", {"sample.py": "if enabled:\n    run()\n    report()\n"},
                  "Native trim compares each line independently; it does not preserve relative indentation.")),
    _case("repeated_anchor", "ambiguity", {"sample.txt": "item\nseparator\nitem\n"},
          [_edit("sample.txt", "item", "changed")],
          _patch("sample.txt", "@@\n-item\n+changed\n"),
          {"sample.txt": "item\nseparator\nitem\n"},
          _native("applied", {"sample.txt": "changed\nseparator\nitem\n"}, "Native chooses the first match, not uniqueness admission.")),
    _case("repeated_anchor_explicit_all", "ambiguity", {"sample.txt": "item\nseparator\nitem\n"},
          [_edit("sample.txt", "item", "changed", replace_all=True)],
          _patch("sample.txt", "@@\n-item\n+changed\n@@\n-item\n+changed\n"),
          {"sample.txt": "changed\nseparator\nchanged\n"},
          _native("applied", {"sample.txt": "changed\nseparator\nchanged\n"}, "Two chunks locate two original occurrences with the forward cursor.")),
    _case("repeated_anchor_with_context", "ambiguity", {"sample.txt": "left\nitem\nright\nitem\n"},
          [_edit("sample.txt", "left\nitem", "left\nchanged")],
          _patch("sample.txt", "@@\n left\n-item\n+changed\n"),
          {"sample.txt": "left\nchanged\nright\nitem\n"},
          _native("applied", {"sample.txt": "left\nchanged\nright\nitem\n"}, "Extra source context disambiguates both interfaces.")),
    _case("landmark_selects_second", "patch_landmark", {"sample.txt": "one\nvalue = 1\ntwo\nvalue = 1\n"},
          [_edit("sample.txt", "two\nvalue = 1", "two\nvalue = 2")],
          _patch("sample.txt", "@@ two\n-value = 1\n+value = 2\n"),
          {"sample.txt": "one\nvalue = 1\ntwo\nvalue = 2\n"},
          _native("applied", {"sample.txt": "one\nvalue = 1\ntwo\nvalue = 2\n"}, "change_context advances past the landmark before matching old_lines.")),
    _case("evolving_hunk_dependency", "multi_hunk", {"sample.py": 'state = "old"\n'},
          [_edit("sample.py", 'state = "old"', 'state = "middle"'),
           _edit("sample.py", 'state = "middle"', 'state = "new"')],
          _patch("sample.py", '@@\n-state = "old"\n+state = "middle"\n@@\n-state = "middle"\n+state = "new"\n'),
          {"sample.py": 'state = "new"\n'},
          _native("rejected", {"sample.py": 'state = "old"\n'}, "Second chunk cannot see the first replacement; original_lines has no middle.")),
    _case("ordered_original_hunks", "multi_hunk", {"sample.py": "first = 1\nsecond = 2\n"},
          [_edit("sample.py", "first = 1", "first = 10"), _edit("sample.py", "second = 2", "second = 20")],
          _patch("sample.py", "@@\n-first = 1\n+first = 10\n@@\n-second = 2\n+second = 20\n"),
          {"sample.py": "first = 10\nsecond = 20\n"},
          _native("applied", {"sample.py": "first = 10\nsecond = 20\n"}, "Independent hunks in source order match original_lines.")),
    _case("reverse_original_hunks", "multi_hunk", {"sample.py": "first = 1\nsecond = 2\n"},
          [_edit("sample.py", "second = 2", "second = 20"), _edit("sample.py", "first = 1", "first = 10")],
          _patch("sample.py", "@@\n-second = 2\n+second = 20\n@@\n-first = 1\n+first = 10\n"),
          {"sample.py": "first = 10\nsecond = 20\n"},
          _native("rejected", {"sample.py": "first = 1\nsecond = 2\n"}, "Forward-only cursor cannot return to first after matching second.")),
])

_same = {"sample.py": "alpha = 1\nbeta = 2\n"}
PAIRED_CASES.append(_case("partial_same_file", "partial_preflight", _same,
    [_edit("sample.py", "alpha = 1", "alpha = 10"),
     _edit("sample.py", "missing = 3", "missing = 30"),
     _edit("sample.py", "beta = 2", "beta = 20")],
    _patch("sample.py", "@@\n-alpha = 1\n+alpha = 10\n@@\n-missing = 3\n+missing = 30\n@@\n-beta = 2\n+beta = 20\n"),
    {"sample.py": "alpha = 10\nbeta = 20\n"},
    _native("rejected", _same, "Every chunk in this Update File is computed before that file is written.")))

_multi = {"a.py": "alpha = 1\n", "b.py": "beta = 2\n", "c.py": "gamma = 3\n"}
PAIRED_CASES.append(_case("partial_multiple_files", "partial_preflight", _multi,
    [_edit("a.py", "alpha = 1", "alpha = 10"), _edit("b.py", "missing = 2", "beta = 20"),
     _edit("c.py", "gamma = 3", "gamma = 30")],
    "*** Begin Patch\n*** Update File: a.py\n@@\n-alpha = 1\n+alpha = 10\n"
    "*** Update File: b.py\n@@\n-missing = 2\n+beta = 20\n"
    "*** Update File: c.py\n@@\n-gamma = 3\n+gamma = 30\n*** End Patch\n",
    {**_multi, "a.py": "alpha = 10\n", "c.py": "gamma = 30\n"},
    _native("partial", {**_multi, "a.py": "alpha = 10\n"},
            "Standalone writes a then fails b and stops; verification rejects without writing any file.",
            ("standalone", "verification", "chunks"), verified={"status": "rejected", "files": _multi})))

PAIRED_CASES.extend([
    _case("duplicate_file_headers", "multi_hunk", {"sample.txt": "old\n"},
          [_edit("sample.txt", "old", "middle"), _edit("sample.txt", "middle", "new")],
          "*** Begin Patch\n*** Update File: sample.txt\n@@\n-old\n+middle\n"
          "*** Update File: sample.txt\n@@\n-middle\n+new\n*** End Patch\n",
          {"sample.txt": "new\n"},
          _native("applied", {"sample.txt": "new\n"},
                  "Standalone re-reads the file at each header; verification rejects duplicate target paths.",
                  ("standalone", "verification"), verified={"status": "rejected", "files": {"sample.txt": "old\n"}})),
    _case("truncated_patch", "patch_boundary", {"sample.txt": "old\n"},
          [_edit("sample.txt", "old", "new")],
          "*** Begin Patch\n*** Update File: sample.txt\n@@\n-old\n+new\n",
          {"sample.txt": "new\n"},
          _native("rejected", {"sample.txt": "old\n"}, "Missing End Patch fails native parser admission.", ("boundaries",))),
])

PAIRED_CASES_BY_ID = {case["id"]: case for case in PAIRED_CASES}
