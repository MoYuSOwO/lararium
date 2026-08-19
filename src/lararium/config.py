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
    l0_max_tokens: int
    max_attempts: int
    recall_min_similarity: float
    sweep_model: str
    compact: str
    compact_low_water: int
    compact_index_days: int
    bind_host: str
    bind_port: int
    control_tokens: dict[str, str]
    ingest_tokens: dict[str, str]

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
            # M3-1:L0 按 token 预算截断,l0_max_turns 只当轮数兜底(M3 前默认 30 太小)。
            l0_max_turns=int(os.environ.get("LARARIUM_L0_MAX_TURNS", "2000")),
            l0_max_tokens=int(os.environ.get("LARARIUM_L0_MAX_TOKENS", "200000")),
            max_attempts=int(os.environ.get("LARARIUM_MAX_ATTEMPTS", "3")),
            # M3-4:语义检索相似度阈值。0.35 是猜的初值(2026-08-18 实测命中 0.44~0.58、
            # 未命中 0.35),真机跑几天要按实际分布调。
            recall_min_similarity=float(os.environ.get("LARARIUM_RECALL_MIN_SIMILARITY", "0.35")),
            # M3-5 夜间归拢(sweep)的廉价模型,单配;空则用主模型。归拢是扫历史做剪枝,
            # 不需要主模型那么强,便宜够用就行。
            sweep_model=os.environ.get("LARARIUM_SWEEP_MODEL", ""),
            # M3-6 压缩(M3 最后一块硬骨头)。整窗 200k、低水位 150k、索引保留 90 天,
            # 口径一律 estimate_tokens + _render_overhead(渲染后形态,M3-1b/M3-3 定死)。
            compact=os.environ.get("LARARIUM_COMPACT", "on"),  # on | off(off 退回纯截断)
            compact_low_water=int(os.environ.get("LARARIUM_COMPACT_LOW_WATER", "150000")),
            compact_index_days=int(os.environ.get("LARARIUM_COMPACT_INDEX_DAYS", "90")),
            bind_host=os.environ.get("LARARIUM_BIND_HOST", "127.0.0.1"),
            bind_port=int(os.environ.get("LARARIUM_BIND_PORT", "8420")),
            # 控制端(你):全权,四个端点都能碰。数据面来源(短信/网页):只准入站。
            # 命令端点是门控的开关——ingest token 若也能按它,恶意短信进站后能自己批准
            # 自己(攻击链不需要攻破模型),门控整个溶掉。所以两种 token 分开配。
            control_tokens=parse_tokens(os.environ.get("LARARIUM_TOKENS", "")),
            ingest_tokens=parse_tokens(os.environ.get("LARARIUM_INGEST_TOKENS", "")),
        )
