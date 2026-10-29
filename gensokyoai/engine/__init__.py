"""运行时引擎 —— Loop / Turn / Refresher 三个协作者。

`Loop` 是对外的入口：notify、run。`Turn` 是一次回合的执行细节。
`Refresher` 管前缀缓存的同步与压缩，跟"回不说话"无关。

旧的 import 路径 `from gensokyoai.loop import Loop, SceneContext` 仍然有效。
"""

from .loop import Loop
from .refresher import Refresher
from .text import salvage
from .turn import SceneContext, Turn

__all__ = ["Loop", "Refresher", "SceneContext", "Turn", "salvage"]
