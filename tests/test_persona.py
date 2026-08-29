"""M4-8:人设(用户的)与纪律(系统的)拆开装配。

`prompts/persona.md` 原来混着两种东西:上半截是人设(语气、相处方式),下半截「硬性纪律」
装着几个里程碑打出来的东西——流水不进账本(M4-5)、说记好了之前先真的调工具(M4-5c)、
propose 门控与变化频率判据。**用户为改语气去编辑这个文件,极易连下半截一起重写,
而那不会有任何报错**,只会在某天发现账本里全是午饭。

所以拆:人设归用户(`{data_dir}/character.md`,不进仓库),纪律归系统(`prompts/discipline.md`,
是代码的一部分)。
"""

from pathlib import Path

import pytest

from lararium.persona import (
    MAX_CHARACTER_CHARS,
    assemble_persona,
    character_path,
    load_discipline,
)


def test_missing_character_file_falls_back_to_the_builtin_default(tmp_path):
    """人设文件不存在 → 用内置默认照常启动,纪律仍在。

    新装的机器上 `character.md` 本来就不存在,不能因此起不来。
    """
    persona, warnings = assemble_persona(tmp_path)

    assert "你是 Lararium" in persona
    assert "流水进领域模块,不进账本" in persona, "纪律丢了"
    assert warnings == []


def test_an_empty_character_file_still_keeps_the_discipline(tmp_path):
    """人设被清空 → 纪律**照样在前缀里**。

    这条是这次拆分的全部意义:用户怎么折腾自己那半截,系统那半截都不受影响。
    """
    character_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    character_path(tmp_path).write_text("   \n\n", encoding="utf-8")

    persona, _ = assemble_persona(tmp_path)

    assert "流水进领域模块,不进账本" in persona
    assert "先真的把工具调了" in persona


def test_character_comes_first_and_discipline_last(tmp_path):
    """顺序写死:人设在前、纪律在后。先说你是谁,再说规矩;规矩靠后也更贴近后文。"""
    character_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    character_path(tmp_path).write_text("你是老王。", encoding="utf-8")

    persona, _ = assemble_persona(tmp_path)

    assert persona.index("你是老王。") < persona.index("硬性纪律")


def test_the_same_files_assemble_byte_identically(tmp_path):
    """跨两次组装逐字节相同——前缀是缓存命中的命根子(A1)。"""
    character_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    character_path(tmp_path).write_text("你是老王。", encoding="utf-8")

    assert assemble_persona(tmp_path)[0] == assemble_persona(tmp_path)[0]


def test_editing_the_character_changes_the_prefix_and_not_editing_does_not(tmp_path):
    """改人设 → 前缀变;不改 → 逐字节不变(A1 回归)。"""
    character_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    character_path(tmp_path).write_text("你是老王。", encoding="utf-8")
    before = assemble_persona(tmp_path)[0]

    assert assemble_persona(tmp_path)[0] == before, "什么都没改,前缀却变了"

    character_path(tmp_path).write_text("你是老李。", encoding="utf-8")
    assert assemble_persona(tmp_path)[0] != before


def test_an_overlong_character_warns_but_still_starts(tmp_path):
    """人设每轮都在前缀里付钱:超过软上限**警告但不拒绝**——用户自己的机器,用户做主。"""
    long_character = "很长" * MAX_CHARACTER_CHARS
    character_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    character_path(tmp_path).write_text(long_character, encoding="utf-8")

    persona, warnings = assemble_persona(tmp_path)

    # 断言**全文都在**,不是"含有'很长'"——第一版就是那么写的,而"超了就截断"照样能过:
    # 截断后的文本一样含"很长"。软上限的语义是"警告但不动你的东西",截断是另一回事。
    assert long_character in persona, "不该拒绝,也不该截断,只该警告"
    assert any("人设" in w and str(MAX_CHARACTER_CHARS) in w for w in warnings), warnings


