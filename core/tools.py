"""工具基类与结果类型。

BaseTool 规定每个工具有：名字、描述、参数模型、execute、to_api_schema。
is_write 标记区分 A类只读 / B类写操作（阶段5 权限机制据此判断要不要确认）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from pydantic import BaseModel


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果（不带配对 id，配对是循环层的事）。"""
    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTool(ABC):
    """所有工具的基类。"""
    name: str
    description: str
    input_model: type[BaseModel]
    is_write: bool = False   # A类只读=False；B类写操作=True（阶段5权限用）

    # 角色门禁：None=所有角色可见可用；非空集合=只对这些角色开放
    # 在 run_query 里两处生效：① 发给模型的 schema 会按 role 过滤
    #                        ② 执行前再兜底检查（防模型瞎编工具名）
    allowed_roles: set[str] | None = None

    @abstractmethod
    async def execute(self, arguments: BaseModel) -> ToolResult:
        ...

    def to_api_schema(self) -> dict[str, Any]:
        """转成（Anthropic 风格的）工具 schema。各 client 再翻译成自己的格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }
