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
                },
                "template_id": {
                    "type": "string",
                    "description": "Optional visual template id (tpl_...) "
                                   "from the import-template upload: the "
                                   "deck binds the uploaded brand deck's "
                                   "palette/typefaces/geometry instead of "
                                   "a curated theme."
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
            "Edit ONE or SEVERAL pages of a deck created by produce_slides, "
            "in plain language — e.g. '第 3 页标题改成…', 'make page 2 "
            "dark', '给第 5 页加一个柱状图'. Each page is re-authored from "
            "its CURRENT layout (everything else stays), re-validated, "
            "re-rendered and the PPTX is re-exported once at the end. Use "
            "this for content rewrites / layout restructuring; for simple "
            "field-level changes (改文字/字号/颜色/图片) prefer patch_slides "
            "(deterministic, no LLM, seconds). Pass `page` for one page or "
            "`pages` for a batch (≤8) sharing one instruction."
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
                    "description": "1-based page number to edit (single-page "
                                   "form; mutually exclusive with pages)."
                },
                "pages": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "integer"},
                    "description": "1-based page numbers for a batch edit "
                                   "with the same instruction (≤8 pages)."
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
            "required": ["task_id", "instruction"],
        },
    },
}

EDIT_VIDEO_TOOL_NAME = 'edit_video'

EDIT_VIDEO_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_video",
        "description": (
            "Edit ONE scene of a video created by produce_video, in plain "
            "language — e.g. '把第 2 个镜头的标题改成…', 'make scene 3 "
            "slower and darker', '给第 4 镜换成柱状图表达'. Two modes: "
            "'rewrite' (default) has the scene author rewrite that scene's "
            "composition from its CURRENT frame (everything else stays; the "
            "better of old/new wins), then only that scene is re-rendered "
            "and the final video re-assembled in place. 'set_narration' "
            "replaces the scene's spoken narration text deterministically "
            "(new TTS must FIT the scene's existing duration — shorten the "
            "text when it overflows); the frame is untouched and only the "
            "audio/subtitles rebuild. Returns immediately with an edit "
            "task_id. Use this for follow-up edits instead of regenerating "
            "the whole film. Scene numbers are 1-based (scene-001 … "
            "scene-NNN also accepted)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The produce_video job id."
                },
                "scene": {
                    "type": "string",
                    "description": "Scene to edit: 1-based number ('2') or "
                                   "scene id ('scene-002')."
                },
                "instruction": {
                    "type": "string",
                    "description": "rewrite: what to change in that scene, "
                                   "in the user's words. set_narration: the "
                                   "NEW narration text for the scene. "
                                   "set_subtitle_style: a short note (may be "
                                   "a placeholder like '字幕样式'). "
                                   "swap_asset: the QUERY describing the "
                                   "replacement asset."
                },
                "op": {
                    "type": "string",
                    "enum": ["rewrite", "set_narration", "set_subtitle_style",
                             "swap_asset"],
                    "description": "Edit mode (default 'rewrite'). "
                                   "set_subtitle_style is JOB-LEVEL (scene "
                                   "may be omitted) and needs `style`."
                },
                "style": {
                    "type": "object",
                    "description": "set_subtitle_style: subtitle overrides — "
                                   "font_scale (0.6..1.8), font_px (10..200), "
                                   "margin_v (px from bottom), outline "
                                   "(0..12), font_name."
                },
                "model": {
                    "type": "string",
                    "description": "Optional model id locking the scene "
                                   "author for this edit (rewrite only)."
                },
                "asset_kind": {
                    "type": "string",
                    "enum": ["stock", "generate", "t2v"],
                    "description": "swap_asset: fetch stock media (default), "
                                   "AI-generate a still, or 't2v' — a "
                                   "text-to-video CLIP (video targets only, "
                                   "needs TOFU_T2V_* configured)."
                },
                "target": {
                    "type": "string",
                    "description": "swap_asset: the scene's current media "
                                   "path to replace (required only when the "
                                   "scene references several media files)."
                }
            },
            "required": ["task_id", "scene", "instruction"],
        },
    },
}