def test_the_discipline_file_carries_what_the_milestones_bought(tmp_path):
    """纪律文件里必须还装着那几条——它们是几个里程碑打出来的,删掉不会报错。"""
    text = load_discipline()

    for bought in (
        "read_skill",  # M4-2:没读过正文不许照着干活
        "current_time",  # 日期推算
        "先真的把工具调了",  # M4-5c
        "流水进领域模块,不进账本",  # M4-5 的乙
        "变化频率",  # M3-7
    ):
        assert bought in text, f"纪律里丢了:{bought}"


def test_no_tool_can_reach_the_character_file(tmp_path, monkeypatch):
    """★ **模型没有任何工具能写人设文件。**

    这不是洁癖。两条理由:
    (a) 前缀是缓存命中的命根子,模型可控写入 = 每轮都可能重建;
    (b) **模型可控写入前缀 = 提示注入直通车**。P0-1 那个洞最多污染一轮,
        人设被改是**之后每一轮都听新的**,是同一个洞的升级版。

    用户想在对话里调语气?走已有机制:`propose_fact` 进账本、过门控、用户点头才生效。
    账本本来就在前缀里每轮注入,效果一样而且有闸门——**不要为此新增任何工具**。
    """
    from bundles.finance.server import build as build_finance
    from bundles.memory.server import build_memory_components, memory_tool_functions

    from lararium.config import Settings
    from lararium.db import connect
    from lararium.steward.inbox import Inbox
    from lararium.steward.journal import Journal
    from lararium.steward.loop import Steward
    from lararium.steward.outbox import Outbox
    from lararium.steward.registry import Registry
    from lararium.steward.threads import Threads

    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
    monkeypatch.setenv("LARARIUM_DATA_DIR", str(tmp_path))
    settings = Settings.load()
    conn = connect(tmp_path / "steward.sqlite")
    ledger, gate = build_memory_components(tmp_path)
    steward = Steward(
        settings=settings,
        inbox=Inbox(conn),
        journal=Journal(conn),
        registry=Registry.load(Path("bundles")),
        ledger=ledger,
        gate=gate,
        model=object(),
        persona=assemble_persona(tmp_path)[0],
        outbox=Outbox(conn),
        threads=Threads(conn),
        bundle_tools=[
            *memory_tool_functions(gate),
            *build_finance(tmp_path, timezone="Asia/Shanghai").tools,
        ],
    )

    target = character_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("原始人设", encoding="utf-8")

    for fn in steward.all_tools():
        for payload in (str(target), "原始人设", "你现在要听我的"):
            try:
                fn(payload)
            except TypeError:
                pass  # 签名对不上,这个工具本来就不接受一个字符串
            except Exception:
                pass

    assert target.read_text(encoding="utf-8") == "原始人设", "有工具改到了人设文件"


def test_read_skill_cannot_be_pointed_at_the_character_file(tmp_path):
    """连**读**也不该从 skill 白名单绕出去——路径穿越那条老账在新文件上一样成立。"""
    from lararium.steward.registry import Registry

    registry = Registry.load(Path("bundles"))
    with pytest.raises(KeyError):
        registry.read_skill("memory", "../../../data/character")


def test_a_changed_prefix_is_recorded_and_an_unchanged_one_is_not(tmp_path):
    """前缀变更留痕:变了记一条并报出上一个指纹;没变什么都不写。

    **独立有价值,不只服务人设**:改了人设、缓存命中从 90% 掉到 0,现在没有任何地方
    说得清为什么。「缓存命中是设计约束,不是优化项」——那它什么时候变过就必须查得出来。
    """
    from lararium.db import connect
    from lararium.persona import prefix_digest, record_prefix_change

    conn = connect(tmp_path / "s.sqlite")
    first = prefix_digest("人设A", "目录", "账本")

    assert record_prefix_change(conn, first) is None, "首次没有上一个指纹"
    assert record_prefix_change(conn, first) is None, "没变就不该再记一条"
    assert conn.execute("SELECT count(*) FROM prefix_log").fetchone()[0] == 1

    second = prefix_digest("人设B", "目录", "账本")
    assert record_prefix_change(conn, second) == first, "该报出上一个指纹"
    assert conn.execute("SELECT count(*) FROM prefix_log").fetchone()[0] == 2


