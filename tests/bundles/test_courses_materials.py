"""M6-6b 学习 bundle 的课件那半:`add_file` / `list_materials`,以及改名、删除、撤回带上归属。

**只记归属,不拷字节**:bundle 按 §5 数据产权摸不到媒体池,也 import 不到 Steward。所以
这边能核对的只有 id 的**形状**;id 在不在池子里、是不是 PDF,读的时候由 Steward 侧的
`read_pdf` / `read_image` 说出来(那两条在 `tests/steward/test_tools.py`)。

归属是表不是文件——「用户会亲手改的用文件,不会亲手改的用表」(PLAN 的判据)。
表里一行的课程键就是**课程目录相对课程根的路径**:活着的课是「线性代数」,回收站里的是
「.trash/线性代数-<时间戳>」。于是"改名 / 删除 / 撤回搬整个目录"在表这一侧的对应物,
就是把那一列从一个路径改成另一个路径——下面这些测试钉的就是这件事一处都没漏(G8)。
"""

import hashlib
import inspect
from pathlib import Path

import pytest
from bundles.courses.server import build

SHANGHAI = "Asia/Shanghai"
PDF_ID = "77aa99bb00cc"
OTHER_ID = "5a6b7c8d9e0f"


@pytest.fixture
def runtime(tmp_path):
    return build(tmp_path, timezone=SHANGHAI)


@pytest.fixture
def tools(runtime):
    return {f.__name__: f for f in runtime.tools}


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "courses"


def rows(listing: str) -> list[str]:
    return [line for line in listing.splitlines() if line.startswith("- ")]


def files(folder: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()
    }


# ── add_file:归到课下,只记归属 ─────────────────────────────────────────


def test_a_filed_pdf_is_listed_with_its_name_and_id(tools):
    tools["append_to_note"]("线性代数", "第一章\n")

    out = tools["add_file"]("线性代数", PDF_ID, "第3讲")
    listing = tools["list_materials"]("线性代数")

    assert "第3讲" in out and PDF_ID in out
    assert rows(listing) == [f"- 「第3讲」 · id {PDF_ID}"], listing


def test_filing_copies_no_bytes_anywhere(tools, root, tmp_path):
    """★ **不拷贝**:课程目录里一个字节都不多,媒体池一个文件都不动。"""
    tools["append_to_note"]("线性代数", "第一章\n")
    media = tmp_path / "media"
    media.mkdir()
    blob = b"%PDF-1.4 lecture"
    (media / f"{hashlib.sha256(blob).hexdigest()}.pdf").write_bytes(blob)
    course_before, media_before = files(root / "线性代数"), files(media)

    tools["add_file"]("线性代数", hashlib.sha256(blob).hexdigest()[:12], "第3讲")

    assert files(root / "线性代数") == course_before
    assert files(media) == media_before


def test_filing_into_a_new_course_says_it_created_one(tools):
    """打错课程名的唯一防线是**说一声**(同 append_to_note):新课是 add_file 建出来的,
    回话里就得有「新建」,而且 list_courses 从此看得见它。"""
    out = tools["add_file"]("线性待数", PDF_ID, "第3讲")

    assert "新建" in out, out
    assert "线性待数" in tools["list_courses"]()
    assert rows(tools["list_materials"]("线性待数")) == [f"- 「第3讲」 · id {PDF_ID}"]


def test_filing_into_an_existing_course_does_not_claim_to_create_it(tools):
    tools["append_to_note"]("线性代数", "第一章\n")
    assert "新建" not in tools["add_file"]("线性代数", PDF_ID, "第3讲")


