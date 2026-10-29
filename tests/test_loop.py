"""Agent 回合测试 —— 走公开 API：Loop / Turn / Refresher / Room。

不碰任何私有成员：制表只通过构造注入（Loop 构造参数 + 假 Mind/Mouth/Memory）
与公开方法（notify/run/is_repeat/is_awake/turn.run/refresher.sync）完成。
"""

import asyncio
from typing import ClassVar

import pytest

from gensokyoai.engine import Loop, Refresher, SceneContext, salvage
from gensokyoai.llm.base import ModelError
from gensokyoai.model import Reply, ToolCall
from gensokyoai.room import Room


class _ScriptedMind:
    """按脚本返回动作的假心智。"""

    prompt_cache = None
    name = "觉"

    def __init__(self, *replies: Reply) -> None:
        self._replies = list(replies)
        self.feeds: list[dict] = []
        self.images: list[list] = []

    async def step(self, feed: dict, scene=None, images=None) -> Reply:
        self.feeds.append(feed)
        self.images.append(list(images or []))
        return self._replies.pop(0) if self._replies else Reply()


class _Mouth:
    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail = fail_times

    async def send(self, name: str, text: str) -> None:
        if self._fail > 0:
            self._fail -= 1
            raise ConnectionError("广播通道抖了一下")
        self.sent.append((name, text))


class _Memory:
    def __init__(self, items: list[str] | None = None) -> None:
        self.items = list(items or [])
        self.queries: list[str] = []
        self.noted: list[tuple[str, float]] = []
        self.fail_note = False

    async def recall(self, query: str, limit: int = 3) -> list[str]:
        self.queries.append(query)
        return list(self.items)

    async def note(self, text: str, importance: float) -> None:
        if self.fail_note:
            raise OSError("磁盘满了")
        self.noted.append((text, importance))


def _say(text: str) -> Reply:
    return Reply(calls=(ToolCall(name="send_message", arguments={"text": text}),))


def _wait(ms: int) -> Reply:
    return Reply(calls=(ToolCall(name="wait", arguments={"ms": ms}),))


def _room(*texts: str) -> Room:
    room = Room("g1")
    for text in texts:
        room.append("message", "帕秋莉", text, user_id="u1")
    return room


def _quiet_scene(mouth: _Mouth, **wake) -> object:
    """按给定的唤醒策略造一个最小场景。"""

    class _Scene:
        _gensokyo_scene: ClassVar[dict] = {
            "name": "t_quiet",
            "prompt": "",
            "bubbles": {"max": 1, "gap_s": 0.0},
            "actions": ("say",),
            "wake": {"on_mention": 1.0, "on_others": 1.0, "cooldown_s": 0.0, **wake},
        }

        def __init__(self, ctx) -> None:
            self.ctx = ctx

        async def say(self, segments, reply_to=None) -> None:
            await self.ctx.mouth.send(self.ctx.name, segments[0]["text"])

    return _Scene(SceneContext(mouth=mouth, name="觉", room="g1", log=None))


# Turn 自己跑一次回合 ---------------------------------------------------------


async def test_turn_sends_what_model_says():
    """模型调 send_message -> 投递 + 入房"""
    room = _room("觉，在吗")
    mind, mouth = _ScriptedMind(_say("在。")), _Mouth()
    loop = Loop(room, mind, mouth)

    await loop.turn.run()

    assert mouth.sent == [("觉", "在。")]
    assert room.events[-1].kind == "reply" and room.events[-1].text == "在。"


async def test_multiple_messages_in_one_turn():
    """一轮里连调几次 send_message = 连发几条（这就是节奏的来源）"""
    room = _room("在吗")
    reply = Reply(
        calls=(
            ToolCall(name="send_message", arguments={"text": "在。"}),
            ToolCall(name="send_message", arguments={"text": "怎么了？"}),
        )
    )
    mouth = _Mouth()
    loop = Loop(room, _ScriptedMind(reply), mouth)

    await loop.turn.run()

    assert [text for _, text in mouth.sent] == ["在。", "怎么了？"]


async def test_no_calls_ends_turn_silently():
    """模型不调工具 = 这轮不动"""
    mouth = _Mouth()
    mind = _ScriptedMind(Reply(text="……"))
    loop = Loop(_room("随便说说"), mind, mouth)

    await loop.turn.run()

    assert mouth.sent == []
    assert len(mind.feeds) == 1


