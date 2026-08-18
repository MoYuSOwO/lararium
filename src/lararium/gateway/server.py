"""HTTP 服务——M2 的组装根。

职责:把消息入队(立即返回 202)、读出件箱(长轮询)、报健康;模型调用只发生在
worker 里(DESIGN §9:入站线程不碰业务逻辑)。worker 由 lifespan 起 task,和本服务
在同一进程——asyncio.Event 不跨进程,这是 worker 能跑的前提。
"""

import asyncio
import contextlib
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from bundles.memory.server import build_memory_components, memory_tool_functions
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from lararium.config import Settings
from lararium.db import connect
from lararium.envelope import Envelope
from lararium.gateway.commands import handle_command
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.loop import Steward
from lararium.steward.model import PydanticAIClient
from lararium.steward.outbox import Outbox
from lararium.steward.registry import Registry
from lararium.steward.worker import Worker

logger = logging.getLogger("lararium")

# content 上限 16KB(协议契约)。缺字段/非 JSON → 400;超限 → 413。
MAX_CONTENT = 16 * 1024

_UNAUTHORIZED = JSONResponse({"error": "未授权"}, status_code=401)


def build_steward(settings: Settings, ledger: Any, gate: Any) -> Steward:
    """组装 Steward。这是全系统唯一允许 import bundles 的地方之一(组装根)。

    放这里而不是 cli.py:M2-6 之后 cli 降级为纯 HTTP 客户端、不再 import bundles,
    build_steward 若留在 cli 上会让那一步被迫动它;server 是和 cli 并列的组装根,
    放这里是它该在的位置。
    """
    conn = connect(settings.data_dir / "steward.sqlite")
    return Steward(
        settings=settings,
        inbox=Inbox(conn),
        journal=Journal(conn),
        registry=Registry.load(Path("bundles")),
        ledger=ledger,
        gate=gate,
        model=PydanticAIClient(settings),
        persona=Path("prompts/persona.md").read_text(encoding="utf-8"),
        outbox=Outbox(conn),
        # M1 进程内挂载;M2 容器化时换成 MCP 传输,工具定义不变
        bundle_tools=memory_tool_functions(gate),
    )


