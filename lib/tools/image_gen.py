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
            "Generate or edit an image. "
            "Use this when the user asks you to create, draw, design, visualize, "
            "or EDIT/MODIFY an image, illustration, diagram, logo, or any visual content. "
            "For image EDITING: if the user provides an image (or refers to one you "
            "generated earlier in this conversation) and asks to modify it — change "
            "background, recolor, add/remove objects, restyle, etc. — pass that image's "
            "URL or path as source_image and describe the change in prompt. Editing "
            "preserves the rest of the source image and is automatically routed to a "
            "capable model. After every successful generation/edit, this tool reports "
            "the saved image reference (a project path or an '/api/images/...' URL) in "
            "its result text — reuse that exact value as source_image to edit the image "
            "you just produced, instead of generating a new one from scratch. "
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
                    "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"]
                },
                "resolution": {
                    "type": "string",
                    "description": "Image resolution. '1K' for standard, '2K' for high-res. Default: '1K'.",
                    "enum": ["1K", "2K"]
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Optional relative file path within the project to save the generated image. "
                        "Example: 'assets/images/hero.png', 'static/logo.webp'. "
                        "If provided and a project is active, the image will be saved to this path "
                        "inside the project directory. The directory will be created if it doesn't exist. "
                        "If not provided, the image is still saved to the server's uploads folder.\n"
                        "**Multi-root workspaces**: prefix with `rootname:` to target a specific "
                        "non-primary root (e.g. 'hope-mcp:assets/logo.png'). Plain relative paths "
                        "resolve under the primary root. NEVER bake the colon into the filename — "
                        "writing 'hope-mcp:assets/logo.png' as a literal name would create a "
                        "top-level dir whose name contains a colon, which is almost never intended."
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
                    )
                }
            },
            "required": ["prompt"]
        }
    }
}

__all__ = ['IMAGE_GEN_TOOL_NAMES', 'GENERATE_IMAGE_TOOL']
