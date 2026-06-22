"""评估 harness：把真实 agent 跑一遍，归约成可观测 Trace；并提供跑批/聚合/报表。

采集口径（关键，勿混用）：
  tool_calls  ← ToolExecutionStarted  ＝模型选了哪个工具（意图），不管之后是否被门禁/确认拦下
  results     ← ToolExecutionCompleted ＝工具真正执行（带 is_error）；"成功执行"＝is_error=False
"""

from dataclasses import dataclass, field
from enum import Enum

from core.events import (
    AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted,
)
from core.messages import TextBlock


@dataclass
class ToolCall:
    name: str
    input: dict


@dataclass
class ToolResultRecord:
    name: str
    output: str
    is_error: bool


@dataclass
class Trace:
    prompt: str
    role: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResultRecord] = field(default_factory=list)
    final_text: str = ""
    store: object = None
    _db_path: str | None = None

    # ---- 给判定函数用的小帮手 ----
    def called(self, name: str) -> bool:
        """模型是否选过这个工具（意图，含被拦下的）。"""
        return any(c.name == name for c in self.tool_calls)

    def executed_ok(self, name: str) -> bool:
        """这个工具是否成功执行过（is_error=False）。"""
        return any(r.name == name and not r.is_error for r in self.results)

    def any_executed_ok(self, names) -> bool:
        """names 里任一工具是否成功执行过。"""
        return any(self.executed_ok(n) for n in names)

    def cleanup(self):
        """关闭并删除本 case 的临时 db。判定函数跑完后由 run_suite 调用。"""
        import os
        if self.store is not None:
            self.store.close()
        if self._db_path:
            try:
                os.unlink(self._db_path)
            except OSError:
                pass


def reduce_events(events, prompt: str, role: str, store, db_path: str | None = None) -> Trace:
    """把事件列表归约成 Trace。纯函数，不碰引擎/IO。"""
    trace = Trace(prompt=prompt, role=role, store=store, _db_path=db_path)
    texts: list[str] = []
    for e in events:
        if isinstance(e, ToolExecutionStarted):
            trace.tool_calls.append(ToolCall(e.tool_name, e.tool_input))
        elif isinstance(e, ToolExecutionCompleted):
            trace.results.append(ToolResultRecord(e.tool_name, e.output, e.is_error))
        elif isinstance(e, AssistantTurnComplete):
            texts.append("".join(b.text for b in e.message.content if isinstance(b, TextBlock)))
    trace.final_text = "\n".join(t for t in texts if t)
    return trace
