"""旧路径保留的别名 —— 新代码请走 `gensokyoai.apis`。

历史插件里常见的 `from gensokyoai.api import scene, tool` 仍然有效，
但公开入口已经搬到 `gensokyoai.apis`。这个文件只做转发，不再实现。
"""

from .apis.scene import (
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
