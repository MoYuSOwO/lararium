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
    """每轮打印缓存命中——这是 DESIGN §1.5 的硬约束的可观测形式。"""
    if reply.cache_hit_tokens is None or not reply.prompt_tokens:
        return f"[cache] 未知 · prompt={reply.prompt_tokens} completion={reply.completion_tokens}"
    rate = reply.cache_hit_tokens / reply.prompt_tokens * 100
    return (
        f"[cache] 命中 {reply.cache_hit_tokens}/{reply.prompt_tokens} ({rate:.1f}%) "
        f"· completion={reply.completion_tokens}"
    )


class PydanticAIClient:
    """真实模型客户端。库 API 若有变动,只改这一个类。"""

    def __init__(self, settings: Settings) -> None:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        self._settings = settings
        self._model = OpenAIChatModel(
            settings.model_name,
            provider=OpenAIProvider(base_url=settings.api_base_url, api_key=settings.api_key),
        )

    async def run(
        self, ctx: AssembledContext, tools: list[Callable], mcp_servers: list[Any]
    ) -> ModelReply:
        from pydantic_ai import Agent
        from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

        agent = Agent(
            self._model,
            system_prompt=ctx.system_prompt,
            tools=tools,
            toolsets=mcp_servers,
        )

        history: list[ModelRequest | ModelResponse] = []
        for msg in ctx.messages[:-1]:
            if msg["role"] == "user":
                history.append(ModelRequest(parts=[UserPromptPart(content=msg["content"])]))
            else:
                history.append(ModelResponse(parts=[TextPart(content=msg["content"])]))

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
        )
