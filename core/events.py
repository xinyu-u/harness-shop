"""事件：循环对外汇报"发生了什么"。

循环不直接 print，而是 yield 这些结构化事件，前端（CLI/以后的Web）
自己决定怎么显示。这让"循环逻辑"和"显示方式"解耦。
"""

from dataclasses import dataclass, field
from typing import Any
from core.messages import ConversationMessage


@dataclass(frozen=True)
class AssistantTurnComplete:
    """一轮 assistant 完成。"""
    message: ConversationMessage


@dataclass(frozen=True)
class ToolExecutionStarted:
    """即将执行某个工具。"""
    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True)
class ToolExecutionCompleted:
    """某个工具执行完成。"""
    tool_name: str
    output: str
    is_error: bool = False
    # 工具结构化透出的附加信息（如 place_order 的 draft_id），原样从 ToolResult 带出。
    metadata: dict[str, Any] = field(default_factory=dict)

