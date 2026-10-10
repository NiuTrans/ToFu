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
            "Read fine detail by transforming the ORIGINAL image at source "
            "resolution before model downscaling. Read the whole image once, then "
            "inspect ONE crop/zoom region per call; many returned images are "
            "downscaled together. Use grid=true first for 0–1 coordinates, then "
            "crop. READ-ONLY; never changes the source."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Local path using read_files rules (png/jpg/jpeg/gif/webp/bmp), "
                        "or a chat upload's shown /api/images/<file> reference. Do not "
                        "invent a path such as /dev/null."
                    )
                },
                "crop": {
                    "type": "array",
                    "description": (
                        "Keep [x0,y0,x1,y1]. Each 0–1 value is its axis fraction; >1 "
                        "is pixels, and units may mix. Coordinates follow the visible "
                        "frame after EXIF orientation then rotate. [.5,0,1,.5] is the "
                        "top-right quadrant. Crop wins over zoom; omit for the whole "
                        "frame. Prefer grid fractions."
                    ),
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4
                },
                "zoom": {
                    "type": "number",
                    "description": (
                        "Centre zoom >1; 2 keeps the middle quarter. Used only when "
                        "crop is omitted."
                    )
                },
                "rotate": {
                    "type": "integer",
                    "description": (
                        "Clockwise degrees after EXIF orientation; default 0."
                    ),
                    "enum": [0, 90, 180, 270]
                },
                "grid": {
                    "type": "boolean",
                    "description": (
                        "Overlay labelled 0–1 coordinates for a later crop; default false."
                    )
                }
            },
            "required": ["path"]
        }
    }
}

__all__ = ['IMAGE_EDIT_TOOL_NAMES', 'INSPECT_IMAGE_TOOL']