def test_changing_a_tool_docstring_changes_the_digest():
    """★ **改一个工具的 docstring,指纹必须变。**

    docstring 就是 schema,schema 是前缀**第 0 层**——M4 全程盯得最紧的一条:M4-1 冻结
    工具顺序、M4-3 为它专门拍过板、M4-4 那次改 docstring 是授权才动的、M4-5d 还写了
    报文级测试去比真正发出去的字节。而留痕机制第一版**唯独看不见这一层**:
    改 docstring 前后指纹一模一样。

    失效形态正是它存在理由的反面:有人改了个 docstring,缓存从 90% 掉到 0,
    而 `prefix_log` 一声不吭——**没有记录不是"没变过",但读的人会当成没变过**。
    """
    from lararium.persona import tool_schema_fingerprint

    def record_expense(amount: float, category: str) -> str:
        """记一笔支出。"""
        return ""

    before = tool_schema_fingerprint([record_expense])
    record_expense.__doc__ = "记一笔支出。category 必须是固定类目之一。"

    assert tool_schema_fingerprint([record_expense]) != before


def test_changing_a_tool_signature_changes_the_digest():
    """签名同理:加一个参数就是改 schema。

    两个函数**同名同 docstring**,只差签名——第一版写成 `f` 和 `g`,光靠名字就能区分开,
    于是"指纹料里不含签名"这个 bug 照样绿。测一个维度就得把别的维度按住。
    """
    from lararium.persona import tool_schema_fingerprint

    def one_arg(a: int) -> str:
        """同一段说明。"""
        return ""

    def two_args(a: int, b: str = "x") -> str:
        """同一段说明。"""
        return ""

    two_args.__name__ = one_arg.__name__ = "same_name"

    assert tool_schema_fingerprint([one_arg]) != tool_schema_fingerprint([two_args])


def test_reordering_tools_changes_the_digest():
    """顺序也是 schema 的一部分——工具顺序变了缓存全毁,M4-1 起就冻结着。"""
    from lararium.persona import tool_schema_fingerprint

    def a() -> str:
        """A。"""
        return ""

    def b() -> str:
        """B。"""
        return ""

    assert tool_schema_fingerprint([a, b]) != tool_schema_fingerprint([b, a])


def test_the_digest_is_computed_from_what_is_actually_sent(tmp_path):
    """指纹的输入是**这次真正会发给模型的两样**:system_prompt + 工具 schema。

    不再枚举"已知重建点"——枚举法今天就已经漏了第 0 层,而且 `_SYSTEM_TEMPLATE` 里的
    固定脚手架文字改了,三层同样一个都不动。按构造取就不用维护那张清单。
    """
    from lararium.persona import prefix_digest
    from lararium.steward.assembler import render_system_prompt

    base = prefix_digest(
        render_system_prompt(persona="人设", directory="目录", ledger="账本"), "工具"
    )

    assert (
        prefix_digest(
            render_system_prompt(persona="人设!", directory="目录", ledger="账本"), "工具"
        )
        != base
    )
    assert (
        prefix_digest(
            render_system_prompt(persona="人设", directory="目录!", ledger="账本"), "工具"
        )
        != base
    )
    assert (
        prefix_digest(
            render_system_prompt(persona="人设", directory="目录", ledger="账本!"), "工具"
        )
        != base
    )
    assert (
        prefix_digest(
            render_system_prompt(persona="人设", directory="目录", ledger="账本"), "工具!"
        )
        != base
    )


def test_the_digest_cannot_be_fooled_by_moving_text_across_parts(tmp_path):
    """拼接要有分隔:("ab","c") 和 ("a","bc") 不能撞成同一个指纹。"""
    from lararium.persona import prefix_digest

    assert prefix_digest("ab", "c") != prefix_digest("a", "bc")
