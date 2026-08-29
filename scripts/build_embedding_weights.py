"""把 embedding 权重转成本地 fp16,供运行时 mmap 加载(M4-9)。

**一次性、离线跑**,不在启动时转——启动时转等于没降峰值,那 1772 MB 照样会出现一次。
转换脚本进仓库,**权重不进**(它在 `data/` 下,已被 .gitignore)。

    uv run python scripts/build_embedding_weights.py

产出三个文件到 `{LARARIUM_EMBEDDING_DIR}`(默认 `{LARARIUM_DATA_DIR}/embedding`):

    embedding.npy    fp16 矩阵,运行时 mmap_mode='r' 打开
    tokenizer.json   原样拷贝
    config.json      原样拷贝(含 normalize 等 model2vec 要的字段)

顺带在 float32 原始权重上量一遍参照相似度,写进 `tests/embedding_reference.json`
——那张表 committed 进仓库,让质量回归**每次都能跑**,不需要 HuggingFace 权重在场。
本脚本自己会短暂占到 1.5 GB 左右(它要先把 float32 读进来),所以在开发机上跑,
别在目标机上跑。
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, "src")

from lararium.steward.embeddings import EMBED_MODEL, weights_dir

REFERENCE = Path("tests/embedding_reference.json")

# 质量回归的句对。沿用 M3-4 那次决定性对照记下来的**查询**(REVIEW.md M3-4),
# 每条配一句它当时该命中的语料。**不是精度测试**——它挡的是"换错权重 / tokenizer
# 不匹配 / 归一化丢了 / 维度变了"这类会让语义检索静默变差的事故。
PAIRS = [
    ("家里人的近况怎么样", "妈妈今天打电话来说她最近身体还行"),
    ("这个月钱是不是花超了", "这个月餐饮花了一千二,比上个月多了三成"),
    ("跑步有没有伤到膝盖", "昨天跑了五公里,右边膝盖有点不舒服"),
    ("家里有没有囤吃的", "冰箱里还有一包速冻饺子和半袋米"),
    ("约了人吃饭见面没", "周六和老王约了在楼下那家川菜馆"),
    ("最近睡得好不好", "这两天总是半夜醒,白天没精神"),
    ("车该保养了吗", "上次换机油是三月,已经跑了八千公里"),
    ("书看到哪了", "《人类简史》读了一半,卡在农业革命那章"),
    ("房租什么时候交", "每月十号交房租,三千八"),
    ("周末干什么", "周日打算去公园走走,天气应该不错"),
]


def main() -> int:
    from model2vec import StaticModel

    out = weights_dir()
    print(f"目标目录:{out}")
    print(f"从 HuggingFace 读 {EMBED_MODEL}(float32,峰值约 1.5 GB)…", flush=True)
    model = StaticModel.from_pretrained(EMBED_MODEL, force_download=False)

    float32_matrix = np.asarray(model.embedding, dtype=np.float32)
    print(
        f"  矩阵 {float32_matrix.shape} {float32_matrix.dtype}"
        f" = {float32_matrix.nbytes / 1024**2:.0f} MB"
    )

    # 先在 float32 上量参照,再转 fp16 —— 顺序反了就量不到"原始"的那一份了
    reference = [_similarity(model, a, b) for a, b in PAIRS]

    out.mkdir(parents=True, exist_ok=True)
    fp16 = float32_matrix.astype(np.float16)
    np.save(out / "embedding.npy", fp16)
    print(f"  → embedding.npy fp16 = {fp16.nbytes / 1024**2:.0f} MB")

    (out / "tokenizer.json").write_text(model.tokenizer.to_str(), encoding="utf-8")
    (out / "config.json").write_text(
        json.dumps(model.config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  → tokenizer.json + config.json({out})")

    # 用刚写出去的 fp16 重新加载一遍,量同一批句对,报出最大漂移
    del model, float32_matrix, fp16
    from lararium.steward import embeddings as em

    em._model = None
    em._model_error = None
    local = em._load()
    got = [_similarity(local, a, b) for a, b in PAIRS]
    worst = max(abs(g - r) for g, r in zip(got, reference, strict=True))
    print(f"\nfloat32 vs fp16 最大相似度漂移:{worst:.6f}(判定阈值 0.35,间隙约 0.09)")

    REFERENCE.write_text(
        json.dumps(
            {
                "model": EMBED_MODEL,
                "measured_at": datetime.now(UTC).date().isoformat(),
                "note": "float32 原始权重上量的参照相似度;质量回归拿 fp16 复现它。",
                "pairs": PAIRS,
                "float32_similarity": reference,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"参照表写入 {REFERENCE}")
    return 0


def _similarity(model: object, left: str, right: str) -> float:
    a, b = model.encode(left), model.encode(right)  # type: ignore[attr-defined]
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    return float(a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
