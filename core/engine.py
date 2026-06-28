"""引擎核心：RunConfig + run_query（执行器）+ QueryEngine（管理器）。"""

from dataclasses import dataclass
from typing import Callable

from core.messages import ConversationMessage, TextBlock, ToolResultBlock
from core.events import AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted
from core.tools import BaseTool, request_token_var
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
    role: str = "user"   # 当前会话角色：决定哪些工具可见可用（步骤5）


async def run_query(config: RunConfig, messages: list[ConversationMessage]):
    """对话循环。config=不变配置，messages=流动数据。"""
    # 按 role 过滤可见工具：模型根本看不到自己用不了的工具，
    # 既省 token，又避免"模型尝试 → 被拒 → 再尝试"的来回。
    visible_tools = {
        name: t for name, t in config.tools.items()
        if t.allowed_roles is None or config.role in t.allowed_roles
    }
    tool_schemas = [t.to_api_schema() for t in visible_tools.values()]

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
            msg = str(e).lower()
            if any(k in msg for k in ("connect", "timeout", "network")):
                text = "网络好像不太稳定，请稍后重试。"
            else:
                text = "服务暂时出了点问题，请稍后重试。"
            yield AssistantTurnComplete(message=ConversationMessage(role="assistant",
                content=[TextBlock(text=text)]))
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

            # 角色门禁兜底：理论上模型看不到这工具就不会调它，
            # 但如果它瞎编名字蒙对了，也要在执行前拦下来。
            if tool.allowed_roles is not None and config.role not in tool.allowed_roles:
                result = ToolResultBlock(
                    tool_use_id=tc.id,
                    content=f"权限不足：{tc.name} 仅限 {sorted(tool.allowed_roles)} 角色使用，当前 role={config.role}",
                    is_error=True,
                )
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
            yield ToolExecutionCompleted(
                tool_name=tc.name, output=result.content, is_error=result.is_error,
                metadata=tool_result.metadata,
            )
            messages.append(ConversationMessage(role="user", content=[result]))

    yield AssistantTurnComplete(
        message=ConversationMessage(role="assistant",
            content=[TextBlock(text=f"（达到最大回合数 {config.max_turns}，结束对话）")])
    )


class QueryEngine:
    """管理器：持有会话状态 + 配置。"""

    def __init__(self, client, tools, max_turns: int = 8, confirm=None,
                 user_id: str = "default", role: str = "user"):
        self._config = RunConfig(
            client=client, tools=tools, max_turns=max_turns,
            confirm=confirm, role=role,
        )
        self._messages: list[ConversationMessage] = []
        self._user_id = user_id
        self._role = role

    async def submit_message(self, prompt: str, request_token: str | None = None):
        # 本轮令牌写入 per-task 上下文，供 place_order 工具 .get()。
        # submit_message 是 async generator，在调用方同一个 Task 内被迭代，
        # 故此处 set 对后续 run_query → tool.execute 可见；并发的另一轮在自己的 Task 上下文里，互不影响。
        request_token_var.set(request_token)
        # 阶段6：读记忆，拼进 system_prompt（每次 submit 更新）
        memory = load_memory(self._user_id)
        role_hint = "（你正在和商家对话，可以使用商家专属工具改价/上架）" if self._role == "merchant" else ""
        self._config.system_prompt = (
            "你是电商客服助手，帮用户查商品、查库存、推荐尺码、下单。"
            + role_hint
            + "\n\n# 实时数据规则\n"
              "价格、库存、订单状态都是会随时变化的实时数据。每当用户问到这些，"
              "必须重新调用对应工具查询，绝不能拿对话历史里出现过的旧数值直接回答"
              "——那些随时可能已经被改过、过时了。"
              "\n\n# 尺码铁律\n"
              "你绝不知道任何商品有哪些尺码，也不能凭常识假设（不要默认 T恤是 S/M/L/XL、牛仔裤是 28-32）。"
              "需要某商品的尺码或各尺码库存时，一律先调 list_stock 拿到真实尺码再回答；"
              "只查某一个尺码用 check_stock。回答里出现的每一个尺码都必须来自工具结果，"
              "工具没返回的尺码就是不存在，绝不能补全或编造。"
              "\n\n# 交互规范\n"
              "当工具需要必填参数（如尺码、数量）而用户未提供时，千万不要自行猜测或使用默认值，请直接用友好的语气反问用户补充信息。"
              "补货时若 restock_product 报\"没有这个尺码\"，不要自行新增，应把工具列出的真实尺码告诉商家、确认后再补。"
              "\n\n# 尺码记忆\n"
              "当用户明确告知/确认自己的尺码时，顺手调一次 write_memory 记住："
              "鞋码用 key=\"shoe_size\"、上衣码用 key=\"top_size\"，value 写具体尺码。"
              "若之前已记过同类尺码，必须用同一个 key 再写一次来更新（覆盖旧值），不要新开 key"
              "——记忆只保留最新尺码。只记尺码这一类，订单/品类/交付不用记。"
            + (f"\n\n# 你记得的用户信息\n{memory}\n" if memory else "")
        )
        self._messages.append(
            ConversationMessage(role="user", content=[TextBlock(text=prompt)])
        )
        async for event in run_query(self._config, self._messages):
            yield event
