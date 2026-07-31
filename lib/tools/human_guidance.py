"""lib/tools/human_guidance.py — ask_human tool schema for the LLM.

Provides the tool definition that allows the LLM to ask the user a question
mid-generation.  Supports two response modes:
- ``free_text``: user types a free-form answer
- ``choice``: user picks from a list of options provided by the LLM
"""

ASK_HUMAN_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_human",
        "description": (
            "Ask the user a question and wait for their response. "
            "Use this when you genuinely need clarification, confirmation, or "
            "additional information from the user before proceeding. "
            "You can ask a free-text question or present multiple-choice options.\n\n"
            "**Do NOT use this when you can decide from existing context.** Asking the "
            "user for things they expect you to figure out yourself is a frustration "
            "signal. Before asking, check: (1) does the conversation already contain "
            "the answer? (2) can a quick read_files / grep_search resolve the "
            "ambiguity? (3) is there a sensible default and the user can correct it "
            "after seeing the result? Reserve ask_human for genuinely irreversible "
            "decisions (which file to delete, which API key to use), strong "
            "preference forks (UI palette, file naming), or missing facts that the "
            "tools cannot recover (the user's intent, an offline credential)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The question to ask the user. Be clear and specific. "
                        "Provide enough context so the user can answer effectively. "
                        "Rendered as MARKDOWN, so an image reference like "
                        "`![alt](/api/images/<name>.png)` is displayed inline — this "
                        "is how you show something the user must LOOK AT or SCAN "
                        "(e.g. a scan-to-login QR code) while this call blocks "
                        "waiting for them. Build that question body with "
                        "`lib.qr.qr_login_question(url)`, which writes the QR PNG "
                        "and returns the markdown; do NOT paste base64 image data "
                        "into this field."
                    ),
                },
                "response_type": {
                    "type": "string",
                    "enum": ["free_text", "choice"],
                    "description": (
                        "How the user should respond: "
                        "'free_text' for open-ended answers, "
                        "'choice' for selecting from predefined options."
                    ),
                },
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "Short label for the option (displayed on the button).",
                            },
                            "description": {
                                "type": "string",
                                "description": "Optional longer description explaining the option.",
                            },
                        },
                        "required": ["label"],
                    },
                    "description": (
                        "List of options for 'choice' response_type. "
                        "Each option has a 'label' (required) and optional 'description'."
                    ),
                },
            },
            "required": ["question", "response_type"],
        },
    },
}

ASK_HUMAN_TOOL_NAME = 'ask_human'
HUMAN_GUIDANCE_TOOL_NAMES = frozenset({ASK_HUMAN_TOOL_NAME})

__all__ = ['ASK_HUMAN_TOOL', 'ASK_HUMAN_TOOL_NAME', 'HUMAN_GUIDANCE_TOOL_NAMES']
