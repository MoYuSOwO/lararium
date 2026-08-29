import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from lararium.config import Settings
from lararium.steward.assembler import AssembledContext
from lararium.steward.vision import ImageReturn

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


class ModelCallError(Exception):
    """模型调用失败。retryable 是 loop 决定「重试」还是「终态」的唯一依据。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


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


class PydanticAIClient:
    """真实模型客户端。库 API 若有变动,只改这一个类。"""

    def __init__(
        self, settings: Settings, model: Any | None = None, *, http_client: Any | None = None
    ) -> None:
        self._settings = settings
        # model 是给报文级测试留的注入口(FunctionModel),也是 M2 换服务商的接缝。
        # 隔离盒是唯一接触第三方语义的地方,必须留得下测试——P0-1 的教训。
        if model is not None:
            self._model = model
            return
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        # SDK 默认 max_retries=2,会和我们的持久重试叠乘(实测一次 429 底下打 3 个请求)。
        # 重试策略只留我们这层:它在库里、有上限、跨重启有效、起居注可见;
        # SDK 那层是内存里的、起居注看不见的、重启就丢的。http_client 是给
        # 报文级测试留的传输注入口(MockTransport),生产不传。
        self._model = OpenAIChatModel(
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

    async def run(
        self, ctx: AssembledContext, tools: list[Callable], mcp_servers: list[Any]
    ) -> ModelReply:
        from pydantic_ai import Agent
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

        try:
            result = await agent.run(prompt, message_history=history)
        except Exception as exc:
            # 把 pydantic-ai 的异常在这里分类成自家类型——loop 只认 ModelCallError,
            # 不认第三方异常,这是隔离盒存在的理由(D2)。消息由 _error_message
            # 翻译(上下文超长类说人话,M3-1)。
            raise ModelCallError(_error_message(exc), retryable=_classify_retryable(exc)) from exc
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
