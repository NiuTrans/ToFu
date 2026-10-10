"""Model-visible schemas for the PID-anchored citation tools (Track C).

Citations enter the manuscript ONLY from real search results (OpenAlex) with
BibTeX fetched by persistent identifier (Crossref/OpenAlex), so a hallucinated
reference is architecturally impossible. Schemas live here, next to the other
project tool declarations; the handler resolves them in
``lib/tasks_pkg/handlers/paper_citations.py``.
"""
from __future__ import annotations

PAPER_CITE_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "paper_cite_search",
        "description": (
            "Search REAL academic literature (OpenAlex, 250M works) to cite. "
            "Returns verified candidate works with DOIs, venue, year, citation "
            "counts and retraction flags. ALWAYS search before inserting a "
            "citation — never invent references."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Free-text search: title keywords, author names, "
                        "topic, or a combination, e.g. 'attention is all you "
                        "need' or 'vaswani transformer'."
                    ),
                    "maxLength": 300
                },
                "limit": {
                    "type": "integer",
                    "description": "Max candidates to return (1-25, default 8).",
                    "minimum": 1,
                    "maximum": 25
                }
            },
            "required": ["query"]
        }
    }
}

PAPER_CITE_INSERT_TOOL = {
    "type": "function",
    "function": {
        "name": "paper_cite_insert",
        "description": (
            "Fetch the work's real BibTeX (Crossref/OpenAlex), append it to "
            "the project's .bib (deduplicated), and insert \\cite{key} at the "
            "anchor in a .tex file. anchor = a short unique search string in "
            "the file; the \\cite is inserted right after the anchor text. "
            "Omit anchor to only add the .bib entry."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": (
                        "DOI of the work to cite, e.g. 10.1038/nature14539 "
                        "(from paper_cite_search). Exactly one of "
                        "doi/openalex_id/arxiv is required."
                    ),
                    "maxLength": 256
                },
                "openalex_id": {
                    "type": "string",
                    "description": "OpenAlex work ID, e.g. W2741809807 or https://openalex.org/W2741809807.",
                    "maxLength": 64
                },
                "arxiv": {
                    "type": "string",
                    "description": "arXiv ID, e.g. 1706.03762.",
                    "maxLength": 64
                },
                "key": {
                    "type": "string",
                    "description": (
                        "BibTeX cite key override (1-64 chars of "
                        "A-Za-z0-9:_./-). Defaults to a proposed "
                        "firstauthorYearTitleword key."
                    ),
                    "maxLength": 64
                },
                "cite_file": {
                    "type": "string",
                    "description": (
                        ".bib file, relative to the project root, to append "
                        "the entry to. Defaults to the first existing .bib "
                        "next to the paper; refs.bib is created if none exists."
                    ),
                    "maxLength": 256
                },
                "tex_file": {
                    "type": "string",
                    "description": (
                        ".tex file, relative to the project root, for the "
                        "anchor insertion. Defaults to the paper's main file."
                    ),
                    "maxLength": 256
                },
                "anchor": {
                    "type": "string",
                    "description": (
                        "Short UNIQUE search string in tex_file (4-300 chars, "
                        "must occur exactly once). \\cite{key} is inserted "
                        "right after it. Omit to only add the .bib entry."
                    ),
                    "maxLength": 300
                },
                "anchor_position": {
                    "type": "string",
                    "enum": ["after", "before"],
                    "description": "Insert the \\cite after (default) or before the anchor text."
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Optional main .tex file to select the paper document, "
                        "as in paper_build. Defaults to the selected document."
                    )
                }
            }
        }
    }
}

PAPER_CITATION_AUDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "paper_citation_audit",
        "description": (
            "Verify every .bib entry / \\cite key in the paper against "
            "OpenAlex: flags probable-hallucinated entries (not found), "
            "metadata mismatches (title/year/author), RETRACTED papers, "
            "duplicate keys, undefined citations and unused entries. Run "
            "before submission or after bulk citation edits."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Optional main .tex file to select the paper document. "
                        "Defaults to the conversation's selected paper document."
                    )
                }
            }
        }
    }
}

PAPER_CITATION_TOOL_NAMES = frozenset({
    "paper_cite_search", "paper_cite_insert", "paper_citation_audit"})
