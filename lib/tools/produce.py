"""lib/tools/produce.py — High-level "one sentence → finished video" tool.

The chat surface of the production substrate (docs/modules/production.md
拍板 #5): a SINGLE semantically-clear tool so the model naturally selects it
when the user says "make me a science-explainer video about <topic>", instead
of orchestrating the 8 low-level ``motion_video_*`` tools by hand.

Unlike the low-level family, ``produce_video`` is NOT project-gated (拍板 #2):
"say one sentence and get a film" cannot require the user to first attach a
project, so a topic job with no project renders under the server's data dir.
It kicks off a background job (research → script → timeline → render →
assemble) and returns immediately with a ``task_id`` the UI polls — the model
does not block on the multi-minute render.
"""

from lib.log import get_logger
from lib.research.contracts import (
    MAX_RESEARCH_DIRECTION_CHARS,
    MAX_RESEARCH_IDEAS,
    MAX_RESEARCH_SEED_PAPERS,
    MIN_RESEARCH_IDEAS,
)

logger = get_logger(__name__)

PRODUCE_VIDEO_TOOL_NAME = 'produce_video'

PRODUCE_VIDEO_TOOL = {
    "type": "function",
    "function": {
        "name": "produce_video",
        "description": (
            "Produce a short science-explainer VIDEO from a single topic or "
            "news headline — fully automatic, no storyboard or SRT needed. "
            "The pipeline researches the topic on the web (every claim is "
            "grounded in a real source URL and credited on an end card), "
            "writes a spoken script, times it to real TTS narration, and "
            "renders a narrated vertical MG video. Returns immediately with a "
            "task_id; the video generates in the background (a few minutes) "
            "and the user watches progress in the video panel. Use this when "
            "the user asks for a video ABOUT something and has not supplied "
            "their own script/SRT/storyboard. Does NOT require an attached "
            "project."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The subject or news headline to explain, "
                                   "e.g. '为什么天空是蓝色的' or 'the James Webb "
                                   "telescope's latest exoplanet finding'."
                },
                "lang": {
                    "type": "string",
                    "enum": ["zh", "en"],
                    "description": "Narration language (default zh)."
                },
                "aspect": {
                    "type": "string",
                    "enum": ["1080x1440", "1080x1920", "1920x1080", "1080x1080"],
                    "description": "Frame aspect (default 1080x1440 vertical)."
                },
                "max_scenes": {
                    "type": "integer",
                    "description": "Upper bound on scene count / cost (3..12, "
                                   "default 8). Exceeding it degrades gracefully."
                },
                "narration": {
                    "type": "boolean",
                    "description": "TTS voice-over (default true; degrades to a "
                                   "silent video when no TTS slot is configured)."
                },
                "visual_quality": {
                    "type": "string",
                    "enum": ["template", "authored"],
                    "description": "'authored' (DEFAULT) gives every scene its "
                                   "own small agent that writes a bespoke "
                                   "composition with real typographic "
                                   "hierarchy, staggered entrances and "
                                   "supporting graphics. 'template' is the "
                                   "fast zero-LLM fallback card (one centred "
                                   "line on a gradient) — pass it only when "
                                   "the user explicitly wants speed over "
                                   "looks. A scene whose authoring fails "
                                   "falls back to the template, so the film "
                                   "always completes."
                },
                "creative_mode": {
                    "type": "string",
                    "enum": ["director", "standard"],
                    "description": "Planning strategy. 'director' (DEFAULT) "
                                   "drafts two contrasting evidence/visual "
                                   "plans, gates both, and uses an independent "
                                   "critic to select the stronger one. "
                                   "'standard' makes one planning call and is "
                                   "the lower-cost A/B control."
                },
                "model": {
                    "type": "string",
                    "description": "Optional model id that locks both the "
                                   "script writer and every scene author, "
                                   "for example 'kimi-k3'."
                }
            },
            "required": ["topic"],
        },
    },
}

PRODUCE_SLIDES_TOOL_NAME = 'produce_slides'

PRODUCE_SLIDES_TOOL = {
    "type": "function",
    "function": {
        "name": "produce_slides",
        "description": (
            "Produce a polished, DESIGNER-QUALITY slide deck (PPTX) from a "
            "single topic — fully automatic. The pipeline classifies the "
            "scenario (tech / business / academic / education / brand / "
            "report / analysis), binds ONE curated theme (palette + licensed "
            "CJK/Latin typefaces + the scenario's design bible), authors "
            "every page in the PPTD layout language, renders per-page "
            "previews, runs a multimodal visual-QA pass, and exports a "
            "NATIVE, fully editable .pptx (real text boxes, shapes, tables "
            "— not page images) with fade transitions. Returns immediately "
            "with a task_id; the deck generates in the background and the "
            "user watches progress. Use this whenever the user asks for a "
            "PPT / slide deck / presentation / 幻灯片 / 课件 / 路演. Does "
            "NOT require an attached project."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The subject of the deck, e.g. '小米 YU7 "
                                   "产品发布' or '固态电池技术路线评审'."
                },
                "style": {
                    "type": "string",
                    "description": "Optional style direction, e.g. '深色科技风' "
                                   "or 'editorial luxury'. The scenario/theme "
                                   "defaults are already curated; use this to "
                                   "steer (or to name a brand to anchor to)."
                },
                "lang": {
                    "type": "string",
                    "enum": ["zh", "en"],
                    "description": "Deck language (default zh)."
                },
                "max_pages": {
                    "type": "integer",
                    "description": "Upper bound on page count / cost (3..20, "
                                   "default 12)."
                },
                "size": {
                    "type": "string",
                    "enum": ["1280x720", "960x540", "720x540"],
                    "description": "Page geometry: 1280x720 (16:9, default), "
                                   "960x540 (16:9 compact), 720x540 (4:3)."
                },
                "creative_mode": {
                    "type": "string",
                    "enum": ["director", "standard"],
                    "description": "Planning strategy. 'director' (DEFAULT) "
                                   "drafts two contrasting deck plans, gates "
                                   "both, and uses an independent critic to "
                                   "select the stronger one. 'standard' is the "
                                   "single-plan A/B control."
                },
                "model": {
                    "type": "string",
                    "description": "Optional model id that locks the outline, "
                                   "page authors and repair rounds, for "
                                   "example 'kimi-k3'."
                }
            },
            "required": ["topic"],
        },
    },
}

