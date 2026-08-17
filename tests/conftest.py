import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_lararium_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉宿主环境里所有 LARARIUM_* 变量,让测试只看见自己设的值。"""
    for key in list(os.environ):
        if key.startswith("LARARIUM_"):
            monkeypatch.delenv(key, raising=False)
