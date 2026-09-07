"""lib/tools/image_gen.py — Image generation tool definition and constants.

Provides the ``generate_image`` tool that the LLM can call mid-conversation
to create images. ``imageGenEnabled`` controls whether its schema is kept
always visible; exact-name execution remains available when the configured
image backend can serve the request.

``IMAGE_GEN_TOOL_NAMES`` is used by tool-display, executor, and the
frontend's ``_isRoundImageGen()`` for UI rendering.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# Tool names for dispatch & display recognition
IMAGE_GEN_TOOL_NAMES = {'generate_image'}

GENERATE_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Create/draw/design an image, or edit one supplied by the user or "
            "produced earlier. For edits, pass the exact source_image reference and "
            "describe only the desired changes; a capable edit model preserves "
            "unmentioned content. Success returns a project path or /api/images/... "
            "reference—reuse it for later edits instead of regenerating. Use a "
            "detailed English prompt; aspect_ratio and resolution are optional."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Detailed English scene (style, composition, colors, "
                        "lighting) or edit instruction describing only desired changes."
                    )
                },
                "source_image": {
                    "type": "string",
                    "description": (
                        "Existing image URL/path to edit, including local "
                        "/api/images/... or a remote URL. Use the exact result "
                        "reference; omit to generate a new image."
                    )
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Aspect ratio; default 1:1.",
                    "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"]
                },
                "resolution": {
                    "type": "string",
                    "description": "1K standard (default) or 2K high resolution.",
                    "enum": ["1K", "2K"]
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Optional project-relative destination; missing directories "
                        "are created. If omitted or no project is active, saves to "
                        "server uploads. Multi-root: rootname:subdir targets that root; "
                        "bare relative uses primary. The prefix is routing, not a "
                        "literal filename."
                    )
                },
                "svg": {
                    "type": "boolean",
                    "description": (
                        "Also use vtracer with background removal to save a "
                        "same-name .svg beside the PNG; useful for scalable logos, "
                        "icons, and illustrations. Default false."
                    )
                }
            },
            "required": ["prompt"]
        }
    }
}

__all__ = ['IMAGE_GEN_TOOL_NAMES', 'GENERATE_IMAGE_TOOL']
