"""测 reduce_events 的采集口径：tool_calls 来自 Started，results 来自 Completed。"""

from core.events import (
    AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted,
)
from core.messages import ConversationMessage, TextBlock
from eval.harness import reduce_events


def test_reduce_collects_intent_and_execution_separately():
    events = [
        ToolExecutionStarted(tool_name="update_price", tool_input={"product_id": "airmax", "price": 1}),
        ToolExecutionCompleted(tool_name="update_price", output="权限不足：...", is_error=True),
        AssistantTurnComplete(message=ConversationMessage(
            role="assistant", content=[TextBlock(text="抱歉，无法改价。")])),
    ]
    trace = reduce_events(events, prompt="改价", role="user", store=None)

    # 意图：模型选了 update_price（即使后面被拦）
    assert trace.called("update_price")
    # 执行：没有成功执行（is_error=True）
    assert not trace.executed_ok("update_price")
    assert trace.final_text == "抱歉，无法改价。"
    print("[PASS] test_reduce_collects_intent_and_execution_separately")


if __name__ == "__main__":
    test_reduce_collects_intent_and_execution_separately()