def test_the_reply_does_not_pretend_the_file_was_checked(tools):
    """这边核对不了文件在不在、是什么——回话不许说得像核对过,得说清"读的时候才知道"。"""
    out = tools["add_file"]("线性代数", PDF_ID, "第3讲")
    assert "读的时候" in out, out


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "ab",
        "AB12CD34EF56",
        "ab*",
        "../../etc/passwd",
        f"{PDF_ID}\n- 「伪造」 · id deadbeefdead",
        "猫",
    ],
)
def test_a_media_id_in_the_wrong_shape_is_refused_and_nothing_is_filed(tools, root, bad_id):
    """id 的形状和 read_image / read_pdf 是**同一个常量**(`envelope.MEDIA_ID_RE`)。
    形状不对就不归:归进去的话,读的那一侧只会回「认不出」,而那时已经隔了好几轮。"""
    tools["append_to_note"]("线性代数", "第一章\n")

    out = tools["add_file"]("线性代数", bad_id, "第3讲")

    assert "认不出" in out, out
    assert "\n" not in out, "回显把模型给的换行原样带出来了"
    assert rows(tools["list_materials"]("线性代数")) == []


@pytest.mark.parametrize(
    ("bad_name", "words"),
    [("", "空"), ("   ", "空"), ("讲" * 61, "太长"), ("第3讲\x00", "控制字符")],
)
def test_a_bad_name_is_refused(tools, bad_name, words):
    out = tools["add_file"]("线性代数", PDF_ID, bad_name)

    assert words in out, out
    assert "没有「线性代数」这门课" in tools["list_materials"]("线性代数"), "拒了却把课建出来了"


def test_a_name_is_a_display_name_not_a_path(tools):
    """★ 课件的名字**只是给人看的**,不是文件名——不拷贝,就没有落点。所以课程名那套
    路径白名单(拒 `/`、`..`)**不套在这里**(G7):「2026/9/13 讲义」是个正常的名字。"""
    out = tools["add_file"]("线性代数", PDF_ID, "2026/9/13 讲义")

    assert rows(tools["list_materials"]("线性代数")) == [f"- 「2026/9/13 讲义」 · id {PDF_ID}"], out


def test_a_name_cannot_forge_another_row(tools):
    """名字要渲染进一行一条的列表:换行折掉、「」中和掉,伪造不出第二行、也提前闭合不了引号。"""
    tools["add_file"]("线性代数", PDF_ID, "第3讲」 · id deadbeefdead\n- 「假的")

    listing = rows(tools["list_materials"]("线性代数"))

    assert len(listing) == 1, listing
    assert listing[0].count("」") == 1 and listing[0].endswith(f"」 · id {PDF_ID}"), listing


def test_the_same_name_twice_in_one_course_is_refused(tools):
    """同名拒绝:两份叫「第3讲」的课件,之后谁也说不清指的是哪份。"""
    tools["add_file"]("线性代数", PDF_ID, "第3讲")

    out = tools["add_file"]("线性代数", OTHER_ID, "第3讲")

    assert "已经有" in out, out
    assert rows(tools["list_materials"]("线性代数")) == [f"- 「第3讲」 · id {PDF_ID}"]


def test_the_same_file_twice_in_one_course_is_refused(tools):
    tools["add_file"]("线性代数", PDF_ID, "第3讲")

    out = tools["add_file"]("线性代数", PDF_ID, "第三讲")

    assert "第3讲" in out and "已经" in out, out
    assert rows(tools["list_materials"]("线性代数")) == [f"- 「第3讲」 · id {PDF_ID}"]


def test_filing_refuses_an_illegal_course_name(tools, root):
    before = files(root)
    for bad in ("", "../x", ".trash", "a/b"):
        out = tools["add_file"](bad, PDF_ID, "第3讲")
        assert out.strip() and "归好" not in out, out
    assert files(root) == before


def test_what_was_filed_survives_a_restart(tmp_path):
    """归属在 bundle 自己的库里,不在内存里:重新 build 一次(=重启)照样列得出来。"""
    first = {f.__name__: f for f in build(tmp_path, timezone=SHANGHAI).tools}
    first["add_file"]("线性代数", PDF_ID, "第3讲")

    again = {f.__name__: f for f in build(tmp_path, timezone=SHANGHAI).tools}

    assert rows(again["list_materials"]("线性代数")) == [f"- 「第3讲」 · id {PDF_ID}"]


# ── list_materials ─────────────────────────────────────────────────────


def test_listing_a_course_that_does_not_exist_says_so(tools):
    assert "没有「量子力学」这门课" in tools["list_materials"]("量子力学")


