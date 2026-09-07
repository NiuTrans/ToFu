"""Shared user-intent vocabulary for built-in capability discovery.

The gateway uses these concepts to bridge user phrasing to hidden tool
schemas, while the request router uses the same aliases to expose likely
families before the model runs. Tool-specific ranking hints remain on each
``ToolSpec``; this module owns only cross-tool capability language.
"""
from __future__ import annotations


CAPABILITY_SEARCH_CONCEPTS: dict[str, tuple[str, ...]] = {
    'slides': (
        'slides', 'slide', 'deck', 'presentation', 'powerpoint', 'ppt',
        'pptx', 'keynote', 'slide deck', '幻灯片', '演示文稿', '演示',
        '课件', '路演',
    ),
    'video': (
        'video', 'videos', 'film', 'films', 'clip', 'clips', 'short video',
        'motion graphics', 'mg animation', '视频', '短视频', '影片', '短片',
        '宣传片', '科普视频', '视频成片',
    ),
    'image': (
        'image', 'images', 'picture', 'pictures', 'photo', 'illustration',
        'cover image', 'poster', 'artwork', '图片', '图像', '封面图', '配图',
        '海报', '插画',
    ),
    'page_render': (
        'render', 'rendered', 'render page', 'page preview', 'preview',
        'headless browser', 'chromium', 'visual check', 'frontend preview',
        'webpage preview', 'ui', '渲染', '预览', '实际打开', '真实浏览器',
        '看看效果', '网页效果', '页面效果', '前端页面', '界面效果',
    ),
}


_ROUTE_GROUP_CAPABILITY_CONCEPTS: dict[str, tuple[str, ...]] = {
    'image': ('image',),
    'video': ('video', 'slides'),
    'page_preview': ('page_render',),
}


def route_capability_aliases(group: str) -> tuple[str, ...]:
    """Return substring-safe aliases for one request-router group."""
    aliases: list[str] = []
    for concept in _ROUTE_GROUP_CAPABILITY_CONCEPTS.get(group, ()):
        for alias in CAPABILITY_SEARCH_CONCEPTS[concept]:
            # The router performs substring matching. Avoid short ASCII tokens
            # such as ``ui`` (which also occurs inside unrelated words such as
            # ``build``); the gateway tokenizer may still use them exactly.
            if alias.isascii() and len(alias) < 3:
                continue
            if alias not in aliases:
                aliases.append(alias)
    return tuple(aliases)


__all__ = ['CAPABILITY_SEARCH_CONCEPTS', 'route_capability_aliases']
