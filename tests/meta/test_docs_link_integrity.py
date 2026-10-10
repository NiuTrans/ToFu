"""Every relative markdown link in tracked *.md files must resolve.

Docs are the model-facing map of this repository (docs/README.md is the
first-hop index; AGENTS.md files are the per-directory guides). A relative
link that points nowhere sends every reader — human or model — to a file that
is not there, and docs moves (like the contracts/proposals/runbooks grouping)
silently create them. This guard walks every tracked markdown file, extracts
relative links, and asserts each resolves to a tracked file or directory.

Scope is deliberately links only — prose path mentions are checked by the
owning structure tests (e.g. test_readme_structure_paths for the READMEs).
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

LINK_RE = re.compile(r"\]\(([^)\s]+?)(?:#[^)\s]*)?\)")
SKIP_SCHEMES = ("http://", "https://", "mailto:", "tel:")


def _tracked_markdown() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True,
        check=True)
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _broken_links():
    broken = []
    for rel in _tracked_markdown():
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for href in LINK_RE.findall(text):
            if href.startswith(SKIP_SCHEMES) or href.startswith(("#", "/")):
                continue
            if not re.search(r"\.(md|png|jpg|jpeg|gif|svg|webp|json|ya?ml|toml|"
                             r"py|js|ts|css|html|sh|txt|rs|sql|csv|jsonl|wasm|"
                             r"zip|tar|gz|pdf|woff2?|ttf|ico)([?#].*)?$",
                             href, re.I):
                continue
            target = (path.parent / href).resolve()
            try:
                target.relative_to(ROOT)
            except ValueError:
                broken.append((rel, href, "escapes the repository"))
                continue
            if not target.exists():
                broken.append((rel, href, "missing"))
    return broken


def test_every_relative_markdown_link_resolves():
    broken = _broken_links()
    assert not broken, (
        f"{len(broken)} dangling relative markdown link(s); "
        f"first 20: {broken[:20]}")
