"""用 FakeClient 测核心流程，不调真 API。"""

import asyncio
from core.client import FakeClient
from core.engine import run_query, QueryEngine
from core.events import AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted
from core.messages import ConversationMessage, TextBlock
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
    messages = [ConversationMessage(role="user", content=[TextBlock(text="测试")])]
    events = [
        e async for e in run_query(
            FakeClient(always_tool=True),    # 永远调工具，停不下来
            messages,
            build_tools(MemoryStore()),
            max_turns=3,
        )
    ]
    tool_runs = [e for e in events if isinstance(e, ToolExecutionCompleted)]
    assert len(tool_runs) == 3, f"应执行3次，实际{len(tool_runs)}次"
    # 最后一个事件应是保险丝收尾
    assert isinstance(events[-1], AssistantTurnComplete)
    assert "最大回合数" in events[-1].message.content[0].text
    print("✅ test_max_turns 通过（保险丝有效）")


if __name__ == "__main__":
    asyncio.run(test_check_stock_flow())
    asyncio.run(test_max_turns())