async def test_feed_shape():
    """投递的输入带 now / events / gap / memories"""
    memory = _Memory(["上次借走了书"])
    mind = _ScriptedMind(Reply())
    loop = Loop(_room("觉，我该看什么书？"), mind, _Mouth(), memory=memory)

    await loop.turn.run()

    feed = mind.feeds[0]
    assert set(feed) == {"now", "events", "gap", "history", "memories", "results"}
    assert feed["events"][0]["text"] == "觉，我该看什么书？"
    assert feed["gap"] is None
    assert feed["memories"] == ["上次借走了书"]
    assert memory.queries == ["觉，我该看什么书？"]


async def test_wait_stops_when_quiet():
    """模型只说 wait 且没人说话 -> 不再继续调模型"""
    mind = _ScriptedMind(_wait(10))
    loop = Loop(_room("嗯"), mind, _Mouth())

    await loop.turn.run()

    assert len(mind.feeds) == 1


async def test_wait_continues_when_message_arrives():
    """wait 期间来了新消息 -> 用新消息再调一次模型"""
    room = _room("在吗")
    mind = _ScriptedMind(_wait(5000), _say("来了。"))
    mouth = _Mouth()
    loop = Loop(room, mind, mouth)

    async def later():
        await asyncio.sleep(0.02)
        loop.notify("message", "帕秋莉", "喂？", user_id="u1")

    task = asyncio.create_task(later())
    await loop.turn.run()
    await task

    assert len(mind.feeds) == 2
    assert mind.feeds[1]["events"][0]["text"] == "喂？"
    assert mouth.sent == [("觉", "来了。")]


async def test_read_events_reveals_gap():
    """read_events 展开 gap，下一轮就能看见被折叠的消息"""
    room = Room("g1")
    for i in range(10):
        room.append("message", "甲", f"第{i}条")
    reply = Reply(
        calls=(
            ToolCall(
                name="read_events",
                arguments={"gap_id": 0, "side": "earliest", "limit": 3},
            ),
        )
    )
    mind = _ScriptedMind(reply, Reply())
    loop = Loop(room, mind, _Mouth(), window=2)

    await loop.turn.run()

    first, second = mind.feeds
    assert first["gap"]["gapId"] == 0 and first["gap"]["skipped"] == 8
    assert [event["text"] for event in second["events"]] == ["第0条", "第1条", "第2条"]
    assert second["gap"] == {"gapId": 1, "skipped": 5, "mentioned": False}


async def test_memory_note_uses_message_text():
    """记忆写的是用户原话，不是模型自己的输出"""
    memory = _Memory()
    loop = Loop(
        _room("觉，我该看什么书？"),
        _ScriptedMind(_say("随便挑一本。")),
        _Mouth(),
        memory=memory,
    )

    await loop.turn.run()

    text, importance = memory.noted[0]
    assert text.startswith("觉，我该看什么书？")
    assert "随便挑一本。" in text
    assert importance > 0


async def test_memory_failure_does_not_block_send():
    """记忆坏了只告警，消息照发"""
    memory = _Memory()
    memory.fail_note = True
    mouth = _Mouth()
    loop = Loop(_room("在吗"), _ScriptedMind(_say("在。")), mouth, memory=memory)

    await loop.turn.run()

    assert mouth.sent == [("觉", "在。")]


# Loop 调度层 ------------------------------------------------------------------


