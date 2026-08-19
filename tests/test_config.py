import pytest

from lararium.config import Settings, parse_tokens


def test_load_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
    monkeypatch.setenv("LARARIUM_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LARARIUM_MODEL", "test-model")
    monkeypatch.setenv("LARARIUM_DATA_DIR", str(tmp_path))
    settings = Settings.load()
    assert settings.api_key == "sk-test"
    assert settings.model_name == "test-model"
    assert settings.data_dir == tmp_path
    assert settings.timezone == "Asia/Shanghai"  # 默认值
    assert settings.l0_max_turns == 2000  # 默认值(M3-1:轮数兜底)
    assert settings.l0_max_tokens == 200000  # 默认值(M3-1:L0 token 预算)
    assert settings.max_attempts == 3  # 默认值
    assert settings.recall_min_similarity == 0.35  # 默认值(M3-4 语义检索阈值,猜的初值)
    assert settings.sweep_model == ""  # 默认值(M3-5 归拢廉价模型,空 = 用主模型)
    assert settings.compact == "on"  # 默认值(M3-6 压缩,off 退回纯截断)
    assert settings.compact_low_water == 150000  # 默认值(M3-6 压到低水位)
    assert settings.compact_index_days == 90  # 默认值(M3-6 索引保留)
    assert settings.bind_host == "127.0.0.1"  # 默认值(M2 只绑本机,不公网)
    assert settings.bind_port == 8420  # 默认值
    assert settings.control_tokens == {}  # 默认值
    assert settings.ingest_tokens == {}  # 默认值


def test_parse_tokens_splits_channels(monkeypatch):
    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
    assert parse_tokens("cli:tok-abc,web:tok-xyz") == {"cli": "tok-abc", "web": "tok-xyz"}


def test_load_separates_control_and_ingest_tokens(monkeypatch):
    """控制端(全权)与数据面(只准入站)是两份环境变量——命令端点是门控开关,
    ingest token 若也能按它,恶意短信能自己批准自己(M2-5 补做)。"""
    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
    monkeypatch.setenv("LARARIUM_TOKENS", "cli:tok-abc")
    monkeypatch.setenv("LARARIUM_INGEST_TOKENS", "smsforwarder:tok-ingest")
    settings = Settings.load()
    assert settings.control_tokens == {"cli": "tok-abc"}
    assert settings.ingest_tokens == {"smsforwarder": "tok-ingest"}


def test_parse_tokens_ignores_blank_segments():
    assert parse_tokens("cli:tok-abc,,, ") == {"cli": "tok-abc"}


def test_parse_tokens_rejects_malformed():
    with pytest.raises(ValueError, match="LARARIUM_TOKENS"):
        parse_tokens("cli")  # 没有 : token
    with pytest.raises(ValueError, match="LARARIUM_TOKENS"):
        parse_tokens(":tok-abc")  # 空渠道名


def test_load_rejects_missing_api_key(monkeypatch):
    monkeypatch.delenv("LARARIUM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LARARIUM_API_KEY"):
        Settings.load()