def test_listing_a_course_with_no_materials_says_how_to_file_one(tools):
    tools["append_to_note"]("线性代数", "第一章\n")

    out = tools["list_materials"]("线性代数")

    assert rows(out) == [] and "add_file" in out, out


def test_the_listing_says_which_tool_reads_what(tools):
    """★ 这边**不标类型**(核对不了;让模型填一个 kind 就是把"猜"写成一个看着很确定的
    标签),所以列表那句话要说清两条路,以及拿不准时先走哪条——read_pdf 碰上图片会指路。"""
    tools["add_file"]("线性代数", PDF_ID, "第3讲")

    out = tools["list_materials"]("线性代数")

    assert "read_pdf" in out and "read_image" in out, out


def test_materials_are_listed_in_the_order_they_were_filed(tools):
    """按归档顺序列:「第10讲」按码位排会跑到「第2讲」前面,而用户是按讲次发的。"""
    filed = ["第1讲", "第2讲", "第15讲"]  # 按码位排是 第15讲 < 第1讲 < 第2讲
    for n, name in enumerate(filed):
        tools["add_file"]("线性代数", f"aa{n:010x}", name)

    names = [
        line.split("「")[1].split("」")[0] for line in rows(tools["list_materials"]("线性代数"))
    ]

    assert names == filed


def test_the_listing_is_paged_and_clamped(tools):
    for n in range(35):
        tools["add_file"]("线性代数", f"{n:012x}", f"第{n}讲")

    first = tools["list_materials"]("线性代数")
    second = tools["list_materials"]("线性代数", 2)

    assert "35 份" in first and "第 1/2 页" in first, first
    assert len(rows(first)) == 30 and len(rows(second)) == 5
    assert tools["list_materials"]("线性代数", 999) == second
    assert tools["list_materials"]("线性代数", 0) == first


# ── 同一份归到两门课 ────────────────────────────────────────────────────


def test_one_file_in_two_courses_is_listed_in_both_under_each_name(tools):
    tools["add_file"]("线性代数", PDF_ID, "第3讲 矩阵")
    tools["add_file"]("数值分析", PDF_ID, "参考:矩阵分解")

    assert rows(tools["list_materials"]("线性代数")) == [f"- 「第3讲 矩阵」 · id {PDF_ID}"]
    assert rows(tools["list_materials"]("数值分析")) == [f"- 「参考:矩阵分解」 · id {PDF_ID}"]


def test_taking_one_course_away_does_not_touch_the_other(tools):
    """★ 从一门课里去掉(这一轮去掉的路就是删这门课)**不影响另一门**,撤回也只回到自己那门。"""
    tools["add_file"]("线性代数", PDF_ID, "第3讲 矩阵")
    tools["add_file"]("数值分析", PDF_ID, "参考:矩阵分解")

    tools["delete_course"]("线性代数", "退课了")
    assert rows(tools["list_materials"]("数值分析")) == [f"- 「参考:矩阵分解」 · id {PDF_ID}"]

    tools["delete_course"]("线性代数", undo=True)
    assert rows(tools["list_materials"]("线性代数")) == [f"- 「第3讲 矩阵」 · id {PDF_ID}"]
    assert rows(tools["list_materials"]("数值分析")) == [f"- 「参考:矩阵分解」 · id {PDF_ID}"]


# ── 改名 / 删除 / 撤回:目录搬到哪,归属跟到哪 ────────────────────────────


def test_renaming_a_course_carries_its_materials(tools):
    """★ 硬口径:改名之后 `list_materials(新名字)` 还列得出来。
    这是 6a「整个目录一起搬」在表这一侧的对应物——漏了就是改名后课件凭空消失。"""
    tools["add_file"]("线性待数", PDF_ID, "第3讲")

    out = tools["rename_course"]("线性待数", "线性代数")

    assert rows(tools["list_materials"]("线性代数")) == [f"- 「第3讲」 · id {PDF_ID}"]
    assert "没有「线性待数」这门课" in tools["list_materials"]("线性待数")
    assert "1 份课件" in out, out


