"""本地中文 embedding 隔离盒——model2vec 只出现在这里(D2)。

`potion-multilingual-128M`(258 维→实际 256)经 model2vec 静态词表,首次加载要
从 HuggingFace 拉权重(约 10 分钟,一次性;M4 要打进镜像,别让 VPS 首启去拉)。
模型不可用 = EmbeddingUnavailable,调用方(数据面 append / 语义检索)据此跳过,
不让第三方的加载错误打崩主循环(E2)。

embedding 在数据面算,不进前缀、不碰缓存、不出机器。
"""

import math
from typing import Any

EMBED_MODEL = "minishlab/potion-multilingual-128M"
_DIM = 256

_model: Any = None  # Any:第三方模型对象,不用给它写 stub
_model_error: str | None = None


class EmbeddingUnavailable(RuntimeError):
    """embedding 模型没加载成功——调用方 fallback,不往上炸。"""


def _load():
    global _model, _model_error
    if _model is not None:
        return _model
    if _model_error is not None:
        raise EmbeddingUnavailable(_model_error)
    try:
        from model2vec import StaticModel

        _model = StaticModel.from_pretrained(EMBED_MODEL)
        return _model
    except Exception as exc:  # 网络断/权重损坏/环境缺构件 都归这一类
        _model_error = f"embedding 模型加载失败({type(exc).__name__}: {exc})"
        raise EmbeddingUnavailable(_model_error) from exc


def embedding_available() -> bool:
    """模型是否可用(已加载或加载失败都会记住,不重复拉)。"""
    if _model is not None:
        return True
    if _model_error is not None:
        return False
    try:
        _load()
        return True
    except EmbeddingUnavailable:
        return False


def embed(text: str) -> list[float] | None:
    """返回 256 维 **L2 归一化**向量;模型不可用返回 None(不抛)。

    归一化后进 vec0:L2 距离序 = 余弦相似度序,cos ≈ 1 - d²/2(阈值判定靠它)。
    """
    try:
        model = _load()
        vec = model.encode(text)  # model2vec 返回 numpy float32 数组
    except EmbeddingUnavailable:
        return None
    vector = [float(x) for x in vec][:_DIM]
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]
