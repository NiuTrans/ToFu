"""lib/tools/produce.py — High-level "one sentence → finished video" tool.

The chat surface of the production substrate (docs/PRODUCTION_PIPELINE_DESIGN.md
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
                }
            },
            "required": ["topic"],
        },
    },
}

__all__ = ['PRODUCE_VIDEO_TOOL', 'PRODUCE_VIDEO_TOOL_NAME',
           'PRODUCE_REPORT_TOOL', 'PRODUCE_REPORT_TOOL_NAME',
           'PRODUCE_RESEARCH_TOOL', 'PRODUCE_RESEARCH_TOOL_NAME']

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
                    "description": "How many ideas to generate and score "
                                   "(3..12, default 6). Ideas that fail the "
                                   "novelty screen against the corpus are "
                                   "reported as rejected, with the reason."
                },
                "seed_arxiv_ids": {
                    "type": "array",
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
