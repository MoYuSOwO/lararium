"""领域 bundle 的统一构造出口。

每个领域 bundle 暴露 `build(data_dir) -> BundleRuntime`,组装根拿它拼工具列表。
memory 是特殊 bundle(§6.1:ledger/gate 走 Steward 的 ports,不试图抹平),不进这个
通用形状。
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class BundleRuntime:
    """一个领域 bundle 的运行期形状。

    tools 是模型可调的工具函数列表,**顺序即冻结顺序**(工具 schema 是前缀第0层,
    DESIGN §4);manifest.yaml 的 tools 顺序是设计时的权威,实现必须逐名对齐。
    bundle 的独有资产不进这里,由各 bundle 自己定义并交给需要的消费者。
    """

    tools: list[Callable] = field(default_factory=list)