async def test_run_retries_after_failure():
    """模型第一次抛异常：退避重试同一批消息，不丢消息、不重复发送"""

    class _Flaky:
        name = "觉"
        prompt_cache = None

        def __init__(self) -> None:
            self.calls = 0

        async def step(self, feed, scene=None, images=None):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("后端断线")
            return _say("在。")

    room = _room("在吗")
    mind, mouth = _Flaky(), _Mouth()
    loop = Loop(room, mind, mouth, retry_backoff=0.01, retry_backoff_max=0.02)
    task = asyncio.create_task(loop.run())
    loop.notify("message", "帕秋莉", "喂？", user_id="u1")
    await asyncio.sleep(0.15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert mind.calls == 2
    assert mouth.sent == [("觉", "在。")], "重试不该把已经发出去的那条再发一遍"


async def test_feed_carries_recent_context(tmp_path):
    """每轮都给上下文：重启前聊过什么、自己刚说了什么都要看得到"""
    from gensokyoai.bgtasks import BgTasks
    from gensokyoai.room import RoomLog

    tasks = BgTasks()
    path = tmp_path / "events.jsonl"
    before = Room("g1", log=RoomLog(path, tasks))
    before.append("message", "帕秋莉", "上次说的那本书", user_id="u1")
    before.append("reply", "觉", "记着")
    await tasks.idle()

    room = Room("g1", log=RoomLog(path, tasks))
    room.append("message", "帕秋莉", "我又来了", user_id="u1")
    mind = _ScriptedMind(_say("在的。"), Reply())
    loop = Loop(room, mind, _Mouth())

    await loop.turn.run()
    room.append("message", "帕秋莉", "还在吗", user_id="u1")
    await loop.turn.run()

    assert [event["text"] for event in mind.feeds[0]["history"]] == [
        "上次说的那本书",
        "记着",
    ]
    assert [event["text"] for event in mind.feeds[0]["events"]] == ["我又来了"]
    tail = [event["text"] for event in mind.feeds[1]["history"]][-2:]
    assert tail == ["我又来了", "在的。"]
    assert "还在吗" not in [event["text"] for event in mind.feeds[1]["history"]]
    assert [event["text"] for event in mind.feeds[1]["events"]] == ["还在吗"]


async def test_same_line_is_not_repeated(tmp_path):
    """同一句在去重窗口里只发一次（模型抽风时的兜底）"""
    mouth = _Mouth()
    # Mind 会给两次 same line；Turn 在第二次该被 is_repeat 栏下
    reply = Reply(
        calls=(
            ToolCall(name="send_message", arguments={"text": "天人的效率总是惊人。"}),
            ToolCall(name="send_message", arguments={"text": "天人的效率总是惊人。"}),
            ToolCall(name="send_message", arguments={"text": "另一句。"}),
        )
    )
    # 需要场景允许最多 3 个气泡才能看出 dedup 真起作用
    mouth = _Mouth()
    scene = _quiet_scene(mouth)
    scene._gensokyo_scene["bubbles"] = {"max": 5, "gap_s": 0.0}
    loop = Loop(
        _room("在吗"), _ScriptedMind(reply), mouth, scene=scene, dedup_window=60.0
    )

    assert loop.is_repeat("天人的效率总是惊人。") is False
    await loop.turn.run()

    assert [text for _, text in mouth.sent] == ["天人的效率总是惊人。", "另一句。"]
    assert loop.is_repeat("天人的效率总是惊人。") is True
    assert loop.is_repeat("另一句。") is True


async def test_unmentioned_message_does_not_wake_quiet_scene():
    """没被点名 + 唤醒概率 0：消息入房，但不开一轮"""
    mouth = _Mouth()
    scene = _quiet_scene(mouth, on_others=0.0)
    loop = Loop(Room("g1"), _ScriptedMind(Reply()), mouth, scene=scene, rng=lambda: 0.0)

    loop.notify("message", "帕秋莉", "今天好热", user_id="u1")

    assert loop.room.unread == 1, "消息照样入房，上下文不丢"
    assert not loop.is_awake, "但没醒"


async def test_mentioned_message_always_wakes():
    """被点名：唤醒概率 1，一定醒"""
    mouth = _Mouth()
    scene = _quiet_scene(mouth, on_others=0.0)
    loop = Loop(
        Room("g1"), _ScriptedMind(Reply()), mouth, scene=scene, rng=lambda: 0.99
    )

    loop.notify("message", "帕秋莉", "觉，在吗", user_id="u1", mentioned=True)

    assert loop.is_awake


async def test_cooldown_blocks_unmentioned_after_speaking():
    """刚说过话的冷却期内，没被点名就不醒"""
    mouth = _Mouth()
    scene = _quiet_scene(mouth, on_others=1.0, cooldown_s=60.0)
    reply = Reply(
        calls=(ToolCall(name="send_message", arguments={"text": "嗯。"}),)
    )
    room = _room("觉，在吗")  # 先进房，别让 notify 抢戏
    loop = Loop(room, _ScriptedMind(reply), mouth, scene=scene, rng=lambda: 0.0)

    # 直接跑一回合让 Turn 说出"嗯。"，登记 _last_spoke
    # （不走 notify，那样会把 _wake 置位，干扰后面"该不醒"的断言）
    await loop.turn.run()
    assert mouth.sent == [("觉", "嗯。")], "前一句该说出口"

    loop.notify("message", "帕秋莉", "随便说说", user_id="u1")
    assert not loop.is_awake, "冷却期内没被点名就不该醒"

    loop.notify("message", "帕秋莉", "觉，你呢", user_id="u1", mentioned=True)
    assert loop.is_awake, "点名可以穿透冷却"


async def test_images_reach_the_model():
    """带图的消息：房间事件记了图，图片作为多模态附件给模型"""
    mind = _ScriptedMind(Reply())
    loop = Loop(Room("g1"), mind, _Mouth())

    loop.notify(
        "message",
        "帕秋莉",
        "看这张 [图片×1]",
        user_id="u1",
        images=("data:image/png;base64,AAAA",),
    )
    await loop.turn.run()

    assert mind.feeds[0]["events"][0]["images"] == 1
    assert mind.images[0] == [("帕秋莉 发的第 1 张图：", "data:image/png;base64,AAAA")]


async def test_prose_is_dropped_by_default():
    """没调工具、只写正文：默认丢掉（协议优先）"""
    mouth = _Mouth()
    mind = _ScriptedMind(Reply(text="我在。"))
    loop = Loop(Room("g1"), mind, mouth)

    loop.notify("message", "帕秋莉", "在吗", user_id="u1")
    await loop.turn.run()

    assert mouth.sent == []


async def test_prose_fallback_sends_when_scene_allows():
    """场景声明 on_prose=send：正文当作一句话发出去，别让用户干等"""
    mouth = _Mouth()
    scene = _quiet_scene(mouth)
    scene._gensokyo_scene = {**scene._gensokyo_scene, "on_prose": "send"}
    loop = Loop(Room("g1"), _ScriptedMind(Reply(text="绿色。")), mouth, scene=scene)

    loop.notify("message", "帕秋莉", "这图什么颜色", user_id="u1")
    await loop.turn.run()

    assert mouth.sent == [("觉", "绿色。")]
    assert loop.room.events[-1].kind == "reply"


# 文本抢救（公开 API） ---------------------------------------------------------


def test_salvage_extracts_payload_from_fake_tool_call():
    """模型把工具调用写成正文：抢救出真正要说的话"""
    cases = {
        "send_message": "",
        "send_message text 「thsss」的复数……是「thssss」啊。": (
            "「thsss」的复数……是「thssss」啊。"
        ),
        'send_message("在的。")': "在的。",
        '{"name": "send_message", "arguments": {"text": "在的。"}}': "在的。",
        '<tool_call>{"name": "send_message", "arguments": {"text": "在的。"}}'
        "</tool_call>": "在的。",
        "在的。": "在的。",
        "wait": "",
    }
    for raw, want in cases.items():
        assert salvage(raw) == want, raw


async def test_protocol_leak_is_never_sent():
    """写成工具调用格式的正文，宁可丢掉也不发到群里"""
    mouth = _Mouth()
    scene = _quiet_scene(mouth)
    scene._gensokyo_scene = {**scene._gensokyo_scene, "on_prose": "send"}
    loop = Loop(
        Room("g1"),
        _ScriptedMind(Reply(text="send_message")),
        mouth,
        scene=scene,
    )

    loop.notify("message", "帕秋莉", "在吗", user_id="u1")
    await loop.turn.run()

    assert mouth.sent == []


async def test_protocol_leak_is_salvaged_when_possible():
    """写得像工具调用但有真内容：抠出来照发"""
    mouth = _Mouth()
    scene = _quiet_scene(mouth)
    scene._gensokyo_scene = {**scene._gensokyo_scene, "on_prose": "send"}
    loop = Loop(
        Room("g1"),
        _ScriptedMind(Reply(text="send_message text 在的。")),
        mouth,
        scene=scene,
    )

    loop.notify("message", "帕秋莉", "在吗", user_id="u1")
    await loop.turn.run()

    assert mouth.sent == [("觉", "在的。")]


# 失败语义 ---------------------------------------------------------------------


def test_should_give_up_distinguishes_errors():
    """4xx 是请求本身有问题：放弃；5xx / 网络抖动：重试"""
    loop = Loop(Room("g1"), _ScriptedMind(), _Mouth())
    assert loop.should_give_up(ModelError(400, "", "http://x")) is True
    assert loop.should_give_up(ModelError(500, "", "http://x")) is False
    assert loop.should_give_up(ConnectionError("断网")) is False


async def test_permanent_error_turn_raises():
    """带图被拒：带图问一次、被 4xx 拒、去掉图再问一次仍被拒 -> 抛出来给 Loop 决定"""

    class _RejectAlways:
        prompt_cache = None
        name = "觉"

        async def step(self, feed, scene=None, images=None):
            raise ModelError(400, "invalid image", "http://x")

    room = Room("g1")
    room.append("message", "帕秋莉", "看这张", user_id="u1")
    loop = Loop(room, _RejectAlways(), _Mouth(), retry_backoff=0.01)

    with pytest.raises(ModelError):
        await loop.turn.run()


async def test_image_rejection_retries_without_images():
    """带图被端点拒（400）：去掉图再问一次，别让整轮失联"""
    seen = []

    class _Pickey:
        name = "觉"
        prompt_cache = None

        async def step(self, feed, scene=None, images=None):
            seen.append(bool(images))
            if images:
                raise ModelError(400, "image too large", "http://x")
            return _say("在。")

    room = Room("g1")
    room.append(
        "message",
        "帕秋莉",
        "看这张",
        user_id="u1",
        images=("data:image/png;base64,AA",),
    )
    mouth = _Mouth()
    loop = Loop(room, _Pickey(), mouth)

    await loop.turn.run()

    assert seen == [True, False], "先带图、再不带图"
    assert mouth.sent == [("觉", "在。")]


# Refresher 公开 API -----------------------------------------------------------


async def test_refresher_sync_only_advances_stable_prefix():
    """sync 只把已处理事件推进前缀，还在 gap 里没展开的不能进前缀"""
    cache_blocks: list[str] = []

    class _Cache:
        def add(self, line: str) -> None:
            cache_blocks.append(line)

        def stabilize(self) -> None:
            pass

        @property
        def history_blocks(self) -> list[str]:
            return cache_blocks

        @property
        def total_tokens(self) -> int:
            return 0

    class _CacheMind:
        name = "觉"
        prompt_cache = _Cache()

    room = _room("第一句")
    room.append("message", "帕秋莉", "第二句", user_id="u1")
    # take_feed(1) 取走最新的 1 条（"第二句"），更早的"第一句"折成 gap 仍是 pending
    room.take_feed(1)
    refresher = Refresher(room, _CacheMind())

    await refresher.sync()
    await refresher.sync()  # 幂等：第二次不该重复推

    assert cache_blocks == ["帕秋莉：第二句"], (
        "pending 的 gap 不能推；第二句已被取走，推一次就够"
    )


async def test_refresher_sync_replaces_without_compact_when_cache():
    """无缓存时 sync/maybe_compact 全是 no-op，不抛"""
    room = _room("嗯")
    refresher = Refresher(room, _ScriptedMind())
    await refresher.sync()
    assert await refresher.maybe_compact() == 0


# 房间落盘 / 媒体回放 ----------------------------------------------------------


async def test_images_survive_restart(tmp_path):
    """图片落盘：重启后回放出来的事件还带着图"""
    import base64

    from gensokyoai.bgtasks import BgTasks
    from gensokyoai.room import RoomLog

    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452") + b"\x00" * 32
    url = "data:image/png;base64," + base64.b64encode(png).decode()

    tasks = BgTasks()
    before = Room("g1", log=RoomLog(tmp_path / "events.jsonl", tasks))
    before.append("message", "帕秋莉", "看这张 [图片×1]", user_id="u1", images=(url,))
    await tasks.idle()

    room = Room("g1", log=RoomLog(tmp_path / "events.jsonl", tasks))

    assert room.events, "回放要有事件"
    assert room.events[-1].images == (url,), "图要能读回来"
    assert (tmp_path / "media").exists(), "图存在 media/ 下"
    assert "base64" not in (tmp_path / "events.jsonl").read_text(encoding="utf-8"), (
        "base64 不该进日志"
    )


async def test_broken_media_is_skipped(tmp_path):
    """media 下的文件丢了/坏了：事件照常回放，只是没图"""
    import base64

    from gensokyoai.bgtasks import BgTasks
    from gensokyoai.room import RoomLog

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    url = "data:image/png;base64," + base64.b64encode(png).decode()

    tasks = BgTasks()
    before = Room("g1", log=RoomLog(tmp_path / "events.jsonl", tasks))
    before.append("message", "帕秋莉", "看这张", user_id="u1", images=(url,))
    await tasks.idle()
    for stale in (tmp_path / "media").glob("*.img"):
        stale.write_bytes(b"not an image")

    room = Room("g1", log=RoomLog(tmp_path / "events.jsonl", tasks))

    assert room.events[-1].text == "看这张"
    assert room.events[-1].images == ()
