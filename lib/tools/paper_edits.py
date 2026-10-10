"""Model-visible schema for the paper edit-proposal tool.

The handler parks exact-anchor hunks in the bounded owner-scoped proposal
store for browser review; accepted hunks land through the journaled write
tools via the proposal routes. Schemas live here, next to the other project
tool declarations.
"""
from __future__ import annotations

PAPER_PROPOSE_EDITS_TOOL = {
    "type": "function",
    "function": {
        "name": "paper_propose_edits",
        "description": (
            "Propose .tex/.bib edits for USER REVIEW instead of applying them "
            "directly. Each edit is an exact-anchor search/replace (the same "
            "matching rules as apply_diff: include enough context to match "
            "once). The user sees every hunk as a diff with accept/reject. Use "
            "when the user asks to see changes before they are applied, for "
            "risky refactors, or when offering alternative phrasings. The "
            "proposal is validated immediately against the current files; "
            "hunks that no longer match are marked failed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Optional main .tex file or source directory, relative to "
                        "the project root. Defaults to the selected paper document."
                    )
                },
                "edits": {
                    "type": "array",
                    "maxItems": 20,
                    "description": (
                        "Hunks to propose, at most 20. Paths are relative to the "
                        "paper document's main-file directory."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Target file, relative to the paper document's main-file directory."
                            },
                            "search": {
                                "type": "string",
                                "description": "Exact anchor text to find; must match the current file exactly once."
                            },
                            "replace": {
                                "type": "string",
                                "description": "Replacement text (omit or empty to delete the anchor)."
                            },
                            "description": {
                                "type": "string",
                                "maxLength": 200,
                                "description": "Short human-readable summary of this hunk."
                            }
                        },
                        "required": ["path", "search"]
                    }
                }
            },
            "required": ["edits"]
        }
    }
}

PAPER_EDITS_TOOL_NAMES = frozenset({'paper_propose_edits'})
