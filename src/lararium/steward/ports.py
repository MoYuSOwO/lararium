from typing import Any, Protocol


class LedgerPort(Protocol):
    """Steward 只需要读账本。写入永远经门控,不在这个接口里——
    这不是疏漏,是把"单写者"编码进了类型。"""

    def read(self) -> str: ...


class GatePort(Protocol):
    """Steward 只需要触发结算、查待审。提案由工具侧发起,不经过 Steward。"""

    def settle(self) -> int: ...

    def pending(self) -> list[Any]: ...
