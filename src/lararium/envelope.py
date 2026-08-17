import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Source = Literal["user", "cron", "module_event"]


class Envelope(BaseModel):
    id: str
    source: Source
    channel: str
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