EDIT_SLIDES_TOOL_NAME = 'edit_slides'

EDIT_SLIDES_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_slides",
        "description": (
            "Edit ONE page of a deck created by produce_slides, in plain "
            "language — e.g. '第 3 页标题改成…', 'make page 2 dark', '给第 "
            "5 页加一个柱状图'. The page is re-authored from its CURRENT "
            "layout (everything else stays), re-validated, re-rendered and "
            "the PPTX is re-exported. Use this for follow-up edits to a "
            "deck instead of regenerating the whole thing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The produce_slides job id."
                },
                "page": {
                    "type": "integer",
                    "description": "1-based page number to edit."
                },
                "instruction": {
                    "type": "string",
                    "description": "What to change on that page, in the "
                                   "user's words."
                },
                "lang": {
                    "type": "string",
                    "enum": ["zh", "en"],
                    "description": "Edit language (default zh)."
                }
            },
            "required": ["task_id", "page", "instruction"],
        },
    },
}

__all__ = ['PRODUCE_VIDEO_TOOL', 'PRODUCE_VIDEO_TOOL_NAME',
           'PRODUCE_REPORT_TOOL', 'PRODUCE_REPORT_TOOL_NAME',
           'PRODUCE_RESEARCH_TOOL', 'PRODUCE_RESEARCH_TOOL_NAME',
           'PRODUCE_SLIDES_TOOL', 'PRODUCE_SLIDES_TOOL_NAME',
           'EDIT_SLIDES_TOOL', 'EDIT_SLIDES_TOOL_NAME',
           'PRODUCE_TOOL_NAMES']

PRODUCE_REPORT_TOOL_NAME = 'produce_report'

PRODUCE_REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "produce_report",
        "description": (
            "Produce a long-form RESEARCH REPORT from a single topic — "
            "fully automatic. The pipeline researches the topic on the web "
            "(every claim grounded in a real source URL), drafts an outline, "
            "writes each section, and assembles a cited markdown report "
            "published as an artifact. Returns immediately with a task_id; "
            "the report generates in the background. Use this when the user "
            "wants a written deep-dive / report / briefing on a subject "
            "rather than a quick chat answer. Does NOT require an attached "
            "project."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The subject to research and report on."
                },
                "lang": {
                    "type": "string",
                    "enum": ["zh", "en"],
                    "description": "Report language (default zh)."
                },
                "depth": {
                    "type": "string",
                    "enum": ["brief", "standard", "deep"],
                    "description": "brief ≈3 sections, standard ≈5, deep ≈8. "
                                   "Deeper costs more and takes longer."
                }
            },
            "required": ["topic"],
        },
    },
}

PRODUCE_RESEARCH_TOOL_NAME = 'produce_research'

PRODUCE_RESEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "produce_research",
        "description": (
            "Find NOVEL, high-value RESEARCH IDEAS in a field — the automated "
            "research pipeline. Given a research direction it harvests the "
            "recent literature into a local paper corpus (parsed once, then "
            "reused), surveys it to map what has already been done, and "
            "proposes scored ideas that are screened against that corpus so "
            "they are genuinely new rather than A+B recombinations. Returns "
            "immediately with a task_id; the survey runs in the background "
            "(several minutes). Use this when the user asks what is worth "
            "working on, wants research directions / open problems / a gap "
            "analysis in a field — NOT for a written summary of a topic "
            "(that is produce_report). Does NOT require an attached project."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "maxLength": MAX_RESEARCH_DIRECTION_CHARS,
                    "description": "The research direction to mine for open "
                                   "problems, e.g. 'long-context KV cache "
                                   "compression' or '扩散模型的推理加速'."
                },
                "lang": {
                    "type": "string",
                    "enum": ["zh", "en"],
                    "description": "Output language (default en)."
                },
                "n_ideas": {
                    "type": "integer",
                    "minimum": MIN_RESEARCH_IDEAS,
                    "maximum": MAX_RESEARCH_IDEAS,
                    "description": "How many ideas to generate and score "
                                   "(3..12, default 6). Ideas that fail the "
                                   "novelty screen against the corpus are "
                                   "reported as rejected, with the reason."
                },
                "seed_arxiv_ids": {
                    "type": "array",
                    "maxItems": MAX_RESEARCH_SEED_PAPERS,
                    "items": {"type": "string"},
                    "description": "Optional arXiv ids to seed the corpus "
                                   "with, e.g. ['2312.00752']. Use when the "
                                   "user names specific papers to start from."
                }
            },
            "required": ["direction"],
        },
    },
}

#: Every name in the high-level produce_* family (one sentence → finished
#: product). Single source of truth for the display-dispatch table and the
#: frontend tool-card family parity guard.
PRODUCE_TOOL_NAMES = frozenset({
    PRODUCE_VIDEO_TOOL_NAME,
    PRODUCE_REPORT_TOOL_NAME,
    PRODUCE_RESEARCH_TOOL_NAME,
    PRODUCE_SLIDES_TOOL_NAME,
    EDIT_SLIDES_TOOL_NAME,
})
