"""lib/tools/image_gen.py — Image generation tool definition and constants.

Provides the ``generate_image`` tool that the LLM can call mid-conversation
to create images.  The tool is gated by the ``imageGenEnabled`` toggle in
the frontend — when enabled, the tool appears in the LLM's tool list and
the executor calls ``lib.image_gen.generate_image()`` to produce the image.

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
            "Generate or edit an image. "
            "Use this when the user asks you to create, draw, design, visualize, "
            "or EDIT/MODIFY an image, illustration, diagram, logo, or any visual content. "
            "For image EDITING: if the user provides an image and asks to modify it "
            "(change background, add objects, change style, remove elements, etc.), "
            "pass the image URL as source_image. "
            "Provide a detailed English prompt for best results. "
            "You can optionally specify aspect ratio and resolution."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Detailed description of the image to generate or edit instruction. "
                        "For generation: describe the full image (style, composition, colors, lighting). "
                        "For editing: describe what changes to make to the source image "
                        "(e.g. 'change the background to a beach sunset', 'add sunglasses to the person', "
                        "'convert to watercolor painting style'). Use English for best results."
                    )
                },
                "source_image": {
                    "type": "string",
                    "description": (
                        "URL of an existing image to edit/modify. "
                        "Use this when the user wants to modify an existing image rather than "
                        "create one from scratch. Can be a local URL (e.g. '/api/images/gen_xxx.png') "
                        "or a remote URL. When provided, the prompt should describe the desired edits. "
                        "If not provided, a new image is generated from scratch."
                    )
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": (
                        "Aspect ratio of the image. "
                        "Options: '1:1' (square), '16:9' (landscape), '9:16' (portrait), "
                        "'4:3', '3:4'. Default: '1:1'."
                    ),
                    "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"],
                    "default": "1:1"
                },
                "resolution": {
                    "type": "string",
                    "description": "Image resolution. '1K' for standard, '2K' for high-res. Default: '1K'.",
                    "enum": ["1K", "2K"],
                    "default": "1K"
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Optional relative file path within the project to save the generated image. "
                        "Example: 'assets/images/hero.png', 'static/logo.webp'. "
                        "If provided and a project is active, the image will be saved to this path "
                        "inside the project directory. The directory will be created if it doesn't exist. "
                        "If not provided, the image is still saved to the server's uploads folder."
                    )
                },
                "svg": {
                    "type": "boolean",
                    "description": (
                        "Whether to also convert the generated PNG to an SVG vector file. "
                        "When true, uses vtracer to trace the PNG into a clean SVG with "
                        "automatic background removal. The SVG is saved alongside the PNG "
                        "with the same name but .svg extension. "
                        "Useful for logos, icons, mascots, and illustrations that need to "
                        "scale without pixelation. Default: false."
                    ),
                    "default": False
                }
            },
            "required": ["prompt"]
        }
    }
}

__all__ = ['IMAGE_GEN_TOOL_NAMES', 'GENERATE_IMAGE_TOOL']