def create_app(
    *,
    steward: Steward,
    ledger: Any,
    gate: Any,
    tokens: dict[str, str],
    wake: asyncio.Event,
) -> Starlette:
    """纯组装,可测。tokens 是 {channel: token},token 决定渠道(协议契约)。

    ledger/gate 由 Memory bundle 提供,这里只调用它的少量方法(如
    gate.unsettled_count),形状归 bundle 管——组装根的适配接口,和 build_steward
    里的 ledger/gate 一样不定死类型。
    """

    def channel_for_token(token: str) -> str | None:
        for channel, expected in tokens.items():
            if hmac.compare_digest(expected, token):
                return channel
        return None

    def authenticate(request: Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return channel_for_token(auth[len("Bearer ") :].strip())

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # 启动:清上次崩溃遗留的 processing(否则串行队列永久卡死),再起 worker。
        requeued, abandoned = steward.inbox.recover_stale()
        if requeued or abandoned:
            logger.info("上次有未处理完的消息:%d 条已重新排队,%d 条已放弃", requeued, abandoned)
        worker = Worker(steward, wake)
        task = asyncio.create_task(worker.run())
        try:
            yield
        finally:
            # 退出:cancel worker + 最后一次结算(把已通过的提案落盘)。
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            settled = steward.settle_if_needed()
            if settled:
                logger.info("退出前结算 %d 条提案", settled)

    async def post_message(request: Request) -> JSONResponse:
        channel = authenticate(request)
        if channel is None:
            return _UNAUTHORIZED
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "body 必须是 JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "body 必须是 JSON 对象"}, status_code=400)
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            return JSONResponse({"error": "缺 content 字段"}, status_code=400)
        if len(content) > MAX_CONTENT:
            return JSONResponse({"error": "content 超出 16KB 上限"}, status_code=413)

        try:
            env = Envelope.new(
                source="user",
                channel=channel,
                content=content,
                meta={},
                id=payload.get("id"),  # None → 服务端生成;给了就构造时带上,让信封自己把关
            )
        except ValidationError:
            # 畸形 id(非字符串/非 hex/超长)是客户端问题 → 400,不能把网络面打成 500。
            return JSONResponse({"error": "id 必须是 32 位 hex"}, status_code=400)

        duplicate = False
        if payload.get("id"):
            # 客户端给 id → 幂等:重发同 id 只处理一次(INSERT OR IGNORE 靠主键)。
            duplicate = not steward.inbox.put_idempotent(env)
        else:
            steward.inbox.put(env)
        wake.set()  # 唤醒 worker,别让它抱着空队列睡到超时才 poll 到新活
        return JSONResponse({"envelope_id": env.id, "duplicate": duplicate}, status_code=202)

    async def get_outbox(request: Request) -> JSONResponse:
        channel = authenticate(request)
        if channel is None:
            return _UNAUTHORIZED
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError:
            after = 0
        try:
            wait = min(max(int(request.query_params.get("wait", "0")), 0), 30)
        except ValueError:
            wait = 0

        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        # 长轮询:有货即返;无货且 wait>0 则等(轮询即可,单用户不值得按渠道分事件)。
        # at-least-once:同一条可能重复出现,客户端按 seq 去重。
        while True:
            items = steward.outbox.take(channel, after=after)
            if items or loop.time() >= deadline:
                break
            await asyncio.sleep(min(0.2, max(0.0, deadline - loop.time())))
        return JSONResponse(
            {
                "items": [
                    {
                        "seq": i.seq,
                        "envelope_id": i.envelope_id,
                        "kind": i.kind,
                        "content": i.content,
                        "created_at": i.created_at,
                    }
                    for i in items
                ]
            },
            status_code=200,
        )

    async def get_health(request: Request) -> JSONResponse:
        channel = authenticate(request)
        if channel is None:
            return _UNAUTHORIZED
        return JSONResponse(
            {"pending": steward.inbox.pending_count(), "unsettled": gate.unsettled_count()},
            status_code=200,
        )

    async def post_command(request: Request) -> JSONResponse:
        """命令端点——**这个端点从此就是门控的开关(D12)**。

        /approve /settle /rollback 都从这里直通 Gate,不经模型。M5 做 python_sandbox
        时,"沙箱无网络"就是防它被**模型自己 POST** 到这里的墙——两条约束是绑定的,
        谁也不许单独放松:沙箱一旦联网,被注入的模型就能自己 /approve 把恶意事实
        永久写进账本。
        """
        channel = authenticate(request)
        if channel is None:
            return _UNAUTHORIZED
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "body 必须是 JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "body 必须是 JSON 对象"}, status_code=400)
        line = payload.get("line")
        if not isinstance(line, str) or not line.strip():
            return JSONResponse({"error": "缺 line 字段"}, status_code=400)

        result = handle_command(line, steward=steward, ledger=ledger, gate=gate)
        text = (
            # /quit 在 HTTP 语境无退出概念:结算已在 handle_command 里执行(副作用),
            # 只把 should_quit 翻译成一句提示,服务不退。
            "服务端无退出概念,请直接关客户端。" if result.should_quit else result.text
        )
        return JSONResponse({"text": text}, status_code=200)

    routes = [
        Route("/v1/messages", endpoint=post_message, methods=["POST"]),
        Route("/v1/outbox", endpoint=get_outbox, methods=["GET"]),
        Route("/v1/health", endpoint=get_health, methods=["GET"]),
        Route("/v1/commands", endpoint=post_command, methods=["POST"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings.load()
    # 服务端是新的组装根:自己和 cli 一样组装内存 bundle 与 Steward(build_steward 就在本模块)。
    ledger, gate = build_memory_components(settings.data_dir)
    steward = build_steward(settings, ledger, gate)
    wake = asyncio.Event()
    app = create_app(
        steward=steward,
        ledger=ledger,
        gate=gate,
        tokens=settings.tokens,
        wake=wake,
    )
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
