"""用 FakeClient 测核心流程，不调真 API。"""

import asyncio
from core.client import FakeClient
from core.engine import run_query, QueryEngine, RunConfig
from core.events import AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted
from core.messages import ConversationMessage, TextBlock, ToolUseBlock
from business.store import MemoryStore
from business.cs_tools import build_tools


async def test_check_stock_flow():
    """测：模型决定调 check_stock -> 工具执行 -> 模型收尾回复。"""
    engine = QueryEngine(FakeClient(), build_tools(MemoryStore()))
    events = [e async for e in engine.submit_message("42码有货吗")]

    # 应该出现 check_stock 的执行
    assert any(
        isinstance(e, ToolExecutionStarted) and e.tool_name == "check_stock"
        for e in events
    ), "没有调用 check_stock"

    # 工具结果应该是"有货"
    completed = [e for e in events if isinstance(e, ToolExecutionCompleted)]
    assert completed and "有货" in completed[0].output, "库存查询结果不对"

    # 最后应该有 assistant 收尾
    assert any(isinstance(e, AssistantTurnComplete) for e in events), "没有收尾回复"
    print("✅ test_check_stock_flow 通过")

async def test_max_turns():
    """测保险丝：FakeClient 永远调工具，max_turns 必须拦住。"""
    # 单条脚本=工具调用：stream_message 的 idx 钳到末尾，所以每回合都返回它
    # → 模型"永远调工具、停不下来"，正好压测保险丝。
    always_tool = ConversationMessage(role="assistant", content=[
        ToolUseBlock(name="check_stock", input={"product_id": "airmax", "size": "42"})
    ])
    config = RunConfig(
        client=FakeClient(scripted=[always_tool]),
        tools=build_tools(MemoryStore()),
        max_turns=3,
    )
    messages = [ConversationMessage(role="user", content=[TextBlock(text="测试")])]
    events = [e async for e in run_query(config, messages)]
    tool_runs = [e for e in events if isinstance(e, ToolExecutionCompleted)]
    assert len(tool_runs) == 3, f"应执行3次，实际{len(tool_runs)}次"
    # 最后一个事件应是保险丝收尾
    assert isinstance(events[-1], AssistantTurnComplete)
    assert "最大回合数" in events[-1].message.content[0].text
    print("✅ test_max_turns 通过（保险丝有效）")


if __name__ == "__main__":
    asyncio.run(test_check_stock_flow())
    asyncio.run(test_max_turns())