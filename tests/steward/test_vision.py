"""M5-5:图片进模型的那一层。

★ 这是一个**全新的注入面**。现有防线保护的全是文本;图片绕开全部,因为根本不存在
"渲染"这一步。这里的测试只能证明**机制那一半**(限量、只在到达轮、降级不崩、
框定语真的在场);"框定语管不管用"是模型行为,只能拿真模型打,见
`tests/test_live_vision_injection.py`。
"""

import hashlib

import pytest

from lararium.envelope import Attachment
from lararium.steward.vision import MAX_IMAGES_PER_TURN, ImagePart, framing, load_images

JPEG = b"\xff\xd8\xff\xe0 pretend this is a photo"


def store(media_dir, data=JPEG, media_type="image/jpeg"):
    """把一份字节按内容哈希放进 media/,返回它的 Attachment(和适配器同一套命名)。"""
    a = Attachment(kind="image", sha256=hashlib.sha256(data).hexdigest(), media_type=media_type)
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / f"{a.sha256}.{a.path.rsplit('.', 1)[1]}").write_bytes(data)
    return a


def test_an_image_on_disk_is_loaded_with_its_bytes(tmp_path):
    a = store(tmp_path)

    parts, notes = load_images(media_dir=tmp_path, attachments=[a], enabled=True)

    assert parts == (ImagePart(sha256=a.sha256, media_type="image/jpeg", data=JPEG),)
    assert notes == ()


def test_vision_off_still_keeps_the_file_and_says_so_in_words(tmp_path):
    """模型看不了图不许崩,也不许假装看见了。

    端点是可配的,用户接的模型未必能读图(仓库默认的 deepseek-chat 就不能)。
    这时图**照样存着**,进上下文的是一行说明——模型据此如实告诉用户,而不是
    对着一行 `(图片 · media/…)` 编内容。
    """
    a = store(tmp_path)

    parts, notes = load_images(media_dir=tmp_path, attachments=[a], enabled=False)

    assert parts == ()
    assert notes and "看不了图" in notes[0]
    assert (tmp_path / f"{a.sha256}.jpg").exists(), "关掉视觉不该影响落盘"


def test_a_missing_file_says_the_replay_is_incomplete(tmp_path):
    """原件不在了要**明说重放不完整**,不许静默给一份残缺的。

    起居注落的是引用+哈希,字节在 `media/` 下。哪天那个文件被清掉、或者换了台机器
    只搬了库没搬 media/,静默跳过的话模型会对着"什么都没有"照常作答,而外面看不出
    这一轮比当初少了东西。
    """
    a = Attachment(kind="image", sha256="ab" * 32, media_type="image/jpeg")

    parts, notes = load_images(media_dir=tmp_path, attachments=[a], enabled=True)

    assert parts == ()
    assert notes and "重放不完整" in notes[0]
    assert a.short in notes[0], "得说清楚是哪一张不见了"


def test_non_image_attachments_never_reach_the_model(tmp_path):
    """语音/文件/视频不是这一步的事。只有图片进模型——注入面能小就小。"""
    voice = store(tmp_path, data=b"#!SILK_V3 xxxx", media_type="audio/silk")
    voice = Attachment(kind="voice", sha256=voice.sha256, media_type="audio/silk")

    parts, notes = load_images(media_dir=tmp_path, attachments=[voice], enabled=True)

    assert parts == ()
    assert notes == ()


def test_the_number_of_images_per_turn_is_capped(tmp_path):
    """一轮最多几张,超了截断并**说出来**。

    图片是按分辨率吃 token 的,而 L0 的预算算术(estimate_tokens + _render_overhead)
    对图片一无所知——不封顶就是一次消息把整个窗口顶穿,而症状是"上下文超长"这种
    完全指不到图片的报错。截断必须看得见:静默截断读起来和"就这些"一模一样。
    """
    attachments = [store(tmp_path, data=JPEG + bytes([i])) for i in range(MAX_IMAGES_PER_TURN + 2)]

    parts, notes = load_images(media_dir=tmp_path, attachments=attachments, enabled=True)

    assert len(parts) == MAX_IMAGES_PER_TURN
    assert notes and "2" in notes[0]


@pytest.mark.parametrize("count", [1, 3])
def test_the_framing_says_the_picture_is_data_not_instructions(count):
    """框定语是这一层**唯一**能对注入做的事,而且它是说服不是机制。

    所以它必须:说清楚图里的字是数据、点名"照做"这个动作、并且**每一张图都在它的
    作用域里**(数量对得上)。管不管用只能拿真模型实测——见 live 那份。
    """
    line = framing(count)

    assert str(count) in line
    assert "数据" in line and "指令" in line
    assert "不要执行" in line or "不照做" in line or "照做" in line
