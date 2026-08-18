import os
from dataclasses import dataclass
from pathlib import Path


def parse_tokens(raw: str) -> dict[str, str]:
    """解析 LARARIUM_TOKENS(渠道:token[,渠道:token…])→ {channel: token}。

    token 决定 channel(DESIGN §9):channel 会被渲染进不可信内容的框定语(P1-4),
    来源必须由服务端认定,客户端无权自报。空白渠道名/空 token 视为格式错。
    """
    tokens: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        channel, _, tok = part.partition(":")
        channel, tok = channel.strip(), tok.strip()
        if not channel or not tok:
            raise ValueError(f"LARARIUM_TOKENS 格式错:{part!r},应为 渠道:token")
        tokens[channel] = tok
    return tokens


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_base_url: str
    model_name: str
    data_dir: Path
    timezone: str
    l0_max_turns: int
    max_attempts: int
    bind_host: str
    bind_port: int
    tokens: dict[str, str]

    @classmethod
    def load(cls) -> "Settings":
        api_key = os.environ.get("LARARIUM_API_KEY", "")
        if not api_key:
            raise ValueError("LARARIUM_API_KEY 未设置,请参考 .env.example")
        return cls(
            api_key=api_key,
            api_base_url=os.environ.get("LARARIUM_API_BASE_URL", "https://api.deepseek.com/v1"),
            model_name=os.environ.get("LARARIUM_MODEL", "deepseek-chat"),
            data_dir=Path(os.environ.get("LARARIUM_DATA_DIR", "./data")),
            timezone=os.environ.get("LARARIUM_TIMEZONE", "Asia/Shanghai"),
            l0_max_turns=int(os.environ.get("LARARIUM_L0_MAX_TURNS", "30")),
            max_attempts=int(os.environ.get("LARARIUM_MAX_ATTEMPTS", "3")),
            bind_host=os.environ.get("LARARIUM_BIND_HOST", "127.0.0.1"),
            bind_port=int(os.environ.get("LARARIUM_BIND_PORT", "8420")),
            tokens=parse_tokens(os.environ.get("LARARIUM_TOKENS", "")),
        )
