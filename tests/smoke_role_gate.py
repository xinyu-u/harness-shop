"""验证 role 门禁：user 调商家工具被拦，merchant 能调通。"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.client import FakeClient
from core.engine import QueryEngine
from core.messages import ConversationMessage, ToolUseBlock, TextBlock
from core.events import AssistantTurnComplete, ToolExecutionCompleted
from business.store import MemoryStore
from business.cs_tools import build_tools


async def run_once(role):
    store = MemoryStore()
    tools = build_tools(store, user_id="tester")

    # 脚本：第1轮 → 模型调 update_price；第2轮 → 文字收尾
    scripted = [
        ConversationMessage(role="assistant", content=[
            ToolUseBlock(name="update_price", input={"product_id": "airmax", "price": 799})
        ]),
        ConversationMessage(role="assistant", content=[
            TextBlock(text="done")
        ]),
    ]
    engine = QueryEngine(FakeClient(scripted=scripted), tools, user_id="tester", role=role)

    tool_result_text = None
    is_error = None
    async for ev in engine.submit_message("test"):
        if isinstance(ev, ToolExecutionCompleted):
            tool_result_text = ev.output
            is_error = ev.is_error
    return tool_result_text, is_error, store


async def main():
    # 1) user 角色：被拦在 run_query 的兜底检查
    out, err, store = await run_once("user")
    assert err is True, f"user should be rejected, got err={err}"
    assert "权限不足" in out, f"unexpected: {out}"
    assert store.get_product("airmax")["price"] == 899, "价格不该被改"
    print("[1] user-role rejected at gate OK:", out)

    # 2) merchant 角色：通过
    out, err, store = await run_once("merchant")
    assert err is False, f"merchant should pass, got err={err}, out={out}"
    assert store.get_product("airmax")["price"] == 799, "价格该被改成 799"
    print("[2] merchant-role passes OK:", out)

    print("\n全部通过。")


if __name__ == "__main__":
    asyncio.run(main())
