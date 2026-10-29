"""Refresher —— 前缀缓存的同步与压缩。

跟"说没说话"无关，只在回合成功之后被 Loop 调用：把稳定历史推进前缀、
接近上下文窗口时调便宜端点压成概要。失败不致命，下一轮再来。
"""

from ..health import HealthCenter
from ..mind import Mind
from ..room import Room
from ..utils.logger import LoggerManager

_logger = LoggerManager.get_logger("REFRESHER")

_COMPACT_PROMPT = (
    "把下面这段群聊记录压成一段概要，保留人名、约定、事实与情感转折，"
    "丢掉寒暄和重复。直接输出概要，不要解释。"
)

_COMPACT_KEEP = 8
""" 压缩时留几条最近的历史不压（模型要靠它们接话） """


class Refresher:
    """前缀缓存的管家（Room × Mind 配对）。"""

    def __init__(
        self,
        room: Room,
        mind: Mind,
        health: HealthCenter | None = None,
        *,
        window: int = 30,
        compact_ratio: float = 0.75,
        compact_keep: int = _COMPACT_KEEP,
    ) -> None:
        """组装。

        Args:
            room: 事件流
            mind: 用它的 cheap_model 做压缩、用它的 prompt_cache 当作缓存本体
            health: 健康中心（None = 不记账）
            window: 第一轮只补最近这么多条回放历史
            compact_ratio: 用到上下文窗口的多少比例就触发压缩（1.0 = 塞满才压）
            compact_keep: 压缩时保留最近几条不压
        """
        self.room = room
        self._mind = mind
        self._health = health
        self._window = max(0, window)
        self._compact_ratio = min(1.0, max(0.1, compact_ratio))
        self._compact_keep = max(0, compact_keep)
        self._cached_ids: set[int] = set()
        self._cache_started = False

    @property
    def _cache(self):
        """前缀缓存本体（None = 关着，一切 no-op）。"""
        return self._mind.prompt_cache

    async def sync(self) -> None:
        """把已处理事件推入前缀；未读与未展开 gap 留到以后。

        第一轮只补最近一窗回放历史。编号集合而不是高水位允许模型先看到
        早到的消息、后看到等待期间的新消息，同时仍保证每条只缓存一次。
        """
        cache = self._cache
        if cache is None:
            return
        events = list(self.room.events)
        self._cached_ids.intersection_update(event.msg_id for event in events)
        pending = self.room.pending_ids()
        eligible = [
            event
            for event in events
            if event.msg_id not in pending and event.msg_id not in self._cached_ids
        ]
        if not eligible:
            return
        if not self._cache_started:
            fresh = eligible[-self._window :] if self._window > 0 else eligible
            self._cache_started = True
        else:
            fresh = eligible
        self._cached_ids.update(event.msg_id for event in eligible)
        for event in fresh:
            cache.add(f"{event.sender}：{event.text}")
        cache.stabilize()

    async def compact(self, keep: int | None = None) -> int:
        """把旧历史压成摘要，只动 `_fixed_msg`（系统块照旧命中）。

        Args:
            keep: 保留最近几条不压（None = 用构造时给的）

        Returns:
            int: 被压掉的条数（0 = 没压，条数不够或模型没答）
        """
        cache = self._cache
        if cache is None:
            return 0
        keep = self._compact_keep if keep is None else keep
        blocks = list(cache.history_blocks)
        if len(blocks) <= keep:
            return 0
        old = blocks[:-keep] if keep else blocks
        summary = ""
        try:
            model = self._mind.cheap_model
            reply = await model.chat(
                _COMPACT_PROMPT,
                "\n".join(old),
                max_tokens=500,
                temperature=0.3,
            )
            summary = f"（往事概要）{reply.text.strip()}"
        except Exception as err:  # noqa: BLE001 - 压缩失败不该影响对话
            _logger.warning(f"{self.room.name} 前缀压缩失败，直接丢弃旧历史：{err}")
        dropped = cache.compact(summary, keep=keep)
        if dropped and self._health is not None:
            self._health.count(f"{self.room.name}.prompt_compacts")
        return dropped

    async def maybe_compact(self) -> int:
        """接近上下文窗口就压一次（否则什么都不做）。

        Returns:
            int: 被压掉的条数（0 = 还没到阈值）
        """
        cache = self._cache
        if cache is None:
            return 0
        context = self._mind.model.context
        if context <= 0:
            return 0
        if cache.total_tokens < context * self._compact_ratio:
            return 0
        return await self.compact()
