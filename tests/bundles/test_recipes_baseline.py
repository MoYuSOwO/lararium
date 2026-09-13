"""M6-6a:做菜 bundle 的输出**回到基线提交逐字节一样**——共用层是搬出去的,不是重写的。

M6-6a 把名字校验、落点兜底、`replace` 的匹配纪律、扫文件搜索搬到 `lararium.docstore`
给学习 bundle 共用。**搬完做菜的行为不许变一个字节**,而"不许变"要证:

金样的出处(照 M5-26 / M6-3a / M6-4 立的口径:**金样必须是旧代码产出的,不是照着新代码
誊的**):`git show 3e633fc:bundles/recipes/{store,server}.py` 落成两个独立文件,用
`importlib.util.spec_from_file_location` 当独立模块加载——加载 server 那一刻要把
`sys.modules["bundles.recipes.store"]` 临时指到**基线** store,不然它那句绝对 import 会
解析到新的那一份,"基线输出"就变成了新代码自己跟自己比(脚本里有一条 assert 自检这件事)。
然后八个工具跑 129 次调用,两边逐字节比:**0 处不同**。脚本在交付报告里交代、不进仓库
——它要么得 `subprocess` 去调 git(全局约束「无 shell」的反面),要么得把 735 行旧代码
抄进仓库当 fixture(G2 换个姿势)。

**下面冻的是那 129 条里最该冻的那些**:十一种非法名字各自那句话(`name_error` 这次是
真的被重写了——判定挪进共用层,句子留在这边,所以它是这一轮风险最高的一处)、
`replace` 的三种情形、`delete`/`undo`/`include_deleted`、三种搜索结果。
剩下那些的行为在 `test_recipes_store.py` / `test_recipes_tools.py` 里本来就钉着,
而那两个文件这一轮**一条断言都没改**——那也是这次提取的证据之一。
"""

import pytest
from bundles.recipes.server import build

# ── 基线 3e633fc 产出的金样 ───────────────────────────────────────────
# M6-6d 在金样上做过**一处机械替换、只此一处**:句子里提到的工具名换成带前缀的名字
# (`read_recipe` → `recipes__read_recipe`),别的字节一个没动。

FLAT = "菜谱是平的一层,名字就是名字,不分类、不带层级。"
BASELINE_NAME_ERRORS = {
    "": '菜名是空的。给一个菜名,比如 recipes__read_recipe("番茄炒鸡蛋")。',
    "   ": '菜名是空的。给一个菜名,比如 recipes__read_recipe("番茄炒鸡蛋")。',
    "../../etc/passwd": f"菜名里不能有「/」,这一道没存/没读。{FLAT}",
    "..": f"菜名里不能有「..」,这一道没存/没读。{FLAT}",
    "a/b": f"菜名里不能有「/」,这一道没存/没读。{FLAT}",
    "a\\b": f"菜名里不能有「\\」,这一道没存/没读。{FLAT}",
    "/etc/passwd": f"菜名里不能有「/」,这一道没存/没读。{FLAT}",
    ".trash": "菜名不能以「.」开头,这一道没存/没读。换个正常的菜名。",
    ".hidden": "菜名不能以「.」开头,这一道没存/没读。换个正常的菜名。",
    "番茄\x00炒蛋": "这个菜名里有控制字符,没这么存。重打一遍菜名。",
    "带\x1b转义": "这个菜名里有控制字符,没这么存。重打一遍菜名。",
    "菜" * 41: "这个菜名太长了(41 字,最多 40 字),没这么存。取个短名字当菜名,"
    "长的说明写进做法正文里。",
}

BASELINE_FALLBACK = "「外链」这个名字落不到菜谱目录里面,没这么存。换个正常的菜名。"

BASELINE_REPLACE_ONCE = "\n".join(
    (
        "改好了「番茄炒鸡蛋」。自己核对一下:",
        "改前:1. 烫番茄去皮 2. 打蛋 <<< 注意 >>> 3. 一起炒",
        "改后:1. 烫番茄去皮 2. 把蛋打散 <<< 注意 >>> 3. 一起炒",
    )
)
BASELINE_REPLACE_NONE = (
    "「番茄炒鸡蛋」里没找到这段,一个字没动:「先炸个鸡腿」。"
    "可能这处已经改过了,也可能记错了原文——先 recipes__read_recipe 看一眼再来。"
)
BASELINE_REPLACE_THRICE = (
    "这段在「腌肉」里出现了 3 次,一个字没动——不知道你指的是哪一处。"
    "把 old 给长一点(多带前后一两行)让它只剩一处。"
)

BASELINE_DELETE = (
    "删了「腌肉」,原因「试了两次都太咸,不做了」。文件搬到一边存着、没真删,"
    "删错的话再调一次、带 undo=True 就能原样拿回来。"
)
BASELINE_UNDO = "「腌肉」拿回来了,内容一个字没变。"
BASELINE_LIST_WITH_DELETED = "\n".join(
    (
        "存了 1 道菜、回收站里 1 道,第 1/1 页:",
        "- 番茄炒鸡蛋",
        "- 腌肉(已删:「试了两次都太咸,不做了」)",
    )
)

