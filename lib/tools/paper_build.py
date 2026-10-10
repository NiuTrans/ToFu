"""Model-visible schema for the conversation paper-build tool.

The handler compiles through the bounded paper-build service (never a raw
subprocess), so the agent sees the same verified receipt the browser preview
consumes. Schemas live here, next to the other project tool declarations.
"""
from __future__ import annotations

PAPER_BUILD_TOOL = {
    "type": "function",
    "function": {
        "name": "paper_build",
        "description": (
            "Compile this conversation's LaTeX paper project through the bounded "
            "build service and read the verified result: status, engine, and "
            "structured diagnostics with file/line positions. ALWAYS call this "
            "after creating or editing .tex/.bib/.sty/.cls files: if the build "
            "failed, fix every error at its reported file:line and compile again "
            "before finishing — never leave the project in a state that does not "
            "compile. action='diagnostics' reports the most recent build without "
            "recompiling (cheap check). The document defaults to the "
            "conversation's selected paper document; pass `path` to target a "
            "specific main .tex file instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["compile", "diagnostics"],
                    "description": (
                        "'compile' (default) runs a fresh build and waits for the "
                        "verified receipt; 'diagnostics' only reports the latest "
                        "build for the resolved document."
                    )
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Optional main .tex file or source directory, relative to "
                        "the project root. Defaults to the selected paper document."
                    )
                },
                "engine": {
                    "type": "string",
                    "enum": ["auto", "pdflatex", "xelatex", "lualatex"],
                    "description": "TeX engine override; 'auto' detects from the preamble (default)."
                }
            }
        }
    }
}

PAPER_LAYOUT_TOOL = {
    "type": "function",
    "function": {
        "name": "paper_layout",
        "description": (
            "Report the page budget of the conversation's LaTeX paper from the "
            "latest verified build: total page count, where each section starts "
            "(file:line → PDF page), and how full the last page is. Use this to "
            "answer 'how many pages is it now' and to plan page-limit work — "
            "e.g. when asked to compress to N pages, read which sections span "
            "the most pages, shorten those, then re-verify with paper_build. "
            "Never edits anything; reads the latest retained build."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Optional main .tex file, relative to the project root. "
                        "Defaults to the selected paper document."
                    )
                }
            }
        }
    }
}

PAPER_ARXIV_IMPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "paper_import_arxiv",
        "description": (
            "Import an arXiv paper's TeX SOURCE into this conversation's project "
            "root as a compilable project: downloads the bounded e-print archive, "
            "extracts it atomically into a fresh arxiv-<id>/ directory (never "
            "overwrites existing files), discovers the main document and selects "
            "it for the paper preview. Use this when the user wants to read, "
            "reuse, or build on an arXiv paper's LaTeX — then edit sources and "
            "verify with paper_build. Requires a local project folder attached "
            "to the conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "arxiv": {
                    "type": "string",
                    "description": "arXiv ID or URL, e.g. 2301.12345 or https://arxiv.org/abs/2301.12345",
                    "maxLength": 160
                }
            },
            "required": ["arxiv"]
        }
    }
}

PAPER_BUILD_TOOL_NAMES = frozenset({
    "paper_build", "paper_layout", "paper_import_arxiv"})
