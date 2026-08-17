from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from lararium.steward.journal import Journal
from lararium.steward.registry import Registry


class BuiltinTools:
    def __init__(self, journal: Journal, registry: Registry, timezone: str) -> None:
        self.journal = journal
        self.registry = registry
        self._tz = ZoneInfo(timezone)

    def current_time(self) -> str:
        """返回当前时间(带时区)。需要精确时刻或做日期推算时调用。"""
        now = datetime.now(self._tz)
        weekday = "一二三四五六日"[now.weekday()]
        return f"{now.isoformat(timespec='seconds')} 星期{weekday}"

    def read_skill(self, bundle: str, skill: str | None = None) -> str:
        """读取某个领域的方法说明。不带 skill 名时返回该领域总览(有哪些方法);
        带 skill 名时返回具体方法正文。照着某个方法干活前必须先读它。"""
        try:
            return self.registry.read_skill(bundle, skill)
        except KeyError as exc:
            return f"读取失败:{exc}"
        except FileNotFoundError:
            return f"读取失败:{bundle} 的 skill 文件缺失,请检查 bundle 安装是否完整"

    def search_history(self, query: str, limit: int = 10) -> str:
        """在历史对话记录里检索。用于翻旧账(几个月前提过的事)。
        搜不到就换个说法再搜——关键词要用对话里可能出现的原话。"""
        hits = self.journal.search(query, limit=limit)
        if not hits:
            return f"没有找到包含「{query}」的历史记录。换个关键词试试,或者用更短的词。"
        lines = [f"找到 {len(hits)} 条:"]
        for h in hits:
            lines.append(f"- [{h.ts[:10]}] ({h.envelope_id}) {h.text[:200]}")
        return "\n".join(lines)

    def as_tool_functions(self) -> list[Callable]:
        """顺序固定——工具 schema 是前缀第0层,顺序变了缓存全毁。"""
        return [self.current_time, self.read_skill, self.search_history]
