"""M4-9:embedding 权重走本地 fp16 + mmap 加载。

**为什么**:目标机 2C2G(可用 1288 MB),而从 HuggingFace 读 safetensors 再转 float32,
**加载峰值 1772 MB**——在那台机器上加载那一瞬间就顶到天花板,起不来。
fp16 + mmap 之后是峰值 955 / 常驻 940 MB。

质量代价为零(见 `embedding_reference.json` 的实测),性能代价有但在无关紧要的地方
(单句编码 69.5 → 104.2 µs,多出的 35 µs 是同轮那次 1~3 秒模型调用的三万分之一)。
"""

import json
from pathlib import Path

import numpy as np
import pytest

from lararium.steward import embeddings as em

REFERENCE = Path("tests/embedding_reference.json")


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """隔离盒的缓存是模块级的(既有设计),测试之间清干净,免得互相串味。"""
    monkeypatch.setattr(em, "_models", {})
    monkeypatch.setattr(em, "_errors", {})


def test_loading_never_touches_the_network(monkeypatch):
    """★ 加载只读本地文件,**一次网络都不碰**。

    断言方式是把 `StaticModel.from_pretrained`(唯一会去 HuggingFace 的入口)换成炸弹:
    只要加载路径还会走到它,这条就红。比"断言没有 HTTP 请求"更直接,也更难糊弄。
    """
    from model2vec import StaticModel

    def explode(*_a, **_k):
        raise AssertionError("加载走了 from_pretrained —— 那会去拉 HuggingFace")

    monkeypatch.setattr(StaticModel, "from_pretrained", staticmethod(explode))

    assert em.embedding_available(), "本地权重加载不起来"
    assert em.embed("今天天气不错") is not None


def test_the_matrix_is_fp16_and_memory_mapped():
    """矩阵是 fp16,**而且不是全量读进内存**。

    只断言 dtype 挡不住"读进来再 astype"——那样峰值一点没降(实测 fp16 全量读入
    仍要 1199 MB,而 mmap 是 955 MB)。所以要断言它真的是 memmap。
    """
    em._load()

    matrix = em._load().embedding
    assert matrix.dtype == np.float16
    assert isinstance(matrix, np.memmap), f"矩阵被全量读进内存了:{type(matrix)}"


def test_fp16_reproduces_the_float32_similarities():
    """质量回归:fp16 与 float32 的相似度差 < 0.01。

    参照表是转换脚本在 float32 原始权重上量出来的(`embedding_reference.json`),
    committed 进仓库——这样这条回归**每次都跑**,不需要 HuggingFace 权重在场。
    它挡的不只是精度:换错权重、tokenizer 不匹配、归一化丢了、维度变了,都会在这里红。

    判定阈值是 0.35,命中与未命中的间隙约 0.09;0.01 的容差比那个间隙小一个数量级。
    """
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))
    assert reference["model"] == em.EMBED_MODEL

    worst = 0.0
    for (left, right), expected in zip(
        reference["pairs"], reference["float32_similarity"], strict=True
    ):
        a, b = em.embed(left), em.embed(right)
        assert a is not None and b is not None
        got = sum(x * y for x, y in zip(a, b, strict=True))
        worst = max(worst, abs(got - expected))

    assert worst < 0.01, f"fp16 与 float32 的相似度最大漂移 {worst:.5f},超过 0.01"


def test_missing_weights_degrade_instead_of_crashing(monkeypatch, tmp_path):
    """权重文件不在 → `EmbeddingUnavailable`,不打崩主循环(E2)。

    这条和"断网"是**同一条降级路径的两个入口**,但提示要分得开:没装权重和装了但坏了,
    给用户看的下一步动作不一样。
    """
    monkeypatch.setenv("LARARIUM_EMBEDDING_DIR", str(tmp_path / "不存在"))

    assert em.embedding_available() is False
    assert em.embed("今天天气不错") is None
    with pytest.raises(em.EmbeddingUnavailable, match="权重"):
        em._load()


def test_a_failure_for_one_directory_does_not_poison_another(monkeypatch, tmp_path):
    """★ 一个目录加载失败,**不许**把别的目录也判死。

    加载路径是运行时解析的(env 可覆盖)。缓存原来是一个全局的 `_model_error`,
    于是"某个用别的 data_dir 的调用先触发加载并失败"会把整个进程钉死——之后所有调用
    都拿那次失败当结论,哪怕路径早就换回来了。**"这个目录加载不了"是关于那个目录的事实,
    不是关于进程的。**

    这不是测试串味才有的毛病:生产里任何在 data_dir 就绪之前碰一下语义检索的代码,
    都会让这台机器此后再也用不上语义检索,而且没有任何报错指向原因。
    """
    monkeypatch.setenv("LARARIUM_EMBEDDING_DIR", str(tmp_path / "不存在"))
    assert em.embedding_available() is False

    monkeypatch.delenv("LARARIUM_EMBEDDING_DIR")
    assert em.embedding_available() is True, "换回好目录之后还在拿上一次的失败当结论"


def test_embed_returns_exactly_unit_vectors():
    """`embed()` 的契约是 **L2 归一化**,而且要精确到 1.0。

    模型自己的 config 里 `normalize: true`,但 `encode()` 出来的模长实测是 0.9994
    ——fp16 舍入之后不再精确。vec0 里靠 `cos ≈ 1 - d²/2` 从 L2 距离反推余弦,
    那个近似的前提就是模长为 1;模长带 0.06% 的误差会直接漂进阈值判定。

    这条是补上来的:相似度回归对这种误差**不敏感**(两个都差不多短,点积几乎不变),
    所以"把归一化删掉"那个变异能从它旁边走过去。**测契约要直接测契约本身。**
    """
    for text in ("今天中午吃饭 45", "妈妈打电话来说她最近身体还行", "a"):
        vector = em.embed(text)
        assert vector is not None
        norm = sum(x * x for x in vector) ** 0.5
        assert abs(norm - 1.0) < 1e-9, f"{text!r} 的模长是 {norm}"
        assert len(vector) == 256
