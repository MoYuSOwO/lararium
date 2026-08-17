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


def test_format_cache_log_shows_request_count():
    """用量是整轮累加的。带上请求数,免得把工具往返稀释的百分比误读成前缀不稳定。"""
    reply = ModelReply(
        text="好的",
        tool_events=[],
        cache_hit_tokens=1344,
        prompt_tokens=2497,
        completion_tokens=207,
        requests=2,
    )
    assert "2 请求" in format_cache_log(reply)


def test_format_cache_log_handles_unknown_cache_stats():
    reply = ModelReply(
        text="好的", tool_events=[], cache_hit_tokens=None, prompt_tokens=1000, completion_tokens=50
    )
    assert "未知" in format_cache_log(reply)