def test_a_refused_rename_merges_no_materials(tools):
    """新名字已占 → 拒绝,**两边的课件也各是各的**(合并比重名更坏,课件一样)。"""
    tools["add_file"]("线性待数", PDF_ID, "甲")
    tools["add_file"]("线性代数", OTHER_ID, "乙")

    tools["rename_course"]("线性待数", "线性代数")

    assert rows(tools["list_materials"]("线性待数")) == [f"- 「甲」 · id {PDF_ID}"]
    assert rows(tools["list_materials"]("线性代数")) == [f"- 「乙」 · id {OTHER_ID}"]


def test_a_deleted_course_lists_no_materials_even_after_the_name_is_reused(tools):
    """★ G8 点名的那条路:删掉「大学物理」→ 同名再建一门 → **旧课件不许冒出来**。

    只看"删完之后列不出"挡不住这个——课程目录没了,不管表改没改都列不出;
    同名重建之后才分得出表到底跟没跟着走。
    """
    tools["add_file"]("大学物理", PDF_ID, "第3讲")
    tools["delete_course"]("大学物理", "退课了")
    assert "没有「大学物理」这门课" in tools["list_materials"]("大学物理")

    tools["append_to_note"]("大学物理", "重新选了这门课\n")

    assert rows(tools["list_materials"]("大学物理")) == []


def test_undo_brings_the_materials_back(tools):
    """★ 硬口径:删了之后列不出,撤回之后又列得出来。"""
    tools["add_file"]("大学物理", PDF_ID, "第3讲")
    tools["delete_course"]("大学物理", "退课了")

    out = tools["delete_course"]("大学物理", undo=True)

    assert rows(tools["list_materials"]("大学物理")) == [f"- 「第3讲」 · id {PDF_ID}"]
    assert "1 份课件" in out, out


def test_undo_takes_back_the_materials_of_the_latest_deletion_only(tools):
    """同一门课删两次:回收站两份各带各的课件,撤回拿最近那份,**早的那份的课件不混进来**。"""
    tools["add_file"]("大学物理", PDF_ID, "第一版的课件")
    tools["delete_course"]("大学物理", "第一次删")
    tools["add_file"]("大学物理", OTHER_ID, "第二版的课件")
    tools["delete_course"]("大学物理", "第二次删")

    tools["delete_course"]("大学物理", undo=True)

    assert rows(tools["list_materials"]("大学物理")) == [f"- 「第二版的课件」 · id {OTHER_ID}"]


def test_the_delete_reply_says_the_materials_went_with_it(tools):
    tools["add_file"]("大学物理", PDF_ID, "第3讲")
    assert "1 份课件" in tools["delete_course"]("大学物理", "退课了")


def test_a_course_without_materials_keeps_the_old_replies(tools):
    """没有课件的课,改名 / 删除 / 撤回的回话**一个字不变**(6a 的行为不许动)。"""
    tools["append_to_note"]("线性待数", "x\n")
    assert tools["rename_course"]("线性待数", "线性代数") == (
        "「线性待数」改名成「线性代数」了。整个课程目录一起搬的,内容一个字节没动。"
    )
    assert tools["delete_course"]("线性代数", undo=True) == (
        "「线性代数」没删过,不用恢复。list_courses 看看现在有哪些。"
    )
    tools["delete_course"]("线性代数", "退课了")
    assert tools["delete_course"]("线性代数", undo=True) == "「线性代数」拿回来了,一个字节没变。"


# ── 接口形状 ────────────────────────────────────────────────────────────


def test_no_docstring_claims_the_materials_can_be_searched(tools):
    """★ 这一轮**不转文字、不建缓存、不搜课件**(那是 6c,要用户拍板)。所以两个新工具的
    docstring 不许暗示"能搜课件内容"——没有任何工具声称能搜,就没有"搜不全 = 静默错"。"""
    for name in ("add_file", "list_materials"):
        doc = inspect.getdoc(tools[name]) or ""
        assert "search_notes" not in doc, name
        assert "可搜" not in doc and "能搜" not in doc and "搜得到" not in doc, name
    assert "不在任何搜索" in (inspect.getdoc(tools["list_materials"]) or "")
