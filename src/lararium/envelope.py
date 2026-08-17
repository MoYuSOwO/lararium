import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Source = Literal["user", "cron", "module_event"]


class Envelope(BaseModel):
    id: str
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
    ) -> "Envelope":
        return cls(
            id=uuid.uuid4().hex,
            source=source,
            channel=channel,
            content=content,
            meta=meta or {},
            ts=datetime.now(UTC),
        )
