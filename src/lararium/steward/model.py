from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from lararium.config import Settings
from lararium.steward.assembler import AssembledContext

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


class PydanticAIClient:
    """真实模型客户端。库 API 若有变动,只改这一个类。"""

    def __init__(self, settings: Settings, model: Any | None = None) -> None:
        self._settings = settings
        # model 是给报文级测试留的注入口(FunctionModel),也是 M2 换服务商的接缝。
        # 隔离盒是唯一接触第三方语义的地方,必须留得下测试——P0-1 的教训。
        if model is not None:
            self._model = model
            return
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        self._model = OpenAIChatModel(
            settings.model_name,
            provider=OpenAIProvider(base_url=settings.api_base_url, api_key=settings.api_key),
        )

    async def run(
        self, ctx: AssembledContext, tools: list[Callable], mcp_servers: list[Any]
    ) -> ModelReply:
        from pydantic_ai import Agent
        from pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            SystemPromptPart,
            TextPart,
            UserPromptPart,
        )

        # 前缀**不能**走 Agent(system_prompt=...):message_history 非空时
        # pydantic-ai 不再注入它(2.31.0 实测),第二轮起人格/目录/账本会整个消失。
        # 唯一可靠的做法是把前缀作为 SystemPromptPart 放进历史首条 ModelRequest。
        # 首轮历史为空时也照此构造——只有一条路径,才不会有一条悄悄退化。
        agent = Agent(self._model, tools=tools, toolsets=mcp_servers)

        history: list[ModelRequest | ModelResponse] = []
        for msg in ctx.messages[:-1]:
            if msg["role"] == "user":
                history.append(ModelRequest(parts=[UserPromptPart(content=msg["content"])]))
            else:
                history.append(ModelResponse(parts=[TextPart(content=msg["content"])]))

        prefix = SystemPromptPart(content=ctx.system_prompt)
        if history and isinstance(history[0], ModelRequest):
            history[0] = ModelRequest(parts=[prefix, *history[0].parts])
        else:
            history.insert(0, ModelRequest(parts=[prefix]))

        result = await agent.run(ctx.messages[-1]["content"], message_history=history)
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
                        }
                    )
                elif kind == "tool-return":
                    tool_events.append(
                        {
                            "type": "tool_result",
                            "tool": part.tool_name,
                            "content": str(part.content),
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
