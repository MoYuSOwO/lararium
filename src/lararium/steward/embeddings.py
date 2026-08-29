"""本地中文 embedding 隔离盒——model2vec 只出现在这里(D2)。

`potion-multilingual-128M`(258 维→实际 256)经 model2vec 静态词表。
**权重走本地 fp16 文件 + mmap 加载,不碰网络**(M4-9)——见下方 `_load`。
模型不可用 = EmbeddingUnavailable,调用方(数据面 append / 语义检索)据此跳过,
不让第三方的加载错误打崩主循环(E2)。

embedding 在数据面算,不进前缀、不碰缓存、不出机器。

## 内存(2026-08-29 实测,别再猜)

目标机 2C2G(可用 1288 MB)。三种加载方式:

    现状(HF safetensors → float32)   峰值 1772 MB   常驻 1465 MB   ← 顶天花板,起不来
    fp16 文件全量读入                  峰值 1199 MB   常驻 1182 MB
    fp16 文件 + mmap_mode='r'          峰值  955 MB   常驻  938 MB   ← 现在这个

(本机复核 2026-08-29:峰值 1767 → 972 MB、常驻 1461 → 955 MB,和上表吻合。
摸过一千句不同内容之后常驻涨到 968 MB——按需换页是真的按需,不是一次性全进来。)

分量测下来和直觉相反:

    解释器 + numpy          28 MB
    + tokenizer(50万词表)  → +542 MB(Linux)  ← 大头在这
    + fp16 矩阵(mmap)      → +0 MB           ← mmap 之后接近免费
    + 摸 1000 行            → +1 MB           ← 一次对话只碰几千行

**别再说「矩阵 489 MB 是硬占的」**:mmap 之后按需换页,矩阵几乎不要钱;真正常驻的是
HuggingFace `tokenizers` 的 Unigram 词表对象。那 542 MB 也不是字符串(50 万条原始文本
总共才 5~10 MB),是 Unigram 为最长匹配建的双数组 Trie——按码点索引,而多语言的
Unicode 跨度极大,数组极稀疏。

**同一个 tokenizer 在 macOS 上量出来是 747 MB,虚高近 40%。内存数字要在目标 OS 上量**
——差一点就按 macOS 的数字得出"2G 不够"的结论,去加内存或者砍功能。

## 性能代价:有,别当它是免费的

同机对照(2026-08-29,macOS;每句都是没碰过的行 vs 页已热):

    float32(HF)   加载 1842 ms   首次触达 40.9 µs/句   页热后 28.9 µs/句   常驻 1461 峰值 1767 MB
    fp16 + mmap   加载  842 ms   首次触达 91.2 µs/句   页热后 31.1 µs/句   常驻  955 峰值  972 MB

**慢在哪里要说准**:页热之后只慢 8%(28.9 → 31.1 µs)——那才是 fp16 算术的代价
(numpy 的 fp16 在多数 CPU 上没硬件加速,算时先转 float32)。首次触达慢 2.2 倍,
**多出来的 ~60 µs 几乎全是缺页**,不是算术。所以"慢 50%"这个说法方向对、归因错:
换成大量不同句子测才看得出真正的分布。

绝对值:最坏一句多 60 微秒。embedding 每轮只调一次,同一轮里还有一次 1~3 秒的模型
API 调用——多出来的是它的**两万分之一**。加载反而快了一倍(不用再解析 HF 那套元数据)。
用这点延迟换八百兆峰值,取舍明确;但别把它当成免费的。

## 明确不做:裁词表

砍掉非中文的 54.7% 词表能把那 542 MB 砍掉大半,是目前唯一还有大头可省的地方。**但不做**,
理由不是技术上的:这是一个开源项目,**多语言是它的产品属性,不是可优化的冗余**。
裁成中文专用等于砍掉所有非中文用户,换来的只是一台 2 GB 机器上的几百 MB——而那台机器上
其实装得下;用户自己也会混着中英文说话。

写在这里就是为了挡住以后的人:谁看到那 542 MB 想"我们只用中文,裁了吧",先读这一段。
内存真到了不够的那天,正确的做法是**换一个更小的多语模型**,不是把语言支持砍掉。
"""

import json
import math
import os
from pathlib import Path
from typing import Any

EMBED_MODEL = "minishlab/potion-multilingual-128M"
_DIM = 256

# 缓存**按权重目录分键**,不是一个全局的 _model/_model_error。
# 路径是运行时解析的(env 可覆盖),而一次失败原来会把整个进程钉死:某个用别的 data_dir
# 的调用先触发加载、失败了,之后所有调用都拿那次失败当结论——哪怕路径早就换回来了。
# "这个目录加载失败"是关于**那个目录**的事实,不是关于进程的。
_models: dict[Path, Any] = {}  # Any:第三方模型对象,不用给它写 stub
_errors: dict[Path, str] = {}


class EmbeddingUnavailable(RuntimeError):
    """embedding 模型没加载成功——调用方 fallback,不往上炸。"""


def weights_dir() -> Path:
    """本地权重目录。`LARARIUM_EMBEDDING_DIR` 可覆盖,默认挂在 data_dir 下。"""
    override = os.environ.get("LARARIUM_EMBEDDING_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("LARARIUM_DATA_DIR", "./data")) / "embedding"


def _load():
    root = weights_dir()
    if root in _models:
        return _models[root]
    if root in _errors:
        raise EmbeddingUnavailable(_errors[root])
    try:
        # **不走 StaticModel.from_pretrained**:那条路会去 HuggingFace,而且把矩阵
        # 读成 float32(峰值 1772 MB)。这里自己拼:矩阵 mmap 打开(按需换页,
        # 常驻接近零),tokenizer 与 config 从本地文件读。
        # 权重由 `scripts/build_embedding_weights.py` 离线转好——**不要在启动时转**,
        # 那等于没降峰值。
        import numpy as np
        from model2vec import StaticModel
        from tokenizers import Tokenizer

        matrix = np.load(root / "embedding.npy", mmap_mode="r")
        tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
        _models[root] = StaticModel(vectors=matrix, tokenizer=tokenizer, config=config)
        return _models[root]
    except FileNotFoundError as exc:
        # 和"装了但坏了"分开说:两者的下一步动作不一样,合成一句话用户就不知道该干嘛。
        _errors[root] = (
            f"embedding 权重不在 {root}(缺 {Path(str(exc.filename or '?')).name})"
            f"——跑一次 `uv run python scripts/build_embedding_weights.py` 生成"
        )
        raise EmbeddingUnavailable(_errors[root]) from exc
    except Exception as exc:  # 权重损坏 / 环境缺构件 都归这一类
        _errors[root] = f"embedding 模型加载失败({type(exc).__name__}: {exc})"
        raise EmbeddingUnavailable(_errors[root]) from exc


def embedding_available() -> bool:
    """模型是否可用(每个权重目录的成败各自记住,不重复试)。"""
    root = weights_dir()
    if root in _models:
        return True
    if root in _errors:
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
        vec = model.encode(text)
    except EmbeddingUnavailable:
        return None
    vector = [float(x) for x in vec][:_DIM]
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]
