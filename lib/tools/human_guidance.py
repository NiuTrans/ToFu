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
            "Use this when you need clarification, confirmation, or "
            "additional information from the user before proceeding. "
            "You can ask a free-text question or present multiple-choice options."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The question to ask the user. Be clear and specific. "
                        "Provide enough context so the user can answer effectively."
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