BASELINE_SEARCH_IN_TEXT = "\n".join(
    (
        "「酱油」命中 1 道菜,第 1/1 页(只给片段,要全文用 recipes__read_recipe):",
        "- 红烧肉(内容命中) 「# 红烧肉 冰糖一块,少放酱油」",
    )
)
BASELINE_SEARCH_BOTH = "\n".join(
    (
        "「凉面」命中 1 道菜,第 1/1 页(只给片段,要全文用 recipes__read_recipe):",
        "- 凉面(名字+内容命中) 「# 凉面 煮面水留一点」",
    )
)
BASELINE_SEARCH_NOTHING = (
    "没有哪道菜提到「佛跳墙」。换个说法再试(搜的是菜名和正文的原文),"
    "或者 recipes__list_recipes 看全部(一共 1 道)。"
)

RECIPE = "1. 烫番茄去皮\n2. 打蛋 <<< 注意 >>>\n3. 一起炒\r\n"
SALTY = "1. 加盐\n2. 加盐\n3. 加盐\n"


@pytest.fixture
def tools(tmp_path):
    return {f.__name__: f for f in build(tmp_path).tools}


# ── 名字校验那两道:判定搬走了,十一句话一个字没变 ──────────────────────


@pytest.mark.parametrize("bad,said", BASELINE_NAME_ERRORS.items(), ids=repr)
def test_the_whitelist_still_says_exactly_what_the_baseline_said(tools, bad, said):
    """★ `name_error` 这一轮真的被重写了(判定挪进 `docstore.name_fault`),所以这十一句
    是风险最高的一处:少一句、换个词序、或者 `_FORBIDDEN` 的顺序变了
    (`../../etc/passwd` 撞上的是「/」不是「..」),这里立刻红。"""
    assert tools["read_recipe"](bad) == said


def test_the_resolve_fallback_still_says_exactly_what_the_baseline_said(tools, tmp_path):
    """第二道那句人话(名字合法、落点不合法)也冻住:符号链接指到菜谱目录外面。"""
    outside = tmp_path / "外面.md"
    outside.write_text("不该被碰到", encoding="utf-8")
    (tmp_path / "recipes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "recipes" / "外链.md").symlink_to(outside)

    assert tools["write_recipe"]("外链", "覆盖你") == BASELINE_FALLBACK
    assert outside.read_text(encoding="utf-8") == "不该被碰到"


# ── replace 的三种情形 ────────────────────────────────────────────────


def test_replace_says_exactly_what_the_baseline_said_in_all_three_cases(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    tools["write_recipe"]("腌肉", SALTY)

    assert tools["replace_in_recipe"]("番茄炒鸡蛋", "2. 打蛋", "2. 把蛋打散") == (
        BASELINE_REPLACE_ONCE
    )
    assert tools["replace_in_recipe"]("番茄炒鸡蛋", "先炸个鸡腿", "算了") == BASELINE_REPLACE_NONE
    assert tools["replace_in_recipe"]("腌肉", "加盐", "加糖") == BASELINE_REPLACE_THRICE
    assert tools["read_recipe"]("腌肉") == SALTY, "改了第一处"


# ── 回收站:删、列、撤回 ───────────────────────────────────────────────


def test_delete_and_undo_say_exactly_what_the_baseline_said(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    tools["write_recipe"]("腌肉", SALTY)

    assert tools["delete_recipe"]("腌肉", "试了两次都太咸,不做了") == BASELINE_DELETE
    assert tools["list_recipes"](include_deleted=True) == BASELINE_LIST_WITH_DELETED
    assert tools["delete_recipe"]("腌肉", undo=True) == BASELINE_UNDO
    assert tools["read_recipe"]("腌肉") == SALTY


# ── 搜索:三种结果 ────────────────────────────────────────────────────


def test_search_says_exactly_what_the_baseline_said(tools):
    """扫文件搜索整段搬进了共用层(`find_all` + `scan`),而 `find_all` 是这一轮**新的**
    ——`scan` 现在拿它的第一个落点。片段窗口、命中分类、排序一个字节都不许动。"""
    tools["write_recipe"]("红烧肉", "# 红烧肉\n冰糖一块,少放酱油\n")
    assert tools["search_recipes"]("酱油") == BASELINE_SEARCH_IN_TEXT
    assert tools["search_recipes"]("佛跳墙") == BASELINE_SEARCH_NOTHING

    tools["delete_recipe"]("红烧肉", "换成凉面")
    tools["write_recipe"]("凉面", "# 凉面\n煮面水留一点\n")
    assert tools["search_recipes"]("凉面") == BASELINE_SEARCH_BOTH
