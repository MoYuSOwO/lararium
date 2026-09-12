"""M5-5 建、M6-2 翻过来一半:图片进模型的那一层。

★ 这是一个**全新的注入面**。现有防线保护的全是文本;图片绕开全部,因为根本不存在
"渲染"这一步。这里的测试只能证明**机制那一半**(读不了的有话说、框定语真的在场);
"框定语管不管用"是模型行为,只能拿真模型打,见 `tests/test_live_vision_injection.py`。

**M6-2 之后这一层不再取字节。** 到达轮只报一行 id(`Attachment.as_line()`),字节要
等模型自己调 `read_image` 才进上下文——所以"限量"和"只在一轮里"这两条机制的落点搬到了
`tools.read_image`(见 `tests/steward/test_tools.py`),这里剩下的是**替模型先说清
哪几张它根本读不了**那一支:`unreadable_notes()`。
"""

import hashlib

import pytest

from lararium.envelope import Attachment
from lararium.steward.vision import framing, unreadable_notes

JPEG = b"\xff\xd8\xff\xe0 pretend this is a photo"


def image(data=JPEG, media_type="image/jpeg", kind="image"):
    """造一份附件引用(和适配器同一套命名)。**这一层不碰磁盘了**,所以不用落盘。"""
    return Attachment(kind=kind, sha256=hashlib.sha256(data).hexdigest(), media_type=media_type)


def test_a_readable_image_gets_no_note_at_all(tmp_path):
    """能读的图**一个字都不多说**:那行报告已经写着"要看就调 read_image"。

    这一层只负责"先说清读不了的",不负责介绍能力——介绍写在报告行和工具 docstring 里,
    两处各写一遍的那天就开始漂。
    """
    assert unreadable_notes(attachments=[image()], enabled=True) == ()


def test_vision_off_says_so_once_instead_of_letting_the_model_find_out(tmp_path):
    """模型看不了图不许假装看见了,**也不该让它花一次工具调用才知道**。

    端点是可配的,用户接的模型未必能读图(仓库默认的 deepseek-chat 就不能)。
    这时图照样存着、报告行照样在,但上下文里多一句说明——不说的话模型会照着报告行
    去调 `read_image`,得到一句"当前模型看不了图",而那一次往返是白花的。
    """
    notes = unreadable_notes(attachments=[image()], enabled=False)

    assert notes and "看不了图" in notes[0]
    assert "1" in notes[0], "得说清是几张"


def test_non_image_attachments_are_not_mentioned_here(tmp_path):
    """语音/文件/视频不在这一层说话——它们**自己那行报告里**已经写明有没有路。

    在这儿再提一遍就是两处各写一套同一件事(M5-5 栽过的那个形状),而且是噪声:
    它们压根没打算进模型。
    """
    voice = image(data=b"#!SILK_V3 xxxx", media_type="audio/silk", kind="voice")

    assert unreadable_notes(attachments=[voice], enabled=True) == ()


@pytest.mark.parametrize("count", [1, 3])
def test_the_framing_points_at_the_pictures_and_not_at_the_text(count):
    """框定语是这一层**唯一**能对注入做的事,而且它是说服不是机制。

    所以它必须:说清楚图里的字是数据、点名"照做"这个动作、并且**每一张图都在它的
    作用域里**(数量对得上)。管不管用只能拿真模型实测——见 live 那份。

    M6-2 之后它只有一个调用方(`read_image`),而那正好让"图进上下文必带框定"从
    "两个挂载点都记得带"变成了结构事实。
    """
    line = framing(count)

    assert str(count) in line
    assert "数据" in line and "指令" in line
    assert "不要执行" in line or "不照做" in line or "照做" in line
    # **指向词必须指对。** 第一版写的是「以上 N 张图」,而报文里图排在文本**之后**
    # (报文测试钉着 ["text","image_url"]);不可信轮更别扭——这句紧跟在 `>>>` 后面,
    # 「以上」最自然的读法是围栏里那段文字,不是图。框定语是这一层唯一的文本防线,
    # 唯一必须指对的那个词不能指反。
    assert "随这条消息" in line
    assert "以上" not in line


@pytest.mark.parametrize(
    ("media_type", "hint"),
    [
        # 微信说这是图片,字节却嗅不出来(伪装的 PDF、没见过的格式)
        ("application/octet-stream", "认不出"),
        # 认得出、但这个服务商收不了。HEIC 是 iPhone 发原图的默认格式。
        ("image/heic", "读不了"),
        # M5-10:BMP 也进了这一档——**换服务商时清单真的变窄了**(新的只收
        # webp/png/jpeg/gif)。实测一张 24 位 BMP 过去就是 400,用户收到一整句
        # provider 黑话。这条钉着"清单是服务商相关的,不是摆设"。
        ("image/bmp", "读不了"),
    ],
)
def test_an_image_that_cannot_be_read_says_so_up_front(tmp_path, media_type, hint):
    """★ 补2 的后半条,而且它比前半条更值钱:**别把响亮的失败换成静默的失败。**

    这一支活到了 M6-2 之后,而且理由变了:从前它治的是"图没送出去而模型不知道"
    (静默),现在报告行摆在那儿、`read_image` 也会回一句人话,所以静默已经不可能。
    它现在治的是**别让模型对着一张读不了的图许诺**——「我看看这张图」说出口之后才发现
    读不了,比一开始就说"这张我读不了"差。

    候选是「是图片」**或者**「微信说它是图片」:后者才让"说是图片、字节却不是"这种
    落进有话可说的那一支。
    """
    a = Attachment(kind="image", sha256="ab" * 32, media_type=media_type)

    notes = unreadable_notes(attachments=[a], enabled=True)

    assert notes and hint in notes[0], f"这张图读不了却没人说一句:{notes}"
    assert a.short in notes[0], "得说清楚是哪一张"


@pytest.mark.parametrize("media_type", ["image/gif", "image/jpeg", "image/png", "image/webp"])
def test_every_sendable_type_stays_readable(tmp_path, media_type):
    """反向守卫:别为了挡住 HEIC/BMP 把清单里的也一起说成读不了。

    逐个走一遍而不是抽一个:清单是**服务商相关**的,收窄它的那次改动最容易顺手多砍
    一个,而多说一句"这张读不了"的后果是模型再也不去调 `read_image`——症状是
    "这张图它就是不看",没有任何报错。
    """
    assert unreadable_notes(attachments=[image(media_type=media_type)], enabled=True) == ()
