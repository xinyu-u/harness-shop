"""数据结构：对话消息与内容块。

这是 harness 的"语言"——一条消息由 role + 内容块列表组成。
四种块用 type 字段做判别式联合，工具请求和结果靠 id 配对。
"""

from typing import Any, Annotated, Literal
from uuid import uuid4
from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    """纯文字内容。"""
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """模型发出的工具调用请求。"""
    type: Literal["tool_use"] = "tool_use"
    id: str = Field(default_factory=lambda: f"toolu_{uuid4().hex}")
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """塞回给模型的工具结果，用 tool_use_id 与请求配对。"""
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False
    result_metadata: dict[str, Any] = Field(default_factory=dict)


ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="type"),
]


class ConversationMessage(BaseModel):
    """一条 user 或 assistant 消息。"""
    role: Literal["user", "assistant"]
    content: list[ContentBlock] = Field(default_factory=list)

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    @property
    def tool_results(self) -> list[ToolResultBlock]:
        return [b for b in self.content if isinstance(b, ToolResultBlock)]
