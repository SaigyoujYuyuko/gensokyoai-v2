"""Loop —— 一个房间的运转调度。

只做调度：监听唤醒、按退避重试 `Turn`、成功后让 `Refresher` 推进前缀。
回合本身（取未读/问模型/执行动作）在 `Turn`；前缀压缩在 `Refresher`。
"""

import asyncio
import random
import time
from collections import deque
from collections.abc import Callable

from ..health import HealthCenter
from ..llm.base import ModelError
from ..memory import Memory
from ..mind import Mind
from ..room import EventKind, Room
from ..utils.logger import LoggerManager
from .refresher import Refresher
from .text import chance
from .turn import Turn

_logger = LoggerManager.get_logger("LOOP")

_BACKOFF_S = 1.0
""" 首次失败后的重试间隔（秒） """

_BACKOFF_MAX_S = 30.0
""" 退避上限（秒） """

_WINDOW = 30
""" 一次投递多少条未读 """

_MAX_STEPS = 4
""" 一轮里最多几次模型调用（read_events 会再来一次） """

_MAX_ATTEMPTS = 3
""" 同一批消息最多试几次（4xx 直接放弃，不纠缠） """

_DEDUP_WINDOW_S = 20.0
""" 同一句话在这个窗口内不重复发（秒） """


class Loop:
    """一个房间的调度器：监听唤醒、重试、推进缓存。

    所有公共入口跟旧版一致（`notify` / `run` / `room` / `mind` / `memory`）。
    """

    def __init__(
        self,
        room: Room,
        mind: Mind,
        mouth,
        memory: Memory | None = None,
        health: HealthCenter | None = None,
        *,
        scene=None,
        window: int = _WINDOW,
        max_steps: int = _MAX_STEPS,
        max_attempts: int = _MAX_ATTEMPTS,
        compact_ratio: float = 0.75,
        compact_keep: int = 8,
        retry_backoff: float = _BACKOFF_S,
        retry_backoff_max: float = _BACKOFF_MAX_S,
        bubble_gap: float | None = None,
        dedup_window: float = _DEDUP_WINDOW_S,
        rng: Callable[[], float] = random.random,
    ) -> None:
        """组装。

        参数跟旧版一致；`compact_ratio`/`compact_keep` 前进给 `Refresher`，
        `window`/`max_steps`/`bubble_gap`/`dedup_window` 前进给 `Turn`。
        """
        self.room = room
        self.mind = mind
        self.mouth = mouth
        self.memory = memory
        self._health = health
        self._scene = scene

        self._wake = asyncio.Event()
        self._retry_backoff = max(0.0, retry_backoff)
        self._retry_backoff_max = max(self._retry_backoff, retry_backoff_max)
        self._backoff = self._retry_backoff
        self._attempts = 0
        self._max_attempts = max(1, max_attempts)
        self._failed_ids: set[int] = set()
        self._last_spoke = 0.0
        self._said: deque[tuple[float, str]] = deque()
        self._dedup_window = max(0.0, dedup_window)
        self._rng = rng

        from ..apis import spec_of

        wake = spec_of(scene)["wake"] if scene is not None else {
            "on_mention": 1.0,
            "on_others": 1.0,
            "cooldown_s": 0.0,
        }
        self._wake_on_mention = chance(wake.get("on_mention", 1.0))
        self._wake_on_others = chance(wake.get("on_others", 1.0))
        self._wake_cooldown = max(0.0, float(wake.get("cooldown_s", 0.0)))

        # 组装 Turn 与 Refresher
        self._refresher = Refresher(
            room,
            mind,
            health,
            window=window,
            compact_ratio=compact_ratio,
            compact_keep=compact_keep,
        )
        self._turn = Turn(
            room,
            mind,
            mouth,
            memory,
            health,
            scene=scene,
            window=window,
            max_steps=max_steps,
            bubble_gap=bubble_gap,
            dedup_window=self._dedup_window,
            last_spoke=lambda: self._last_spoke,
            record_spoke=self._record_spoke,
            is_repeat=self.is_repeat,
            sleep_for_event=self._sleep_for_event,
        )

    # 对外入口 ----------------------------------------------------------------

    def notify(
        self,
        kind: str,
        sender: str,
        text: str,
        *,
        user_id: str = "",
        mentioned: bool = False,
        images: tuple[str, ...] = (),
    ) -> None:
        """消息入房，按场景的唤醒策略决定开不开一轮。

        空文本且无图不入房（房间那边已经过滤，这里是双保险）。
        """
        self.room.append(
            _kind(kind),
            sender,
            text,
            user_id=user_id,
            mentioned=mentioned,
            images=images,
        )
        woke, reason = self._decide_wake(mentioned)
        _logger.debug(f"{self.room.name} {'唤醒' if woke else '不唤醒'}（{reason}）")
        if woke:
            self._wake.set()

    async def run(self) -> None:
        """常驻循环（被取消退出；单轮失败退避重试）。"""
        while True:
            await self._wake.wait()
            self._wake.clear()
            while True:
                started = time.monotonic()
                try:
                    await self._turn.run()
                    self._failed_ids.clear()
                    await self._refresher.sync()
                    await self._refresher.maybe_compact()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - 单轮任何失败都不该让房间死掉
                    if self._health is not None:
                        self._health.count(f"{self.room.name}.turn_errors")
                    self._attempts += 1
                    if not self._failed_ids:
                        # 只在首次失败时锁定原批次；退避期间到达的新消息不该受放弃连坐
                        self._failed_ids = set(self._turn.recovered_ids)
                    if self.should_give_up(err):
                        _logger.error(
                            f"{self.room.name} 放弃这一批"
                            f"（第 {self._attempts} 次）：{err}"
                        )
                        self._attempts = 0
                        self.room.discard(self._failed_ids)
                        self._failed_ids.clear()
                        break
                    _logger.warning(
                        f"{self.room.name} 回合失败，{self._backoff:.1f}s 后重试"
                        f"（{err}）"
                    )
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, self._retry_backoff_max)
                    continue
                self._backoff = self._retry_backoff
                self._attempts = 0
                self._failed_ids.clear()
                if self._health is not None:
                    self._health.observe(
                        f"{self.room.name}.turn_s", time.monotonic() - started
                    )
                break

    # 失败与重试 ---------------------------------------------------------------

    def should_give_up(self, err: Exception) -> bool:
        """这一批要不要放弃：请求本身有问题（4xx）重试没用，重试次数到顶也不再纠缠。"""
        if isinstance(err, ModelError) and not err.retryable:
            return True
        return self._attempts >= self._max_attempts

    # 公开属性 -----------------------------------------------------------------

    @property
    def turn(self) -> Turn:
        """回合执行器（只跑一次的"模型判定"）。"""
        return self._turn

    @property
    def refresher(self) -> Refresher:
        """前缀缓存管家。"""
        return self._refresher

    @property
    def is_awake(self) -> bool:
        """下一次 `run()` 会不会立刻进入一轮（被 notify 标记过了）。"""
        return self._wake.is_set()

    @property
    def scene(self):
        """装在 Loop 上的场景实例（None = 裸会话）。"""
        return self._scene

    # 唤醒策略 ----------------------------------------------------------------

    def _decide_wake(self, mentioned: bool) -> tuple[bool, str]:
        """要不要因为这条消息开一轮，以及理由（日志里要说清是哪种原因）。"""
        if mentioned:
            return True, "点名"
        if self._wake_cooldown > 0:
            gap = time.monotonic() - self._last_spoke
            if gap < self._wake_cooldown:
                return False, f"冷却中（还差 {self._wake_cooldown - gap:.0f}s）"
        if self._rng() < self._wake_on_others:
            return True, f"概率命中 {self._wake_on_others:g}"
        return False, f"概率没中 {self._wake_on_others:g}"

    # Turn 调用的回调 ---------------------------------------------------------

    def _record_spoke(self, text: str) -> None:
        """`Turn._say` 在真正出口之前先调它：更时间戳 + dedup 登记。"""
        self._last_spoke = time.monotonic()
        if self._dedup_window > 0:
            self._said.append((self._last_spoke, text))
            now = self._last_spoke
            while self._said and now - self._said[0][0] > self._dedup_window:
                self._said.popleft()

    def is_repeat(self, text: str) -> bool:
        """这句话是不是刚说过（窗口内逐字相同）。给 Turn 当拒发依据。"""
        if self._dedup_window <= 0:
            return False
        now = time.monotonic()
        while self._said and now - self._said[0][0] > self._dedup_window:
            self._said.popleft()
        return any(previous == text for _, previous in self._said)

    async def _sleep_for_event(self, ms: int):
        """`Turn` 想等下一批消息或超时，借 Loop 的 wake 事件。"""
        if ms <= 0:
            return self.room.unread > 0
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=ms / 1000)
        except TimeoutError:
            return False
        self._wake.clear()
        return True


def _kind(kind: str) -> EventKind:
    """把字符串收成事件类型（只认 message / reply）。"""
    return "reply" if kind == "reply" else "message"
