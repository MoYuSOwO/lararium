"""HTTP 服务——M2 的组装根。

职责:把消息入队(立即返回 202)、读出件箱(长轮询)、报健康;模型调用只发生在
worker 里(DESIGN §9:入站线程不碰业务逻辑)。worker 由 lifespan 起 task,和本服务
在同一进程——asyncio.Event 不跨进程,这是 worker 能跑的前提。
"""

import asyncio
import contextlib
import hmac
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import uvicorn
from bundles.courses.server import build as build_courses
from bundles.finance.server import build as build_finance
from bundles.memory.server import build_memory_components, memory_tool_functions
from bundles.recipes.server import build as build_recipes
from bundles.todos.server import build as build_todos
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from lararium.config import Settings
from lararium.db import connect
from lararium.envelope import MAX_ATTACHMENTS, Attachment, Envelope
from lararium.gateway.commands import handle_command
from lararium.persona import (
    assemble_persona,
    prefix_digest,
    record_prefix_change,
    tool_schema_fingerprint,
)
from lararium.steward.assembler import render_system_prompt
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.loop import Steward
from lararium.steward.model import PydanticAIClient
from lararium.steward.outbox import Outbox
from lararium.steward.pdftext import PdfText
from lararium.steward.registry import Registry
from lararium.steward.threads import Threads
from lararium.steward.transcribe import Transcriber, load_page_prompt
from lararium.steward.worker import Worker

logger = logging.getLogger("lararium")

# content 上限 16KB(协议契约)。缺字段/非 JSON → 400;超限 → 413。
MAX_CONTENT = 16 * 1024

_UNAUTHORIZED = JSONResponse({"error": "未授权"}, status_code=401)
_FORBIDDEN = JSONResponse({"error": "无权限"}, status_code=403)


@dataclass(frozen=True)
class AssembledTools:
    """组装根拼好的 bundle 工具,外加 P0-1 守卫要认的那一个。

    `proposal_tool` 是 memory 交出来的**原函数对象**(没加前缀的那个)。Steward 顺着
    `__wrapped__` 链按身份找它,不按名字——名字是注册那一刻才加的,以后还可能再变(M6-6d)。
    """

    tools: list[Callable]
    proposal_tool: Callable


def _assemble_bundle_tools(
    data_dir: Path, gate: Any, *, timezone: str, registry: Registry
) -> AssembledTools:
    """组装根的显式小表:加一个领域 bundle,在这里加一行。

    memory 是特殊 bundle(§6.1,ledger/gate 走 Steward 的 ports,不试图抹平),
    工具**仍排最前**;领域 bundle 走统一构造入口 `build(data_dir) -> BundleRuntime`,
    追加在后。顺序即工具 schema(前缀第0层),`test_bundle_tool_order_*` 把它钉死——
    一旦定了不许再动,免得哪天有人把 finance 插到 memory 前面还自认为是排序优化。

    M6-6d:每一段都过 `registry.qualify_tools`,**前缀取自那个 bundle 的 manifest**。
    这里写的 "finance" 只是去找 manifest 的钥匙,找到之后要和 build() 交出来的函数名逐个对账,
    写错成别人的名字当场炸,贴不上错的前缀。
    """
    memory = memory_tool_functions(gate)
    tools = registry.qualify_tools("memory", memory)
    # 每加一个领域,加一行
    tools += registry.qualify_tools("finance", build_finance(data_dir, timezone=timezone).tools)
    # M6-5 做菜:**追加在末尾**,finance 那一段一格没动。新 bundle = 目录行 + 8 个工具
    # schema,前缀重建一次(A1,启动时 prefix_log 会记);插到中间则是**每轮**毁一次缓存。
    # 它不收 timezone:那一层没有任何时间戳(见 bundles/recipes/server.py 的 build)。
    tools += registry.qualify_tools("recipes", build_recipes(data_dir).tools)
    # M6-6a 学习:同样**追加在末尾**,上面 20 个一格没动(M6-6b 课件那两个在它自己那段末尾)。
    # 它收 timezone:回收站目录名里有时间戳。
    tools += registry.qualify_tools("courses", build_courses(data_dir, timezone=timezone).tools)
    # M6-7 待办:同样**追加在末尾**,上面 29 个一格没动。收 timezone:「今天」和完成时间按它算。
    tools += registry.qualify_tools("todos", build_todos(data_dir, timezone=timezone).tools)
    return AssembledTools(tools=tools, proposal_tool=memory.propose_fact)


