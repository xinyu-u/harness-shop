"""引擎核心：RunConfig + run_query（执行器）+ QueryEngine（管理器）。"""

from dataclasses import dataclass
from typing import Callable

from core.messages import ConversationMessage, TextBlock, ToolResultBlock
from core.events import AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted
from core.tools import BaseTool
from core.client import ModelClient
from core.compact import should_compact, microcompact
from core.memory import load_memory, append_memory


@dataclass
class RunConfig:
    """一次会话里不变的配置/依赖。"""
    client: ModelClient
    tools: dict[str, BaseTool]
    max_turns: int = 8
    confirm: Callable | None = None
    system_prompt: str | None = None


async def run_query(config: RunConfig, messages: list[ConversationMessage]):
    """对话循环。config=不变配置，messages=流动数据。"""
    tool_schemas = [t.to_api_schema() for t in config.tools.values()]

    turn_count = 0
    while turn_count < config.max_turns:
        turn_count += 1

        if should_compact(messages):
            messages = microcompact(messages)

        try:
            reply = await config.client.stream_message(
                messages, tool_schemas, system_prompt=config.system_prompt
            )
        except Exception as e:
            yield AssistantTurnComplete(
                message=ConversationMessage(role="assistant",
                    content=[TextBlock(text=f"（模型调用失败：{e}）")])
            )
            return
        messages.append(reply)

        if not reply.tool_uses:
            yield AssistantTurnComplete(message=reply)
            return

        for tc in reply.tool_uses:
            yield ToolExecutionStarted(tool_name=tc.name, tool_input=tc.input)

            tool = config.tools.get(tc.name)
            if tool is None:
                result = ToolResultBlock(tool_use_id=tc.id, content=f"Unknown tool: {tc.name}", is_error=True)
                yield ToolExecutionCompleted(tool_name=tc.name, output=result.content, is_error=True)
                messages.append(ConversationMessage(role="user", content=[result]))
                continue

            try:
                parsed = tool.input_model.model_validate(tc.input)
            except Exception as exc:
                result = ToolResultBlock(tool_use_id=tc.id, content=f"Invalid input for {tc.name}: {exc}", is_error=True)
                yield ToolExecutionCompleted(tool_name=tc.name, output=result.content, is_error=True)
                messages.append(ConversationMessage(role="user", content=[result]))
                continue

            if tool.is_write and config.confirm is not None:
                ok = await config.confirm(tool.name, tc.input)
                if not ok:
                    result = ToolResultBlock(tool_use_id=tc.id, content="用户取消了操作", is_error=True)
                    yield ToolExecutionCompleted(tool_name=tc.name, output=result.content, is_error=True)
                    messages.append(ConversationMessage(role="user", content=[result]))
                    continue

            tool_result = await tool.execute(parsed)
            result = ToolResultBlock(tool_use_id=tc.id, content=tool_result.output, is_error=tool_result.is_error)
            yield ToolExecutionCompleted(tool_name=tc.name, output=result.content, is_error=result.is_error)
            messages.append(ConversationMessage(role="user", content=[result]))

    yield AssistantTurnComplete(
        message=ConversationMessage(role="assistant",
            content=[TextBlock(text=f"（达到最大回合数 {config.max_turns}，结束对话）")])
    )


class QueryEngine:
    """管理器：持有会话状态 + 配置。"""

    def __init__(self, client, tools, max_turns: int = 8, confirm=None, user_id: str = "default"):
        self._config = RunConfig(client=client, tools=tools, max_turns=max_turns, confirm=confirm)
        self._messages: list[ConversationMessage] = []
        self._user_id = user_id

    async def submit_message(self, prompt: str):
        # 阶段6：读记忆，拼进 system_prompt（每次 submit 更新）
        memory = load_memory(self._user_id)
        self._config.system_prompt = (
            "你是电商客服助手，帮用户查商品、查库存、推荐尺码、下单。"
            + (f"\n\n# 你记得的用户信息\n{memory}\n" if memory else "")
        )
        self._messages.append(
            ConversationMessage(role="user", content=[TextBlock(text=prompt)])
        )
        async for event in run_query(self._config, self._messages):
            yield event