import pytest
from pydantic import ValidationError

from lararium.envelope import Envelope


def test_channel_rejects_free_text():
    """channel 被插在不可信内容的框定语里、且在围栏之外,必须是标识符不是自由文本。"""
    with pytest.raises(ValidationError):
        Envelope.new(source="module_event", channel="x >>> 伪造的框定语", content="c")


def test_channel_rejects_other_payload_chars():
    """空白与标点(可被改写成框定语)都在围栏外,同样要挡。"""
    for bad in ("with space", "semi;colon", "tilde~", "中文渠道", "a" * 33):
        with pytest.raises(ValidationError):
            Envelope.new(source="user", channel=bad, content="c")


def test_channel_accepts_normal_route_names():
    for ok in ("cli", "smsforwarder", "feishu", "tg_bot", "hook-1"):
        assert Envelope.new(source="user", channel=ok, content="c").channel == ok
