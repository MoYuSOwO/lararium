import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from lararium.steward.loop import Steward

logger = logging.getLogger("lararium")


class Worker:
    """唯一的队列消费者。有活逐条干,没活歇着——严格串行的延续(D11)。

    asyncio.Event 不跨进程:这套的前提是 HTTP 服务和 worker 在**同一进程**
    (M2-4 的 lifespan 里起 task)。跨进程时 wake 得换成别的唤醒机制。
    """

    # 退避上限:指数退避 2**attempts,封顶 60 秒。
    MAX_BACKOFF = 60.0

    def __init__(
        self,
        steward: Steward,
        wake: asyncio.Event,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._steward = steward
        # wake 公开:server(lifespan)和新消息入队端点都要能唤醒它。
        self.wake = wake
        # 可注入的 sleep:退避时长测试用假 sleep 验证,不注入就用真 asyncio.sleep。
        self._sleep = sleep or asyncio.sleep

    async def run(self) -> None:
        busy = False
        while True:
            try:
                outcome = await self._steward.process_next()
            except Exception:
                # 毒消息已被 loop 标记 failed,别让 worker 陪葬——记日志继续下一条。
                logger.exception("worker: 本条消息处理失败,继续下一条")
                busy = True
                continue
            if outcome.kind == "replied":
                # 本轮消费了一个信封走到终态(成功回复或放弃)——队列可能还有活。
                busy = True
                continue
            if outcome.kind == "retry_later":
                # 可重试失败:指数退避后再认领同一条,绝不等 wake。
                # 若把它当"空"去等 wake,任何新消息 wake.set() 都会立刻重锤
                # 那条被限流的消息——流量越大敲得越狠,退避形同虚设。
                busy = True
                await self._sleep(min(2.0**outcome.attempts, self.MAX_BACKOFF))
                continue
            # kind == "empty":收件箱空了
            if busy:
                # 空闲结算:队列排空、没人对话的时刻重建前缀,最不疼(D11)。
                settled = self._steward.settle_if_needed()
                if settled:
                    logger.info("空闲结算 %d 条提案", settled)
                busy = False
            self.wake.clear()
            # 兜底防丢唤醒:就算 clear 与 wake.set() 赛跑丢了唤醒,5 秒后也会再 poll。
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=5)
