"""Model-visible schemas for the paper quality-gate tools (Track D).

paper_check is the submission-readiness gate (source QA rules + PDF-level
font/DPI checks on the latest build), paper_review assembles the dossier the
calling model then fills as an expert reviewer, and paper_scaffold renders a
new-paper skeleton as text. All three are read-only: handlers write nothing.
Schemas live here, next to the other project tool declarations.
"""
from __future__ import annotations

PAPER_CHECK_TOOL = {
    "type": "function",
    "function": {
        "name": "paper_check",
        "description": (
            "Submission-readiness gate for the LaTeX paper: source-level "
            "academic QA rules (unreferenced figures, hard-coded numbers, "
            "caption style, heading structure, missing graphics files, "
            "undefined refs/citations) plus PDF-level checks on the latest "
            "build (fonts embedded — a hard IEEE/ACM requirement, image "
            "DPI, page count). Run before declaring the paper finished; fix "
            "all errors, then re-verify with paper_build."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Optional main .tex file or source directory, relative "
                        "to the project root. Defaults to the selected paper "
                        "document."
                    )
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "source", "pdf"],
                    "description": (
                        "'all' (default) runs source checks and, when a "
                        "successful build is retained, PDF checks; 'source' "
                        "or 'pdf' restrict to one family."
                    )
                }
            }
        }
    }
}

PAPER_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "paper_review",
        "description": (
            "Assemble a rigorous peer-review dossier for this paper (rubric "
            "+ the paper's key content + a claims-audit protocol). After "
            "calling this, YOU act as an expert reviewer: fill the rubric "
            "with 1-10 scores, write strengths/weaknesses/questions, run "
            "the claims audit, and end with a prioritized revision plan. "
            "Use when the user asks to review/critique the paper or prepare "
            "for submission."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Optional main .tex file or source directory, relative "
                        "to the project root. Defaults to the selected paper "
                        "document."
                    )
                },
                "focus": {
                    "type": "string",
                    "description": (
                        "Optional review focus requested by the user, e.g. "
                        "'soundness of the ablation' or 'related work "
                        "coverage'."
                    ),
                    "maxLength": 500
                }
            }
        }
    }
}

PAPER_SCAFFOLD_TOOL = {
    "type": "function",
    "function": {
        "name": "paper_scaffold",
        "description": (
            "Generate a LaTeX paper skeleton proposal (main.tex + "
            "per-section stubs) as TEXT for the user to confirm. After "
            "confirmation, create the files with write_file. Use when "
            "starting a new paper from scratch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Paper title for \\title{...}.",
                    "maxLength": 200
                },
                "sections": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 120},
                    "maxItems": 32,
                    "description": (
                        "Ordered section titles, e.g. ['Introduction', "
                        "'Related Work', 'Method', 'Experiments', "
                        "'Conclusion']."
                    )
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Optional target directory (relative to the project "
                        "root) the user has in mind; only echoed back in the "
                        "proposal, nothing is written."
                    )
                }
            },
            "required": ["title"]
        }
    }
}

PAPER_QUALITY_TOOL_NAMES = frozenset({
    "paper_check", "paper_review", "paper_scaffold"})