PATCH_SLIDES_TOOL_NAME = 'patch_slides'

PATCH_SLIDES_TOOL = {
    "type": "function",
    "function": {
        "name": "patch_slides",
        "description": (
            "DETERMINISTIC field-level edits to ONE page of a deck created "
            "by produce_slides — zero LLM, applies in seconds, fully "
            "predictable. PREFER this over edit_slides whenever the user's "
            "request maps to a field mutation: 改标题/正文文字 (set_text), "
            "调字号/颜色/加粗 (set_style), 挪位置/改大小 (set_bounds), "
            "换图片 (swap_image), 删元素 (delete), 复制元素 (duplicate). "
            "Targets are elementId / componentId shown in the page source; "
            "use edit_slides instead when the change needs rewriting copy, "
            "restructuring the layout, or editing component internals "
            "beyond metric/quote/code scalar fields. Every op is validated "
            "and layout-measured; a patch that breaks validation or worsens "
            "the page's measured layout is rolled back and reported."
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
                    "description": "1-based page number to patch."
                },
                "ops": {
                    "type": "array",
                    "maxItems": 16,
                    "description": "Ordered patch operations.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": ["set_text", "set_style", "set_bounds",
                                         "swap_image", "delete", "duplicate"]
                            },
                            "target": {
                                "type": "string",
                                "description": "elementId or componentId on "
                                               "this page."
                            },
                            "text": {
                                "type": "string",
                                "description": "set_text: the new text."
                            },
                            "field": {
                                "type": "string",
                                "description": "set_text on a component: the "
                                               "scalar field (metric: value|"
                                               "label|support|source; quote: "
                                               "text|source; code: code)."
                            },
                            "props": {
                                "type": "object",
                                "description": "set_style: style fields "
                                               "(fontSize, color, bold, "
                                               "italic, fontFamily, "
                                               "lineHeight, letterSpacing, "
                                               "align, backgroundColor)."
                            },
                            "bounds": {
                                "type": "array",
                                "items": {"type": "number"},
                                "description": "set_bounds: [x, y, w, h] in "
                                               "page points."
                            },
                            "src": {
                                "type": "string",
                                "description": "swap_image: new image src "
                                               "(deck media path or https)."
                            },
                            "newId": {
                                "type": "string",
                                "description": "duplicate: optional explicit "
                                               "id for the clone."
                            },
                            "dx": {"type": "number"},
                            "dy": {"type": "number"}
                        },
                        "required": ["op", "target"]
                    }
                }
            },
            "required": ["task_id", "page", "ops"],
        },
    },
}

RESTYLE_SLIDES_TOOL_NAME = 'restyle_slides'

RESTYLE_SLIDES_TOOL = {
    "type": "function",
    "function": {
        "name": "restyle_slides",
        "description": (
            "Deck-LEVEL deterministic restyle of a deck created by "
            "produce_slides — zero LLM, re-renders in seconds. PREFER this "
            "whenever the user asks to restyle the WHOLE deck: 换主题 "
            "(theme_id), 换配色 (colors), 换字体 (fonts). The deck's "
            "elements reference theme tokens precisely so the entire "
            "visual language changes without rewriting one page. Do NOT "
            "re-run produce_slides for a restyle."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The produce_slides job id."
                },
                "theme_id": {
                    "type": "string",
                    "description": "Curated theme id to bind (palette + "
                                   "typeface pairing + table defaults). The "
                                   "error reply lists available ids."
                },
                "colors": {
                    "type": "object",
                    "description": "Palette slot overrides, e.g. "
                                   "{\"primary\": \"#0E7A5F\", \"bg\": "
                                   "\"#101418\"}. Slots: bg, ink, primary, "
                                   "accent, muted, hairline; hex only."
                },
                "fonts": {
                    "type": "object",
                    "description": "Typeface swap per role: {\"title\": "
                                   "\"ZCOOL XiaoWei\", \"body\": "
                                   "\"MiSans\"}. Roles: title, body, "
                                   "caption, bignum; registry family or id."
                }
            },
            "required": ["task_id"],
        },
    },
}

