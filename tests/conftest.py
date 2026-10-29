"""测试公共夹具 —— 场景注册表是全局的，每个用例跑完复原。"""

import pytest

from gensokyoai.apis.scene import _SCENES as scenes_registry


@pytest.fixture(autouse=True)
def _isolate_scenes():
    saved = dict(scenes_registry)
    yield
    scenes_registry.clear()
    scenes_registry.update(saved)
