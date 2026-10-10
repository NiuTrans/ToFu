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
            "Pause and ask the user questions. Use only when work cannot safely "
            "continue without an irreversible decision, subjective preference/product "
            "intent, or a fact unavailable from the conversation, files, and tools. "
            "First inspect context, use a quick read/grep when useful, and take a "
            "sensible reversible default when the user can correct the result. Do not "
            "ask for something you can decide. Supports free text or choices. "
            "Use `questions[]` to ask 2-8 questions as a single form (one interaction)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "maxLength": 32768,
                    "description": (
                        "Clear, specific MARKDOWN with enough context. To show or scan "
                        "an image, embed `![alt](/api/images/name.png)`; for a login QR "
                        "use `lib.qr.qr_login_question(url)`, never base64."
                    ),
                },
                "response_type": {
                    "type": "string",
                    "enum": ["free_text", "choice"],
                    "description": (
                        "free_text for open answers; choice requires options."
                    ),
                },
                "options": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "maxLength": 1024,
                                "description": "Short button label.",
                            },
                            "description": {
                                "type": "string",
                                "maxLength": 8192,
                                "description": "Optional explanation.",
                            },
                        },
                        "required": ["label"],
                    },
                    "description": "1-16 choices for choice mode.",
                },
                "questions": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "maxLength": 32768,
                                "description": "Question markdown.",
                            },
                            "response_type": {
                                "type": "string",
                                "enum": ["free_text", "choice"],
                                "description": "free_text or choice.",
                            },
                            "options": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 16,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "maxLength": 1024,
                                        },
                                        "description": {
                                            "type": "string",
                                            "maxLength": 8192,
                                        },
                                    },
                                    "required": ["label"],
                                },
                            },
                        },
                        "required": ["question", "response_type"],
                    },
                    "description": (
                        "Batch mode: 2-8 questions asked as one form (single interaction). "
                        "When present, top-level question/response_type/options are ignored."
                    ),
                },
            },
            "required": ["response_type"],
            "anyOf": [
                {
                    "properties": {
                        "response_type": {"const": "free_text"},
                        "questions": {"minItems": 2},
                    },
                    "required": ["questions"],
                },
                {
                    "properties": {
                        "response_type": {"const": "choice"},
                        "questions": {"minItems": 2},
                    },
                    "required": ["questions"],
                },
                {
                    "properties": {
                        "response_type": {"const": "free_text"},
                        "question": {"minLength": 1},
                    },
                    "required": ["question"],
                },
                {
                    "properties": {
                        "response_type": {"const": "choice"},
                        "question": {"minLength": 1},
                        "options": {"minItems": 1},
                    },
                    "required": ["question", "options"],
                },
            ],
        },
    },
}

ASK_HUMAN_TOOL_NAME = 'ask_human'
HUMAN_GUIDANCE_TOOL_NAMES = frozenset({ASK_HUMAN_TOOL_NAME})

__all__ = ['ASK_HUMAN_TOOL', 'ASK_HUMAN_TOOL_NAME', 'HUMAN_GUIDANCE_TOOL_NAMES']
