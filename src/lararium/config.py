import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_base_url: str
    model_name: str
    data_dir: Path
    timezone: str
    l0_max_turns: int
    max_attempts: int

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
        )
