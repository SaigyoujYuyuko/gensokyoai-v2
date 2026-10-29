"""GensokyoAI 公开 API —— 插件从这里 import，不依赖内核实现。

插件只需要：

    from gensokyoai.apis import scene, tool

    @scene(name="qq", prompt="你在一个 QQ 群里。", bubbles={"max": 3, "gap_s": 0.4})
    class QQScene:
        def __init__(self, ctx):
            self.ctx = ctx          # ctx.mouth / ctx.name / ctx.room / ctx.log

        @tool("read_member", "读一个人的群名片", {"user_id": "string"})
        async def read_member(self, user_id):
            return [{"name": "帕秋莉", "text": "红魔馆的魔法使"}]

        def describe(self, event):
            return event            # 可选：把事件改写成你想让模型看到的形状

        async def say(self, segments, reply_to=None):
            await self.ctx.mouth.send(self.ctx.name, "".join(
                s["text"] for s in segments if s["type"] == "text"))

旧路径 `gensokyoai.api` 仍 re-export 这些符号（向后兼容），新代码请直接用
`gensokyoai.apis`。
"""

from .scene import (
    API_VERSION,
    SceneContext,
    call_tool,
    drop_module,
    scene,
    scenes,
    schema_of,
    spec_of,
    tool,
    tools_of,
)

__all__ = [
    "API_VERSION",
    "SceneContext",
    "call_tool",
    "drop_module",
    "scene",
    "scenes",
    "schema_of",
    "spec_of",
    "tool",
    "tools_of",
]