REVERT_SLIDES_TOOL_NAME = 'revert_slides'

REVERT_SLIDES_TOOL = {
    "type": "function",
    "function": {
        "name": "revert_slides",
        "description": (
            "Undo a conversational deck edit: restore the deck to the "
            "snapshot taken before a patch_slides / restyle_slides / "
            "edit_slides step (versions are listed in every edit result, "
            "newest first). The restored deck is re-validated, every page "
            "re-rendered and the PPTX re-exported. Reverting keeps the "
            "snapshot — the user can bounce between versions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The produce_slides job id."
                },
                "version": {
                    "type": "integer",
                    "description": "Snapshot version to restore (from the "
                                   "edit results' versions list)."
                }
            },
            "required": ["task_id", "version"],
        },
    },
}

REVERT_VIDEO_TOOL_NAME = 'revert_video'

REVERT_VIDEO_TOOL = {
    "type": "function",
    "function": {
        "name": "revert_video",
        "description": (
            "Undo a conversational scene edit: restore ONE scene's "
            "composition to the snapshot taken before an edit_video "
            "rewrite (every rewrite result carries its `version`). Only "
            "that scene is re-rendered and the final video re-assembles at "
            "its original URL. Narration/audio is not affected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The produce_video job id."
                },
                "scene": {
                    "type": "string",
                    "description": "Scene to revert: 1-based number ('2') "
                                   "or scene id ('scene-002')."
                },
                "version": {
                    "type": "integer",
                    "description": "Snapshot version to restore."
                }
            },
            "required": ["task_id", "scene", "version"],
        },
    },
}

__all__ = ['PRODUCE_VIDEO_TOOL', 'PRODUCE_VIDEO_TOOL_NAME',
           'PRODUCE_REPORT_TOOL', 'PRODUCE_REPORT_TOOL_NAME',
           'PRODUCE_RESEARCH_TOOL', 'PRODUCE_RESEARCH_TOOL_NAME',
           'PRODUCE_SLIDES_TOOL', 'PRODUCE_SLIDES_TOOL_NAME',
           'EDIT_SLIDES_TOOL', 'EDIT_SLIDES_TOOL_NAME',
           'EDIT_VIDEO_TOOL', 'EDIT_VIDEO_TOOL_NAME',
           'PATCH_SLIDES_TOOL', 'PATCH_SLIDES_TOOL_NAME',
           'RESTYLE_SLIDES_TOOL', 'RESTYLE_SLIDES_TOOL_NAME',
           'REVERT_SLIDES_TOOL', 'REVERT_SLIDES_TOOL_NAME',
           'REVERT_VIDEO_TOOL', 'REVERT_VIDEO_TOOL_NAME',
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
            "Develop candidate RESEARCH IDEAS in a field — the automated "
            "research pipeline. Given a research direction it harvests the "
            "recent literature into a local paper corpus (parsed once, then "
            "reused), surveys it to map what has already been done, and "
            "proposes scored ideas screened against that inspected corpus. "
            "Novelty and value judgments are provisional and limited to the "
            "retrieved evidence. The output is a survey and candidate ideas; "
            "research conclusions require reproducible baseline and candidate "
            "experiments, independent validation and inspection of originals. "
            "Continue the user goal through those stages before claiming "
            "empirical gains or publication readiness. Returns "
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
    EDIT_VIDEO_TOOL_NAME,
    PATCH_SLIDES_TOOL_NAME,
    RESTYLE_SLIDES_TOOL_NAME,
    REVERT_SLIDES_TOOL_NAME,
    REVERT_VIDEO_TOOL_NAME,
})
