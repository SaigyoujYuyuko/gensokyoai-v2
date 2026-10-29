"""Turn —— 一次回合：取未读 → 问模型 → 执行 call → 可能再来一轮。

只关心事件怎么变成动作并执行；不知道循环、唤醒、退避这一层。
任何失败都抛给上层，由 Loop 决定重试还是放弃；自己先把已取的事件退回去。
"""

from collections.abc import Callable

from ..apis import call_tool, spec_of
from ..health import HealthCenter
from ..llm.base import ModelError
from ..memory import Memory
from ..mind import Mind
from ..room import Event, Room
from ..utils.logger import LoggerManager
from .text import (
    images_of,
    last_message,
    salvage,
    segments_of,
    text_of,
    to_int,
)

_logger = LoggerManager.get_logger("TURN")

_IMPORTANCE = 0.6
""" 记忆写入强度 """


class SceneContext:
    """库交给场景实例的东西（每个房间一份）。

    保留在 engine 里是因为它与 `Turn` 同生共死；协议声明在 `apis.SceneContext`。
    """

    def __init__(self, mouth, name: str, room: str, log) -> None:
        self.mouth = mouth
        self.name = name
        self.room = room
        self.log = log


class Turn:
    """一个房间的回合执行器（与重试策略无关）。"""

    def __init__(
        self,
        room: Room,
        mind: Mind,
        mouth,
        memory: Memory | None = None,
        health: HealthCenter | None = None,
        *,
        scene=None,
        window: int = 30,
        max_steps: int = 4,
        bubble_gap: float | None = None,
        dedup_window: float = 20.0,
        prose_policy: str | None = None,
        last_spoke: Callable[[], float],
        record_spoke: Callable[[str], None],
        is_repeat: Callable[[str], bool] | None = None,
        sleep_for_event: Callable[[int], "object"] | None = None,
    ) -> None:
        """组装一个回合执行器。

        Args:
            room: 事件流
            mind: 决定动作的模型端
            mouth: 场景默认出口
            memory: 记忆（None = 不记不召回）
            health: 健康中心（None = 不记账）
            scene: 场景实例（None = 裸会话，启用全部内核动作）
            window: 一轮最多投递多少条未读
            max_steps: 一轮里最多几次模型调用（read_events 会再来一次）
            bubble_gap: 连发间隔秒（None = 用场景声明的）
            dedup_window: 同一句话多少秒内不重复发（0 = 不拦）
            prose_policy: 模型只写正文没调工具怎么办（None = 取场景声明）
            last_spoke: 取"上次开口"的 monotonic 时间（读它判冷却）
            record_spoke: 出口一条之后调它（更新 spoke 时间与 dedup 窗口）
            sleep_for_event: 由 Loop 提供的"等下一批或超时"
        """
        self.room = room
        self.mind = mind
        self.mouth = mouth
        self.memory = memory
        self._health = health
        self._scene = scene
        # 裸会话也有合理的连发默认（模型想多说几句别拦一句就完）
        bubbles = (
            spec_of(scene)["bubbles"]
            if scene is not None
            else {"max": 3, "gap_s": 0.0}
        )
        self._max_bubbles = max(1, int(bubbles.get("max", 1)))
        self._bubble_gap = (
            max(0.0, float(bubbles.get("gap_s", 0.0)))
            if bubble_gap is None
            else max(0.0, bubble_gap)
        )
        self._window = max(0, window)
        self._max_steps = max(1, max_steps)
        self._dedup_window = max(0.0, dedup_window)
        self._prose_policy = (
            spec_of(scene).get("on_prose", "drop")
            if prose_policy is None and scene is not None
            else (prose_policy or "drop")
        )
        self._last_spoke = last_spoke  # 现在只读；真正驱动去重的登记在 _record_spoke
        self._record_spoke = record_spoke
        self._is_repeat = is_repeat
        self._sleep_for_event = sleep_for_event
        self._revealed: list[Event] = []
        self._revealed_gap: dict | None = None
        self._results: list[dict] = []

    async def run(self) -> None:
        """一个回合：投递未读 → 模型选动作 → 照做 → 可能要再来一次。

        任何异常都会先把已取出的未读与 gap 退回房间，再向上抛。
        """
        shown, gap = self.room.take_feed(self.window, self._revealed)
        self._revealed = []
        taken = {event.msg_id: event for event in shown if event.kind == "message"}
        gap_ids = {gap["gapId"]} if gap is not None else set()
        recovered: set[int] = set()
        try:
            for _ in range(self._max_steps):
                if not shown:
                    return
                reply = await self._step(shown, gap)
                if not reply.calls:
                    await self._on_prose(reply.text)
                    return
                waited = None
                said = 0
                for call in reply.calls:
                    if call.name == "send_message":
                        if said >= self._max_bubbles:
                            continue
                        if said and self._bubble_gap > 0:
                            import asyncio

                            await asyncio.sleep(self._bubble_gap)
                        await self._say(segments_of(call.arguments))
                        said += 1
                    elif call.name == "read_events":
                        self._handle_read_events(call, taken, gap_ids)
                    elif call.name == "wait":
                        waited = max(waited or 0, to_int(call.arguments.get("ms"), 0))
                    else:
                        self._results.extend(
                            await call_tool(self._scene, call.name, call.arguments)
                        )
                if self._revealed:
                    shown, gap = self._revealed, self._revealed_gap
                    self._revealed, self._revealed_gap = [], None
                    continue
                if self._results:
                    continue  # 工具结果回填，同一批事件再看一遍
                if waited is None:
                    return
                if not await self._sleep_for_event(waited):
                    return
                shown, gap = self.room.take_feed(self._window)
                taken.update(
                    (event.msg_id, event) for event in shown if event.kind == "message"
                )
                if gap is not None:
                    gap_ids.add(gap["gapId"])
        except BaseException:
            recovered = self.room.push_back(list(taken.values()), gap_ids=gap_ids)
            self._recovered_ids = recovered
            self._revealed, self._revealed_gap = [], None
            raise

    @property
    def window(self) -> int:
        """一轮最多投递多少条未读。"""
        return self._window

    @property
    def recovered_ids(self) -> set[int]:
        """上次失败时退回给房间的事件编号（给 Loop 作放弃依据）。"""
        return getattr(self, "_recovered_ids", set())

    def _handle_read_events(self, call, taken: dict, gap_ids: set) -> None:
        """展开一段 gap，更新"本轮已取事件"和"还在房里的 gap"账本。"""
        gap_id = to_int(call.arguments.get("gap_id"), -1)
        self._revealed, self._revealed_gap = self.room.reveal(
            gap_id,
            side=str(call.arguments.get("side") or "latest"),
            limit=to_int(call.arguments.get("limit"), 0),
        )
        gap_ids.add(gap_id)
        taken.update(
            (event.msg_id, event)
            for event in self._revealed
            if event.kind == "message"
        )
        if self._revealed_gap is not None:
            gap_ids.add(self._revealed_gap["gapId"])

    async def _step(self, shown: list[Event], gap: dict | None):
        """问模型一次；带图被端点拒了就去掉图再问一次（图不该让整轮失联）。"""
        images = images_of(shown)
        payload = await self._feed(shown, gap)
        try:
            return await self.mind.step(payload, self._scene, images=images)
        except ModelError as err:
            if not images or err.retryable:
                raise
            _logger.warning(
                f"{self.room.name} 带图请求被拒（HTTP {err.status}），去掉图重试："
                f"{err.body[:160]}"
            )
            return await self.mind.step(payload, self._scene, images=None)

    async def _feed(self, shown: list[Event], gap: dict | None) -> dict:
        """组出给模型的 JSON（事件 / gap / 召回 / 工具结果）。"""
        events = (
            [self._scene.describe(event.wire()) for event in shown]
            if hasattr(self._scene, "describe")
            else [event.wire() for event in shown]
        )
        results, self._results = self._results, []
        feed = {
            "now": _now(),
            "events": events,
            "gap": gap,
            "memories": await self._recall(shown),
            "results": results,
        }
        if self.mind.prompt_cache is None:
            # 没有前缀缓存时，history 仍走 JSON（模型需要它才接得上话）
            feed["history"] = [event.wire() for event in self.room.context(shown)]
        return feed

    async def _on_prose(self, text: str) -> None:
        """模型没调工具、只写了正文。

        常见的两种情况：真写了句话（带图时尤其容易），或者把工具调用写成了
        正文——`send_message text ...`、`{"name": "send_message", ...}` 这种。
        后者先从正文里抢救出真正要说的话，抢救不出来就当它什么也没说。
        """
        raw = text.strip()
        if not raw:
            return
        salvaged = salvage(raw)
        if not salvaged:
            _logger.warning(
                f"{self.room.name} 模型写成了工具调用格式却没调工具，已丢弃: "
                f"{raw[:40]!r}"
            )
            return
        if self._prose_policy != "send":
            _logger.warning(
                f"{self.room.name} 模型只写了正文、没调工具（协议要求工具调用，"
                f"已丢弃）: {salvaged[:40]!r}"
            )
            return
        _logger.warning(
            f"{self.room.name} 模型只写了正文、没调工具，按场景策略直接发出"
            + ("（已从工具调用格式里抢救）" if salvaged != raw else "")
        )
        await self._say([{"type": "text", "text": salvaged}])

    async def _say(self, segments: list[dict], reply_to: int | None = None) -> None:
        """经场景出口投递，入房，记一笔。

        裸会话（没装场景）时直接经 mouth 出口。
        """
        text = text_of(segments)
        if not text:
            return
        if self._scene is not None and hasattr(self._scene, "clean"):
            text = self._scene.clean(text) or text
        if self._is_repeat is not None and self._is_repeat(text):
            _logger.debug(f"{self.room.name} 同一句刚说过，丢弃: {text[:30]!r}")
            return
        if self._scene is None:
            await self.mouth.send(self.mind.name, text)
        else:
            await self._scene.say(segments, reply_to)
        self._record_spoke(text)  # 出口之后再登记，下一次想重复就能拦
        self.room.append("reply", self.mind.name, text)
        if self._health is not None:
            self._health.count(f"{self.room.name}.says")
        await self._remember(text)

    async def _remember(self, text: str) -> None:
        """记忆是旁路：写失败只告警，不重试（否则发言会重播）。"""
        if self.memory is None:
            return
        trigger = last_message(self.room.events)
        try:
            await self.memory.note(f"{trigger} / {self.mind.name}: {text}", _IMPORTANCE)
        except Exception:
            _logger.exception(f"{self.room.name} 记忆写入失败")

    async def _recall(self, shown: list[Event]) -> list[str]:
        """按这一轮最后一条消息召回；失败当没想起。"""
        if self.memory is None:
            return []
        query = last_message(shown)
        if not query:
            return []
        try:
            return await self.memory.recall(query)
        except Exception:
            _logger.exception(f"{self.room.name} 记忆召回失败")
            return []


def _now() -> str:
    """当前时间的可读字符串（给模型看的 now）。"""
    import time  # 人类hint: 不建议函数内导入，移动后删掉这个注释

    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
