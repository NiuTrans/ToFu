"""lib/tools/video_inspect.py — ``inspect_video`` tool definition.

``inspect_video`` lets a vision model understand any video the agent can name:
a project file, an absolute server-staged download (browser_download_url_to_server
returns ``data/fetched/server-download-*`` paths), or a chat video attachment
reference (``att_media_<id>``). The handler returns probe facts, timestamped key
frames as images (the read_files multi-image batch) and an optional timestamped
transcript; an audio-only file yields the probe facts and transcript as text.
The implementation owner is
:func:`lib.video_analysis.inspect_video_file`; it is the read-only sibling of
``inspect_image`` (lib/tools/image_edit.py).

``VIDEO_INSPECT_TOOL_NAMES`` is consumed by tool-display.
"""

VIDEO_INSPECT_TOOL_NAMES = frozenset({'inspect_video'})

INSPECT_VIDEO_TOOL = {
    "type": "function",
    "function": {
        "name": "inspect_video",
        "description": (
            "Watch a local video or hear an audio file: probe facts, timestamped "
            "key frames as images (video) and, when speech-to-text is available, "
            "a transcript. Pass start/end seconds to inspect one section densely. "
            "Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Project-relative or absolute path (e.g. a data/fetched/ "
                        "file from fetch_url or browser_download_url_to_server), "
                        "or an att_media_<id> chat video reference."
                    )
                },
                "start": {
                    "type": "number",
                    "description": "Window start, seconds (default 0)."
                },
                "end": {
                    "type": "number",
                    "description": "Window end, seconds (default: video end)."
                }
            },
            "required": ["path"]
        }
    }
}

__all__ = ['VIDEO_INSPECT_TOOL_NAMES', 'INSPECT_VIDEO_TOOL']
