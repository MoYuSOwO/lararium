"""注入图字库的判据(M5-9)。

这里钉的是**选暗号的标准**,不是暗号本身好不好用——好不好用只有真模型说了算
(`tests/test_live_vision_injection.py`)。但标准错了,选出来的暗号就只是运气:
M5-5 写的是「ZORVAX 六个字母在 5x7 点阵里两两不相似」,而模型输出的是**整个字母表**,
不是那六个字母。按对的判据量,`ZORVAX` 最弱一环是 `O`(距离 2/35),
和第一版被否掉的 `PWNED9` 好不了多少——实测 DeepSeek 上 1/3。
"""

from tests.injection_image import _FONT
from tests.test_live_vision_injection import CANARY

# 一个字形 35 位(5 x 7)。低于这个距离的字符,缩图之后就得靠运气。
MIN_HAMMING = 9


def _bits(ch: str) -> str:
    return "".join(_FONT[ch])


def _closest(ch: str) -> tuple[int, str]:
    """这个字符和**字库里任意其他字符**的最小汉明距离,以及最像谁。"""
    return min(
        (sum(a != b for a, b in zip(_bits(ch), _bits(o), strict=True)), o)
        for o in _FONT
        if o != ch and o != " "
    )


def test_every_canary_character_is_far_from_every_other_glyph():
    """★ 判据:比的是**与整个字库**的最小距离,不是暗号内部两两之间。

    距离 ≥9 的字符全字库只有 `J K X Y 4` 五个,暗号只能从这里挑。
    失败信息里带上"最像谁",免得下一个人还得自己去算。
    """
    weak = {ch: _closest(ch) for ch in set(CANARY) if _closest(ch)[0] < MIN_HAMMING}

    assert not weak, f"暗号里有认不牢的字符(距离/最像谁):{weak}"


def test_the_criterion_is_not_vacuous():
    """阳性对照:这条判据得真能否掉东西。

    否不掉的话它就是一句好听的话——`ZORVAX` 必须被它挡下来(那个 `O` 距离 2),
    而距离 ≥9 的白名单必须小(全字库只有五个字符够格)。
    """
    assert any(_closest(ch)[0] < MIN_HAMMING for ch in "ZORVAX"), "老暗号居然过了,判据是空的"
    assert sorted(ch for ch in _FONT if ch != " " and _closest(ch)[0] >= MIN_HAMMING) == [
        "4",
        "J",
        "K",
        "X",
        "Y",
    ]
