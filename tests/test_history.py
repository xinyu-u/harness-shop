"""聊天档案（步骤2 · server 层）验证。

两块新逻辑：
  A. /history 查询接口：user_id 从 token 取，只查自己的（归属隔离）；
     透传 limit/before 游标；无 token 被拒。
  B. /chat/stream 写入：存「user 消息」+「assistant 最终回复」两条，
     不存工具过程（中间的 ToolExecution* 事件不落库）。

写入路径用假 engine 驱动，不打真实 LLM。

跑法（项目根目录）：python -m tests.test_history
"""

import asyncio

from fastapi import HTTPException

from business.store import SqliteStore
from core.auth import create_token
from core.events import AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted
from core.messages import ConversationMessage, TextBlock
import server


def _fresh_server_store():
    """把 server 的全局 store 换成隔离的内存库，避免污染 shop.db。"""
    server.store = SqliteStore(":memory:")
    return server.store


# ════════════════ A. /history 查询接口 ════════════════

def test_history_returns_own_messages_in_order():
    store = _fresh_server_store()
    store.save_message("alice", "user", "hi")
    store.save_message("alice", "assistant", "yo")

    resp = server.get_history(f"Bearer {create_token('alice', 'user')}")

    assert [(m["role"], m["content"]) for m in resp["messages"]] == [
        ("user", "hi"), ("assistant", "yo"),
    ], "应按对话顺序返回自己的消息"
    print("✅ 步骤2A /history：返回自己的消息、正序")


def test_history_isolated_per_user():
    store = _fresh_server_store()
    store.save_message("alice", "user", "alice 的秘密")
    store.save_message("bob", "user", "bob 的秘密")

    resp = server.get_history(f"Bearer {create_token('bob', 'user')}")

    assert [m["content"] for m in resp["messages"]] == ["bob 的秘密"], \
        "user_id 从 token 取，bob 只看到自己的、看不到 alice 的"
    print("✅ 步骤2A 归属隔离：换用户只看到自己的历史")


def test_history_requires_auth():
    _fresh_server_store()
    try:
        server.get_history(None)
        assert False, "无 token 应被拒"
    except HTTPException as e:
        assert e.status_code == 401, "缺 token → 401"
    print("✅ 步骤2A 无凭证：/history 必须登录")


def test_history_passes_before_cursor():
    store = _fresh_server_store()
    for i in range(1, 6):
        store.save_message("alice", "user", f"m{i}")
    token = f"Bearer {create_token('alice', 'user')}"

    third_time = server.get_history(token)["messages"][2]["created_at"]
    earlier = server.get_history(token, before=third_time)["messages"]

    assert [m["content"] for m in earlier] == ["m1", "m2"], \
        "before 游标应透传到 store，只取更早的 2 条"
    print("✅ 步骤2A 游标透传：/history?before= 只返回更早的")


# ════════════════ B. /chat/stream 写入路径 ════════════════

class _FakeEngine:
    """假 engine：先吐一轮工具事件，再吐最终回复。
    用来验证写入只落 user + assistant final，不落工具过程。"""

    def __init__(self, reply: str, tool_name: str | None = None):
        self._reply = reply
        self._tool_name = tool_name

    async def submit_message(self, message: str):
        if self._tool_name:
            yield ToolExecutionStarted(self._tool_name, {})
            yield ToolExecutionCompleted(self._tool_name, "工具内部输出", False)
        yield AssistantTurnComplete(
            ConversationMessage(role="assistant", content=[TextBlock(text=self._reply)])
        )


async def _drive_stream(req, authorization):
    """调 chat_stream 并把 StreamingResponse 完整消费掉（驱动 generate 跑完）。"""
    resp = await server.chat_stream(req, authorization)
    async for _ in resp.body_iterator:
        pass


def test_chat_stream_saves_user_and_assistant_only():
    store = _fresh_server_store()
    server.sessions[("alice", "user")] = _FakeEngine("有什么可以帮你的", tool_name="search_products")
    token = f"Bearer {create_token('alice', 'user')}"

    asyncio.run(_drive_stream(server.ChatRequest(message="我想买鞋"), token))

    msgs = store.get_messages("alice")
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "我想买鞋"),
        ("assistant", "有什么可以帮你的"),
    ], f"应只落 user 消息 + assistant 最终回复（不含工具过程），得到 {msgs}"
    print("✅ 步骤2B 写入：只存 user + assistant final，不存工具过程")


def main():
    test_history_returns_own_messages_in_order()
    test_history_isolated_per_user()
    test_history_requires_auth()
    test_history_passes_before_cursor()
    test_chat_stream_saves_user_and_assistant_only()
    print("\n🎉 聊天档案 步骤2 server 层 全部验证通过")


if __name__ == "__main__":
    main()
