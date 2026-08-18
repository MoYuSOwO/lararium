"""CLI 客户端——M2 之后降级为普通 HTTP 客户端,没有任何特殊地位。

聊天 → POST /v1/messages 拿信封 id → 长轮询 GET /v1/outbox 等回复(路过的 notice
顺手打印);`/` 开头 → POST /v1/commands。Ctrl-C/Ctrl-D 直接退出——结算是服务端
worker 的事(D11),客户端关窗口不是系统事件。

与将来的 IM 适配器同级:只 import httpx,不 import 任何 bundles/steward
(`.importlinter` 有对应禁入契约)。配置:
  LARARIUM_SERVER_URL   默认 http://127.0.0.1:8420
  LARARIUM_CLIENT_TOKEN 控制端 token(必须,命令端点是门控开关)
  LARARIUM_CLIENT_STATE after 游标持久化文件(默认 ~/.lararium/cli.seq)
"""

import os
import sys
import time
from pathlib import Path

import httpx

DEFAULT_SERVER_URL = "http://127.0.0.1:8420"

_QUIT_HINT = "已退出客户端。服务端还在跑,结算由 worker 空闲时自动执行。"


class Client:
    """薄 HTTP 客户端。同步就够(单终端 REPL)。"""

    def __init__(
        self,
        server_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=server_url,
            # 超时要为长轮询留足余量:单次 outbox wait 最多 5s,多轮往返
            timeout=httpx.Timeout(70.0, connect=5.0),
            transport=transport,
        )
        self._headers = {"Authorization": f"Bearer {token}"}
        # 已消费的最大 outbox seq——客户端按 seq 去重(at-least-once 的客户端侧)。
        # 持久化在 main() 里完成(跨重启续传,不丢不重)。
        self.after = 0

    def close(self) -> None:
        self._http.close()

    def send_message(self, content: str) -> str:
        """入队一条聊天消息,返回服务端信封 id(回复靠它匹配)。"""
        return self._post("/v1/messages", {"content": content})["envelope_id"]

    def poll_reply(self, envelope_id: str, timeout_s: float = 120.0) -> str | None:
        """长轮询直到出现该信封的 reply;路过的 notice 顺手打印(非目标渠道不消费)。

        返回回复文本;超时返回 None。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            wait = int(min(5, max(1, deadline - time.monotonic())))
            items = self._poll_outbox(wait_s=wait)
            for it in items:
                if it["kind"] == "notice":
                    print(f"\n[通知] {it['content']}")
                elif it["envelope_id"] == envelope_id and it["kind"] == "reply":
                    return it["content"]
        return None

    def command(self, line: str) -> str:
        return self._post("/v1/commands", {"line": line})["text"]

    # ── 内部 ──

    def _post(self, path: str, payload: dict) -> dict:
        r = self._http.post(path, json=payload, headers=self._headers)
        if r.status_code in (401, 403):
            raise PermissionError(
                f"鉴权失败({r.status_code})——检查 LARARIUM_CLIENT_TOKEN 是否控制端 token"
            )
        r.raise_for_status()
        return r.json()

    def _poll_outbox(self, wait_s: int) -> list[dict]:
        r = self._http.get(f"/v1/outbox?after={self.after}&wait={wait_s}", headers=self._headers)
        if r.status_code in (401, 403):
            raise PermissionError("鉴权失败——检查 LARARIUM_CLIENT_TOKEN")
        r.raise_for_status()
        items = r.json()["items"]
        if items:
            self.after = items[-1]["seq"]
        return items


def _state_path() -> Path:
    return Path(os.environ.get("LARARIUM_CLIENT_STATE", str(Path.home() / ".lararium" / "cli.seq")))


def _read_after(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def _write_after(path: Path, after: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(after), encoding="utf-8")
    except OSError as exc:  # 游标写不进去不致命:最多重拉一次
        print(f"(warning: 无法持久化游标 {after}:{exc})", file=sys.stderr)


def main() -> None:
    server_url = os.environ.get("LARARIUM_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")
    token = os.environ.get("LARARIUM_CLIENT_TOKEN") or ""
    if not token:
        print("LARARIUM_CLIENT_TOKEN 未设置。(需要控制端 token,服务端配置见 LARARIUM_TOKENS)")
        sys.exit(1)

    client = Client(server_url, token)
    state = _state_path()
    client.after = _read_after(state)
    print(f"Lararium 客户端已连接 {server_url}。输入 /help 看命令,输入 /quit 退出客户端。")

    try:
        while True:
            try:
                line = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{_QUIT_HINT}")
                return
            if not line:
                continue
            if line.startswith("/"):
                if line.strip() == "/quit":
                    print(_QUIT_HINT)
                    return  # 客户端退出;结算归服务端 worker(D11)
                try:
                    print(client.command(line))
                except Exception as exc:
                    print(f"命令出错(不影响后续):{type(exc).__name__}: {exc}")
                continue

            try:
                env_id = client.send_message(line)
                reply = client.poll_reply(env_id)
            except Exception as exc:
                print(f"处理出错(不影响后续):{type(exc).__name__}: {exc}")
                continue
            if reply is not None:
                print(f"\nLararium > {reply}")
            else:
                print("\n(超时未收到回复)")
            _write_after(state, client.after)
    finally:
        _write_after(state, client.after)
        client.close()


def run_cli() -> None:
    main()


if __name__ == "__main__":
    run_cli()