def _push_notifier(steward: Steward) -> Any:
    """三处调用点共用的通知器构造。渠道走配置(`LARARIUM_PUSH_CHANNEL`),不再写死 "cli"
    ——M5 双通道下推送若掉进没人看的窗口,等于没推(M3 结转第 2 条)。"""
    from lararium.steward.sweep import make_daily_notifier

    return make_daily_notifier(
        journal=steward.journal,
        outbox=steward.outbox,
        conn=steward.outbox.conn,
        timezone=steward.settings.timezone,
        channel=steward.settings.push_channel,
    )


def build_steward(settings: Settings, ledger: Any, gate: Any) -> Steward:
    """组装 Steward。这是全系统唯一允许 import bundles 的地方之一(组装根)。

    放这里而不是 cli.py:M2-6 之后 cli 降级为纯 HTTP 客户端、不再 import bundles,
    build_steward 若留在 cli 上会让那一步被迫动它;server 是和 cli 并列的组装根,
    放这里是它该在的位置。
    """
    conn = connect(settings.data_dir / "steward.sqlite")
    registry = Registry.load(Path("bundles"))
    # M4-8:人设(用户的)+ 纪律(系统的)。人设怎么改都不影响纪律,那是拆开的全部意义。
    persona, warnings = assemble_persona(settings.data_dir)
    for warning in warnings:
        logger.warning(warning)
    inbox = Inbox(conn)
    # M6-6c:收到的 PDF 后台逐页转文字。**视觉关着就不接**——转换是让模型看页图,它看不了图
    # 就无从谈起(read_pdf 那时也只回"看不了图")。模型客户端单独一个:和聊天同一个 key、
    # 同一个模型(得是能看图的那个),但各用各的连接池;让不让路由 `chat_busy` 管。
    transcriber = (
        Transcriber(
            pages=PdfText(conn),
            journal=Journal(conn),
            media_dir=settings.data_dir / "media",
            reader=PydanticAIClient(settings),
            chat_busy=inbox.has_unfinished,
            instructions=load_page_prompt(),
        )
        if settings.vision
        else None
    )
    assembled = _assemble_bundle_tools(
        settings.data_dir, gate, timezone=settings.timezone, registry=registry
    )
    steward = Steward(
        settings=settings,
        inbox=inbox,
        journal=Journal(conn),
        registry=registry,
        ledger=ledger,
        gate=gate,
        model=PydanticAIClient(settings),
        persona=persona,
        outbox=Outbox(conn),
        threads=Threads(conn),
        # M1 进程内挂载;M2 容器化时换成 MCP 传输,工具定义不变
        bundle_tools=assembled.tools,
        proposal_tool=assembled.proposal_tool,
        transcriber=transcriber,
    )
    # 前缀变更留痕:改了人设、缓存命中从 90% 掉到 0,得有地方说得清为什么
    # (不可协商第 1 条:缓存命中是设计约束,不是优化项)。
    # **按真正发出去的东西算**,不枚举"已知重建点"——那张清单第一版就漏了工具 schema。
    # 放在 Steward 造好之后,是因为要拿 all_tools()(第 0 层就在那里面)。
    digest = prefix_digest(
        render_system_prompt(
            persona=persona, directory=registry.directory_lines(), ledger=ledger.read()
        ),
        tool_schema_fingerprint(steward.all_tools()),
    )
    previous = record_prefix_change(conn, digest)
    if previous is not None:
        logger.warning("前缀区变了:%s → %s,本次启动缓存会重建一次", previous[:12], digest[:12])
    # M6-6d 旧工具名映射的删除条件:每次启动问一次,它的读者(这台机器上没压缩的历史)
    # 没了就说出来。读到这句的人要做的事是删代码,所以措辞直接写删哪几处。
    if steward.legacy_tool_names_retired():
        logger.warning(
            "旧工具名映射已经没有读者了(没压缩的历史里一次改名前的工具记录都没有):"
            "可以删掉 Registry.legacy_tool_names、Steward._legacy_tool_names 及其两处调用(M6-6d)"
        )
    return steward


