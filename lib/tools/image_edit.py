"""lib/tools/image_edit.py — Image inspection tool definition and constants.

Provides the ``inspect_image`` tool that lets the LLM request a different
*view* of a local image — zoom into a region, rotate, or crop — re-rendered
server-side from the ORIGINAL file at full source resolution.

Why this exists: when the model reads a large image via ``read_files``, the
file is compressed to ~1 MB and then downscaled to the model's pixel ceiling,
so fine detail in a big schematic/diagram/screenshot is unreadable. A regular
re-read returns the same squashed snapshot. ``inspect_image`` instead crops
the original bytes BEFORE downscaling, so a region comes back sharp.

The handler emits the same ``__screenshot__`` protocol as a normal image
read, so dispatch / compaction / inline rendering / billing all work unchanged.

``IMAGE_EDIT_TOOL_NAMES`` is consumed by tool-display and the frontend's
tool-round renderer for icon/badge selection.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# Tool names for dispatch & display recognition
IMAGE_EDIT_TOOL_NAMES = {'inspect_image'}

INSPECT_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "inspect_image",
        "description": (
            "Get a closer, transformed VIEW of a local image to read detail that is "
            "too small to see in a normal read. When you read a large image (a "
            "schematic, diagram, screenshot, scanned page, photo of a whiteboard) it "
            "is downscaled to fit the model's input limit, so fine text and symbols "
            "blur. This tool re-renders the image FROM THE ORIGINAL FILE at full "
            "resolution after applying crop / zoom / rotate, recovering that detail.\n\n"
            "**Typical workflow:** read the whole image once to get oriented, then "
            "call inspect_image with `crop` (or `zoom`) on the region you need to "
            "read. Inspect ONE region at a time — each call adds another image to "
            "the conversation, and many large images get re-downscaled together.\n\n"
            "Pass `grid=true` on a first pass to overlay a 0.0–1.0 coordinate grid, "
            "then use those numbers to pick a precise `crop` box on the next call.\n\n"
            "This is READ-ONLY: it never modifies the file on disk."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path to the image file (relative to the project root, or an "
                        "absolute / ~ path). Same path rules as read_files. Supported "
                        "formats: png, jpg, jpeg, gif, webp, bmp."
                    )
                },
                "crop": {
                    "type": "array",
                    "description": (
                        "Region to keep, as [x0, y0, x1, y1]. Values between 0 and 1 "
                        "are fractions of the image size (e.g. [0.5, 0, 1, 0.5] = "
                        "top-right quadrant); values greater than 1 are absolute "
                        "pixels. Omit to keep the whole frame."
                    ),
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4
                },
                "zoom": {
                    "type": "number",
                    "description": (
                        "Convenience centre-zoom factor > 1 (e.g. 2 keeps the middle "
                        "quarter, magnified). Applied AFTER crop. Use crop instead "
                        "when the region of interest is off-centre."
                    )
                },
                "rotate": {
                    "type": "integer",
                    "description": (
                        "Clockwise rotation in degrees. One of 0, 90, 180, 270. "
                        "Useful for sideways scans or photos. Default 0."
                    ),
                    "enum": [0, 90, 180, 270]
                },
                "grid": {
                    "type": "boolean",
                    "description": (
                        "Overlay a labelled 0.0–1.0 coordinate grid on the returned "
                        "view to help you choose a precise `crop` box next. Default false."
                    )
                }
            },
            "required": ["path"]
        }
    }
}

__all__ = ['IMAGE_EDIT_TOOL_NAMES', 'INSPECT_IMAGE_TOOL']
