"""兼容 re-export —— 新代码请直接走 `gensokyoai.engine`。

历史外部代码 / 测试还在 `from gensokyoai.loop import Loop, SceneContext`，
这个文件留着只是为了不打破 import。实现在 `engine/` 里。
"""

from .engine import Loop, Refresher, SceneContext, Turn
from .engine.text import (
    chance,
    images_of,
    last_message,
    salvage,
    segments_of,
    text_of,
    to_int,
)

__all__ = [
    "Loop",
    "Refresher",
    "SceneContext",
    "Turn",
    "chance",
    "images_of",
    "last_message",
    "salvage",
    "segments_of",
    "text_of",
    "to_int",
]