def create_app(
    *,
    steward: Steward,
    ledger: Any,
    gate: Any,
    control_tokens: dict[str, str],
    ingest_tokens: dict[str, str],
    wake: asyncio.Event,
) -> Starlette:
    """纯组装,可测。token 分能力两类(M2-5 补做):

    - control_tokens(控制端,你):四个端点全权——messages + outbox + commands + health;
    - ingest_tokens(数据面来源):**只准 POST /v1/messages**,其余一律 403。

    命令端点是门控的开关:若是任意 token 都能按它,恶意短信正常入站(提案 pending,
    门控在正常工作)后,同一个 token 自己 POST /v1/commands 就能批准自己——攻击链
    不需要攻破模型。所以控制端与数据面必须分开配(token 决定渠道,也决定能力)。

    ledger/gate 由 Memory bundle 提供,这里只调用它的少量方法(如
    gate.unsettled_count),形状归 bundle 管——组装根的适配接口,和 build_steward
    里的 ledger/gate 一样不定死类型。
    """

    def authenticate(request: Request) -> tuple[str, str] | None:
        """返回 (scope, channel);scope ∈ {"control","ingest"}。token 对不上 → None。"""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer ") :].strip()
        for channel, expected in control_tokens.items():
            if hmac.compare_digest(expected, token):
                return ("control", channel)
        for channel, expected in ingest_tokens.items():
            if hmac.compare_digest(expected, token):
                return ("ingest", channel)
        return None

    def require_control(request: Request) -> tuple[str, str] | JSONResponse:
        """控制端专属端点认证:通过返回 (scope, channel);否则返回要直接回给客户端的
        401(无/错 token)或 403(有效 token 但能力不足——ingest 数据面)。"""
        auth = authenticate(request)
        if auth is None:
            return _UNAUTHORIZED
        if auth[0] != "control":
            # 有效但只配入站的 token 想按门控开关 → 403。不泄露(比如)token 是否有效。
            return _FORBIDDEN
        return (auth[0], auth[1])

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # 启动:清上次崩溃遗留的 processing(否则串行队列永久卡死),再起 worker。
        requeued, abandoned = steward.inbox.recover_stale()
        if requeued or abandoned:
            logger.info("上次有未处理完的消息:%d 条已重新排队,%d 条已放弃", requeued, abandoned)
        # M3-4 补做:启动期预热 embedding,失败只记日志不拦启动。
        # embed() 本是在第一次 journal.append(process_next 里,同步)时才懒加载;
        # 没缓存时是十分钟,那十分钟事件循环整个卡住,/health 和 /messages 一起没反应。
        # 慢启动是诚实的,聊到一半卡住不是。
        from lararium.steward.embeddings import embedding_available

        if not await asyncio.to_thread(embedding_available):
            logger.warning(
                "embedding 模型未就绪:recall_similar 将提示暂不可用。首次需下载权重(约 10 分钟,M4 打进镜像)"
            )
        # M3-6:空闲自动压缩——compactor 用真 Gate 造好(Steward 的 GatePort 不放 propose)。
        # P1-3:注入带日限的通知器,压缩被屏障停/归拢提提案时用户能收到消息(别堵死没人知)。
        from lararium.steward.compact import make_compactor

        notify = _push_notifier(steward)
        compactor = make_compactor(
            steward.settings,
            steward.journal,
            gate,
            steward.threads,
            steward.registry,
            ledger=steward.ledger,
            notify=notify,
        )
        worker = Worker(steward, wake, compactor=compactor)
        tasks = [asyncio.create_task(worker.run())]
        # M6-6c:PDF 后台转换和 worker 并排跑——**不在 worker 的循环里**:那样一页挂住的转换
        # 会把后面所有消息堵住。它启动时先扫一遍池子,重启前转到一半的、6c 之前就收到的都会续上。
        if steward.transcriber is not None:
            tasks.append(asyncio.create_task(steward.transcriber.run()))
        try:
            yield
        finally:
            # 退出:cancel worker(和转换)+ 最后一次结算(把已通过的提案落盘)。
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            settled = steward.settle_if_needed()
            if settled:
                logger.info("退出前结算 %d 条提案", settled)

    async def post_message(request: Request) -> JSONResponse:
        # 控制端与数据面都能入站(数据面来的消息照样是 Envelope,照样走门控)。
        auth = authenticate(request)
        if auth is None:
            return _UNAUTHORIZED
        scope, channel = auth
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
        # M5-4:附件是**引用**,字节在 `{data_dir}/media/` 下。校验立在 Attachment
        # 的类型上(sha256 必须是 64 位十六进制)——一个能自报路径的附件就是路径穿越
        # 的入口。畸形是客户端问题 → 400,别把网络面打成 500。
        raw_attachments = payload.get("attachments") or []
        if not isinstance(raw_attachments, list) or len(raw_attachments) > MAX_ATTACHMENTS:
            return JSONResponse({"error": "attachments 必须是列表且不超过 8 个"}, status_code=400)
        try:
            attachments = [Attachment.model_validate(a) for a in raw_attachments]
        except ValidationError:
            return JSONResponse({"error": "attachments 形状不对"}, status_code=400)

        # P0-1 安全洞:入口按 token scope 定信封形状。数据面(短信/邮件转发)投进来的
        # 内容是不可信来源——信封必须带 untrusted,否则被当用户亲口说渲染、被自动放行。
        # **不许从请求体读 meta**:让投递方自己声明自己可信等于没有防线。
        if scope == "control":
            source: Literal["user", "module_event"] = "user"
            meta: dict[str, Any] = {}
        else:  # ingest 数据面
            source = "module_event"
            meta = {"untrusted": True}

        try:
            env = Envelope.new(
                source=source,
                channel=channel,
                content=content,
                attachments=attachments,
                meta=meta,
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
        auth = require_control(request)
        if isinstance(auth, JSONResponse):
            return auth
        _, channel = auth
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
        auth = require_control(request)
        if isinstance(auth, JSONResponse):
            return auth
        return JSONResponse(
            {"pending": steward.inbox.pending_count(), "unsettled": gate.unsettled_count()},
            status_code=200,
        )

    async def post_command(request: Request) -> JSONResponse:
        """命令端点——**这个端点从此就是门控的开关(D12)**,且只对控制端开放。

        /approve /settle /rollback 都从这里直通 Gate,不经模型。ingest token 若也能按它,
        恶意短信正常入站(提案 pending)后同一个 token 自己批准自己——攻击链不需要攻破模型,
        门控整个溶掉。所以这里只认控制端 token(M2-5 补做)。

        M5 做 python_sandbox 时,"沙箱无网络"就是防它被**模型自己 POST** 到这里的墙——
        两条约束是绑定的,谁也不许单独放松:沙箱一旦联网,被注入的模型就能自己 /approve
        把恶意事实永久写进账本。
        """
        auth = require_control(request)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "body 必须是 JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "body 必须是 JSON 对象"}, status_code=400)
        line = payload.get("line")
        if not isinstance(line, str) or not line.strip():
            return JSONResponse({"error": "缺 line 字段"}, status_code=400)

        if line.strip() == "/quit":
            # /quit 在 HTTP 语境零副作用:结算有它自己的时机(worker 空闲 D11 / /settle),
            # 客户端关窗口不是系统事件,不该触发前缀缓存重建,也不该吞掉任何错误。
            return JSONResponse({"text": "服务端无退出概念,请直接关客户端。"}, status_code=200)

        if line.strip() == "/sweep":
            # M3-5 夜间归拢:手动命令(占时,需要模型)。归拢**只写话头和 pending 提案**,
            # 账本写入永远走 Gate.settle;模型输入输出都落起居注(sweep.sweep 内部做)。
            from datetime import UTC as _UTC
            from datetime import datetime as _dt
            from datetime import timedelta as _td

            from lararium.steward.sweep import make_sweeper

            notify = _push_notifier(steward)
            now = _dt.now(_UTC)
            sweeper = make_sweeper(
                steward.settings,
                steward.journal,
                steward.threads,
                gate,
                steward.registry,
                ledger=steward.ledger,
                notify=notify,
            )
            # since 只是这次触发的名义窗口(留进起居注当由头),**不是扫描下界**——
            # 下界是光标,没归拢过的历史再老也要补(M5-24)。改这里的 24 小时不影响扫哪一段。
            sweep_result = await sweeper.run(
                since=(now - _td(hours=24)).isoformat(), until=now.isoformat()
            )
            return JSONResponse({"text": sweep_result.summary}, status_code=200)

        if line.strip() == "/compact":
            # M3-6 压缩:手动触发(占时,需要模型)。compactor 用真 Gate 造好(Steward 的
            # GatePort 不放 propose,单写者编进类型);上下文未顶满时 no-op。
            from lararium.steward.compact import make_compactor

            notify = _push_notifier(steward)
            compactor = make_compactor(
                steward.settings,
                steward.journal,
                gate,
                steward.threads,
                steward.registry,
                ledger=steward.ledger,
                notify=notify,
            )
            compact_summary = await steward.maybe_compact(compactor)
            return JSONResponse({"text": compact_summary or "上下文未满,无需压缩"}, status_code=200)

        result = handle_command(line, steward=steward, ledger=ledger, gate=gate)
        return JSONResponse({"text": result.text}, status_code=200)

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
        control_tokens=settings.control_tokens,
        ingest_tokens=settings.ingest_tokens,
        wake=wake,
    )
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
