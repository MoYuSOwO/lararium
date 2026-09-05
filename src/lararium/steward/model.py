import functools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from lararium.config import Settings
from lararium.steward.assembler import AssembledContext
from lararium.steward.vision import ImageReturn

logger = logging.getLogger("lararium")

# 不同服务商对"缓存命中 token"的字段名不一样,按优先级探测。
# pydantic-ai 的 RunUsage 顶层用 cache_read_tokens;DeepSeek/OpenAI 兼容层用
# details.prompt_cache_hit_tokens / cached_tokens。
_CACHE_HIT_KEYS = (
    "prompt_cache_hit_tokens",
    "cache_read_input_tokens",
    "cached_tokens",
    "cache_read_tokens",
)


@dataclass(frozen=True)
class ModelReply:
    text: str
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    cache_hit_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # 注意:下面这些 token 数字是**整轮累加**的,不是单次请求。一轮里模型每调一次
    # 工具就多一次请求(发起调用一次、拿到结果再答一次),用量逐次叠加。
    # requests 记录请求次数,看到「2 请求」就知道百分比被工具往返稀释过,
    # 不必怀疑前缀不稳定。
    requests: int | None = None


class ModelClient(Protocol):
    async def run(
        self, ctx: AssembledContext, tools: list[Callable], mcp_servers: list[Any]
    ) -> ModelReply: ...


# 模型调用错误的二分类(P2-3)。HTTP 状态下:429/5xx 是临时性失败,重试有意义;
# 4xx 里只有明确的"这个请求本身没戏"(key 错、上下文超长、请求非法)才算终态。
# 其余一律按可重试——重试上限会把持续失败转成终态,而把可重试误判成终态
# 是消息永久丢失,这个不对称是有意的,不许为了"保守"反过来写。
_NON_RETRYABLE_STATUS = frozenset({400, 401, 403, 404, 422})


# 一条重试细节里每个字段的字符上限。**要够长到能看清参数,又不能无界**:
# 它会进起居注,而模型填错的参数理论上可以任意长。
MAX_RETRY_DETAIL_CHARS = 600
# 一轮最多带出几条。工具重试上限是 1,一轮里几个工具各错一次就到头了。
MAX_RETRY_DETAILS = 8


