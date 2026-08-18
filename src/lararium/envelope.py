import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["user", "cron", "module_event"]


class Envelope(BaseModel):
    # 信封是**所有外部输入**的入口,校验不该有"从旁边绕进来"的路。
    # 曾经 env.id = client_id 这行事后赋值绕过了校验(P1-4 换字段),打开
    # validate_assignment 后任何赋值都会重新过一遍类型/pattern——下次轮到谁
    # 也不会再从旁边溜进来。
    model_config = ConfigDict(validate_assignment=True)

    # id 是协议上就由客户端提供的(幂等键),却被 search_history 渲染在围栏**外**——
    # 自由文本能伪装成系统的框定语(P1-4,这次更硬)。所以它必须是 32 位 hex,
    # 在类型上立死,不许让任何别的东西流进来。
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source: Source
    # channel 会被插在不可信内容的框定语里(且在围栏外),所以它必须是个标识符而不是
    # 自由文本。M2 的 ingress 是它的入口:路由名由服务端给,但校验要立在类型上,
    # 不能指望每个调用方都自觉。
    channel: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)
    ts: datetime

    @classmethod
    def new(
        cls,
        *,
        source: Source,
        channel: str,
        content: str,
        meta: dict[str, Any] | None = None,
        id: str | None = None,
    ) -> "Envelope":
        # 客户端给 id 就构造时带上(让 Envelope 自己把关,非法即 ValidationError);
        # 不给才生成。绝不构造后再赋值绕过校验。
        return cls(
            id=id or uuid.uuid4().hex,
            source=source,
            channel=channel,
            content=content,
            meta=meta or {},
            ts=datetime.now(UTC),
        )
