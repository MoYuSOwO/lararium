from types import SimpleNamespace

from lararium.steward.model import ModelReply, extract_cache_hit_tokens, format_cache_log


def test_extract_cache_hit_from_deepseek_field():
    usage = SimpleNamespace(details={"prompt_cache_hit_tokens": 1536})
    assert extract_cache_hit_tokens(usage) == 1536


def test_extract_cache_hit_from_openai_style_field():
    usage = SimpleNamespace(details={"cached_tokens": 900})
    assert extract_cache_hit_tokens(usage) == 900


def test_extract_cache_hit_returns_none_when_absent():
    assert extract_cache_hit_tokens(SimpleNamespace(details={})) is None
    assert extract_cache_hit_tokens(SimpleNamespace()) is None


def test_format_cache_log_reports_hit_rate():
    reply = ModelReply(
        text="好的", tool_events=[], cache_hit_tokens=800, prompt_tokens=1000, completion_tokens=50
    )
    line = format_cache_log(reply)
    assert "800/1000" in line
    assert "80.0%" in line


def test_format_cache_log_handles_unknown_cache_stats():
    reply = ModelReply(
        text="好的", tool_events=[], cache_hit_tokens=None, prompt_tokens=1000, completion_tokens=50
    )
    assert "未知" in format_cache_log(reply)