class ModelCallError(Exception):
    """模型调用失败。retryable 是 loop 决定「重试」还是「终态」的唯一依据。

    `details` 是**出错那条路上才有**的重试细节(M5-13):模型填的参数 + 服务端的反馈。
    `Tool 'x' exceeded max retries count of 1` 这一行本身什么都没说,而库把校验详情吞在
    异常正文之外——没有这份东西,真机上只能看着「处理失败,已放弃」干瞪眼。
    """

    def __init__(
        self, message: str, *, retryable: bool, details: tuple[dict[str, str], ...] = ()
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.details = details


def _classify_retryable(exc: Exception) -> bool:
    """第三方异常的形状只有隔离盒知道——分类必须在这里做完,loop 只认自家类型。"""
    from pydantic_ai.exceptions import ModelHTTPError

    if isinstance(exc, ModelHTTPError):
        # 拿得到 status_code 就按白名单判终态;429/5xx/未知码一律可重试。
        return exc.status_code not in _NON_RETRYABLE_STATUS
    # 连接错误、超时、UnexpectedModelBehavior 及一切认不出的:默认可重试。
    return True


# 服务商对"上下文超长"的报错措辞五花八门(market)。M3-1 识别这一类 400,
# 把 notice 从 `status_code: 400` 换成说人话的「把 LARARIUM_L0_MAX_TOKENS 调小」。
_CONTEXT_TOO_LONG_MARKERS = (
    "maximum context length",
    "context length exceeded",
    "context_length_exceeded",
    "prompt is too long",
    "the prompt is too long",
    "tokens exceeds the maximum",
    "请求的上下文长度超过",
    "上下文超长",
)


def _context_too_long(exc: Exception) -> bool:
    """是不是"上下文超长"类 400——只有 400,且 body/消息里带超长措辞才认。"""
    from pydantic_ai.exceptions import ModelHTTPError

    if not isinstance(exc, ModelHTTPError) or exc.status_code != 400:
        return False
    blob = f"{exc.body or ''} {exc}".lower()
    return any(m in blob for m in _CONTEXT_TOO_LONG_MARKERS)


def _error_message(exc: Exception) -> str:
    """把第三方异常翻译成给用户看的消息;上下文超长类给可操作的提示。"""
    if _context_too_long(exc):
        return "上下文超长:把 LARARIUM_L0_MAX_TOKENS 调小,或等压缩(L3 起)腾出空间。"
    return f"{type(exc).__name__}: {exc}"


def unwrap_tool_args(args: Any, schema: dict[str, Any] | None) -> Any | None:
    """服务商把参数多包了一层 `{"arguments": {...}}` 时,返回剥掉之后的那份;否则 None。

    M5-13 实测(AMD 自部署端点):`function.arguments` 偶发地变成

        {"arguments": {"amount": 5, "category": "交通", "note": "地铁"}}

    校验于是报 missing/extra_forbidden,一轮里两次都包错就把工具重试耗尽,
    用户收到「处理失败,已放弃」——**一笔账就这么没了**。

    **判据是那个工具的 schema 里有没有 `arguments` 这个参数**,不是"长得像信封"。
    照形状猜的话,总有一天会把一次合法调用拆散,而症状是参数凭空少了一半。
    """
    if schema and "arguments" in (schema.get("properties") or {}):
        return None  # 人家真有这么个参数,别动
    parsed = args
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except ValueError:
            return None
    if not isinstance(parsed, dict) or set(parsed) != {"arguments"}:
        return None
    inner = parsed["arguments"]
    if not isinstance(inner, dict):
        return None
    return json.dumps(inner, ensure_ascii=False) if isinstance(args, str) else inner


def _clip(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    return (
        text if len(text) <= MAX_RETRY_DETAIL_CHARS else text[:MAX_RETRY_DETAIL_CHARS] + "…(截断)"
    )


def retry_details(messages: list[Any]) -> tuple[dict[str, str], ...]:
    """从库自己的消息流里把「重试提示」配上「触发它的那次调用」。

    **两半都要**:只有反馈不知道模型填了什么,只有参数不知道哪里不合法。配对靠
    `tool_call_id`——重试提示会作为一条 tool 消息回给模型,所以它一定在这份消息流里,
    哪怕那条 HTTP 请求抓不到(实测三种抓报文的办法都没拦到那条 client)。
    """
    args_by_id: dict[str, Any] = {}
    out: list[dict[str, str]] = []
    for message in messages:
        for part in getattr(message, "parts", []):
            kind = getattr(part, "part_kind", "")
            if kind == "tool-call":
                args_by_id[part.tool_call_id] = part.args
            elif kind == "retry-prompt":
                out.append(
                    {
                        "tool": str(getattr(part, "tool_name", "") or "(无工具名)"),
                        "args": _clip(
                            args_by_id.get(getattr(part, "tool_call_id", ""), "(没抓到)")
                        ),
                        "feedback": _clip(part.model_response()),
                    }
                )
    return tuple(out[:MAX_RETRY_DETAILS])


def extract_cache_hit_tokens(usage: Any) -> int | None:
    details = getattr(usage, "details", None) or {}
    for key in _CACHE_HIT_KEYS:
        if key in details:
            return int(details[key])
    for key in _CACHE_HIT_KEYS:
        value = getattr(usage, key, None)
        if value is not None:
            return int(value)
    return None


def format_cache_log(reply: ModelReply) -> str:
    """每轮打印缓存命中——这是 DESIGN §1.5 的硬约束的可观测形式。
    用量是整轮累加的;带上请求数,免得把工具往返稀释的百分比误读成前缀不稳定。"""
    reqs = f" · {reply.requests} 请求" if reply.requests else ""
    if reply.cache_hit_tokens is None or not reply.prompt_tokens:
        return f"[cache] 未知 · prompt={reply.prompt_tokens} completion={reply.completion_tokens}{reqs}"
    rate = reply.cache_hit_tokens / reply.prompt_tokens * 100
    return (
        f"[cache] 本轮命中 {reply.cache_hit_tokens}/{reply.prompt_tokens} ({rate:.1f}%) "
        f"· completion={reply.completion_tokens}{reqs}"
    )


def _adapt(tool: Callable) -> Callable:
    """把返回 `ImageReturn` 的工具转成库自己的 `ToolReturn`。

    **转换必须在这里**:`tools.py` 不许 import pydantic-ai(D2——第三方语义只准出现在
    这个隔离盒里),所以它返回的是中立形状。签名和 docstring 原样保留
    (`functools.wraps`),否则工具 schema 变了 = 前缀第 0 层变了 = 缓存全毁。

    `return_value` 只放**一行文本**:它是进起居注和 L0 的那一份,不能是一坨字节的 repr。
    图片走 `content`,由库当成随后的一条 user 内容发出去——和到达轮的处理是同一种形状。
    """

    @functools.wraps(tool)
    def adapted(*args: Any, **kwargs: Any) -> Any:
        result = tool(*args, **kwargs)
        if not isinstance(result, ImageReturn):
            return result
        from pydantic_ai import ToolReturn
        from pydantic_ai.messages import BinaryContent

        return ToolReturn(
            return_value=result.text,
            content=[BinaryContent(data=i.data, media_type=i.media_type) for i in result.images]
            or None,
        )

    return adapted


def _unwrapping_model(wrapped: Any) -> Any:
    """把模型包一层,专治上面那个信封。**放在隔离盒里**:它是某一家服务商的毛病,
    不该渗进组装器、工具或者 schema——工具 schema 动一个字节就是一次前缀重建。
    """
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.models.wrapper import WrapperModel

    class Unwrapping(WrapperModel):
        async def request(self, messages, model_settings, model_request_parameters):
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
            schemas = {
                t.name: t.parameters_json_schema
                for t in getattr(model_request_parameters, "function_tools", []) or []
            }
            for part in response.parts:
                if not isinstance(part, ToolCallPart):
                    continue
                fixed = unwrap_tool_args(part.args, schemas.get(part.tool_name))
                if fixed is not None:
                    logger.warning(
                        "服务商把 %s 的参数多包了一层 arguments,已剥掉:%s",
                        part.tool_name,
                        part.args,
                    )
                    part.args = fixed
            return response

    return Unwrapping(wrapped)


class PydanticAIClient:
    """真实模型客户端。库 API 若有变动,只改这一个类。"""

    def __init__(
        self, settings: Settings, model: Any | None = None, *, http_client: Any | None = None
    ) -> None:
        self._settings = settings
        # model 是给报文级测试留的注入口(FunctionModel),也是 M2 换服务商的接缝。
        # 隔离盒是唯一接触第三方语义的地方,必须留得下测试——P0-1 的教训。
        if model is not None:
            self._model = _unwrapping_model(model)
            return
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        # SDK 默认 max_retries=2,会和我们的持久重试叠乘(实测一次 429 底下打 3 个请求)。
        # 重试策略只留我们这层:它在库里、有上限、跨重启有效、起居注可见;
        # SDK 那层是内存里的、起居注看不见的、重启就丢的。http_client 是给
        # 报文级测试留的传输注入口(MockTransport),生产不传。
        self._model = _unwrapping_model(
            OpenAIChatModel(
                settings.model_name,
                provider=OpenAIProvider(
                    openai_client=AsyncOpenAI(
                        base_url=settings.api_base_url,
                        api_key=settings.api_key,
                        http_client=http_client,
                        max_retries=0,
                    )
                ),
            )
        )

    async def run(
        self, ctx: AssembledContext, tools: list[Callable], mcp_servers: list[Any]
    ) -> ModelReply:
        from pydantic_ai import Agent, capture_run_messages
        from pydantic_ai.messages import (
            BinaryContent,
            ModelRequest,
            ModelResponse,
            SystemPromptPart,
            TextPart,
            ToolCallPart,
            ToolReturnPart,
            UserPromptPart,
        )

        # 前缀**不能**走 Agent(system_prompt=...):message_history 非空时
        # pydantic-ai 不再注入它(2.31.0 实测),第二轮起人格/目录/账本会整个消失。
        # 唯一可靠的做法是把前缀作为 SystemPromptPart 放进历史首条 ModelRequest。
        # 首轮历史为空时也照此构造——只有一条路径,才不会有一条悄悄退化。
        # 等价写法是 Agent(instructions=...):它是 pydantic-ai 为"每轮重新应用、不进历史"
        # 这个语义加的参数,HTTP body 逐字节相同(实测)。哪天升级后本写法失效,那是退路。
        agent = Agent(self._model, tools=[_adapt(t) for t in tools], toolsets=mcp_servers)

        # M4-5c v2:历史里的工具往返走**协议层原生形状**——assistant 带 tool_calls,
        # 每次调用配一条 tool 结果消息。组装器给的是与服务商无关的 dict 形态
        # (role/tool_calls/tool_call_id),映射成库内部表示是隔离盒的活(D2):
        # 形状的细节只有这里该知道。
        history: list[ModelRequest | ModelResponse] = []
        for msg in ctx.messages[:-1]:
            if msg["role"] == "user":
                history.append(ModelRequest(parts=[UserPromptPart(content=msg["content"])]))
            elif msg["role"] == "tool":
                history.append(
                    ModelRequest(
                        parts=[
                            ToolReturnPart(
                                tool_name=msg["name"],
                                content=msg["content"],
                                tool_call_id=msg["tool_call_id"],
                            )
                        ]
                    )
                )
            elif msg.get("tool_calls"):
                history.append(
                    ModelResponse(
                        parts=[
                            ToolCallPart(tool_name=c["name"], args=c["args"], tool_call_id=c["id"])
                            for c in msg["tool_calls"]
                        ]
                    )
                )
            else:
                history.append(ModelResponse(parts=[TextPart(content=msg["content"])]))

        prefix = SystemPromptPart(content=ctx.system_prompt)
        if history and isinstance(history[0], ModelRequest):
            history[0] = ModelRequest(parts=[prefix, *history[0].parts])
        else:
            history.insert(0, ModelRequest(parts=[prefix]))

        # M5-5:图**只挂在最后一条**(组装器结构上只有那一个挂载点)。有图时当前轮
        # 发的是 [正文, 图…] 的多模态形状;没图时逐字节还是原来那个字符串
        # ——不许因为加了这条路,把所有不带图的轮次的报文也换个形状。
        last = ctx.messages[-1]
        prompt: Any = last["content"]
        if last.get("images"):
            prompt = [
                last["content"],
                *(BinaryContent(data=i.data, media_type=i.media_type) for i in last["images"]),
            ]

        # M5-13:`capture_run_messages` 是库自己给的口子,专门用来回答"炸的时候都发了些
        # 什么"。**只在出错那条路上取**,正常轮次一个字节都不多带。
        with capture_run_messages() as captured:
            try:
                result = await agent.run(prompt, message_history=history)
            except Exception as exc:
                # 把 pydantic-ai 的异常在这里分类成自家类型——loop 只认 ModelCallError,
                # 不认第三方异常,这是隔离盒存在的理由(D2)。消息由 _error_message
                # 翻译(上下文超长类说人话,M3-1)。
                details = retry_details(captured)
                if details:
                    logger.warning("工具重试耗尽,模型填的参数与服务端反馈:%s", details)
                raise ModelCallError(
                    _error_message(exc), retryable=_classify_retryable(exc), details=details
                ) from exc
        usage = result.usage

        tool_events: list[dict[str, Any]] = []
        for message in result.new_messages():
            for part in getattr(message, "parts", []):
                kind = getattr(part, "part_kind", "")
                if kind == "tool-call":
                    tool_events.append(
                        {
                            "type": "tool_call",
                            "tool": part.tool_name,
                            "args": part.args,
                            # M4-5c v2:配对要用它(L0 回放时把调用和结果配成对)。
                            # 只用于配对,**不往外发**——发出去的 call_id 是自己造的。
                            "tool_call_id": part.tool_call_id,
                        }
                    )
                elif kind == "tool-return":
                    tool_events.append(
                        {
                            "type": "tool_result",
                            "tool": part.tool_name,
                            "content": str(part.content),
                            "tool_call_id": part.tool_call_id,
                        }
                    )

        return ModelReply(
            text=result.output,
            tool_events=tool_events,
            cache_hit_tokens=extract_cache_hit_tokens(usage),
            prompt_tokens=getattr(usage, "input_tokens", None)
            or getattr(usage, "request_tokens", None),
            completion_tokens=getattr(usage, "output_tokens", None)
            or getattr(usage, "response_tokens", None),
            requests=getattr(usage, "requests", None),
        )
