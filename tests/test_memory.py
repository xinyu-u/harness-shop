"""阶段6 记忆 · 分层验证。

从底层到顶层，一层层验证。哪层挂了，问题就在那层。

跑法（项目根目录）：python -m tests.test_memory
"""

import asyncio
import os
from pathlib import Path

from core.memory import load_memory, append_memory
from core.client import FakeClient
from core.engine import QueryEngine
from core.events import ToolExecutionStarted, ToolExecutionCompleted, AssistantTurnComplete
from core.messages import ConversationMessage, TextBlock, ToolUseBlock
from business.store import MemoryStore
from business.cs_tools import build_tools

TEST_USER = "test_user"
MEM_FILE = f"memory_{TEST_USER}.md"


def cleanup():
    """每次测试前清掉测试记忆文件，保证干净。"""
    if Path(MEM_FILE).exists():
        os.remove(MEM_FILE)


# ───────────────────────── 第1层：记忆读写本身 ─────────────────────────

def test_layer1_memory_rw():
    """最底层：append 写进去，load 读出来。"""
    cleanup()
    assert load_memory(TEST_USER) == "", "空文件应返回空字符串"
    append_memory("用户穿42码鞋", TEST_USER)
    append_memory("用户买过Air Max", TEST_USER)
    content = load_memory(TEST_USER)
    assert "42码" in content and "Air Max" in content, "写进去的应该能读出来"
    print("✅ 第1层 记忆读写：append 能写、load 能读")


# ───────────────────── 第2层：system_prompt 带上了记忆 ─────────────────────

async def test_layer2_memory_in_system_prompt():
    """记忆是否被读出来、拼进 system_prompt、传到 client。"""
    cleanup()
    append_memory("用户穿42码鞋", TEST_USER)

    fake = FakeClient(scripted=[
        ConversationMessage(role="assistant", content=[TextBlock(text="好的")])
    ])
    engine = QueryEngine(fake, build_tools(MemoryStore(), TEST_USER), user_id=TEST_USER)

    _ = [e async for e in engine.submit_message("你好")]

    # FakeClient 记录了收到的 system_prompt，检查里面有没有记忆
    assert fake.last_system_prompt is not None, "system_prompt 应该被传给 client"
    assert "42码" in fake.last_system_prompt, "记忆应该被拼进 system_prompt"
    print("✅ 第2层 注入：记忆被读出 → 拼进 system_prompt → 传到 client")


# ───────────────────── 第3层：write_memory 工具能存 ─────────────────────

async def test_layer3_write_memory_tool():
    """模型调 write_memory 工具，记忆是否真写进文件。"""
    cleanup()

    # 脚本：第1次模型调 write_memory，第2次纯文字收尾
    fake = FakeClient(scripted=[
        ConversationMessage(role="assistant", content=[
            ToolUseBlock(name="write_memory", input={"content": "用户穿43码"})
        ]),
        ConversationMessage(role="assistant", content=[TextBlock(text="已经记下了")]),
    ])
    engine = QueryEngine(fake, build_tools(MemoryStore(), TEST_USER), user_id=TEST_USER)

    events = [e async for e in engine.submit_message("我穿43码，记一下")]

    # write_memory 应被执行
    assert any(
        isinstance(e, ToolExecutionStarted) and e.tool_name == "write_memory"
        for e in events
    ), "应该调用了 write_memory"
    # 文件里应有内容
    assert "43码" in load_memory(TEST_USER), "write_memory 应把内容写进文件"
    print("✅ 第3层 写入：模型调 write_memory → 内容进文件")


# ───────────────────── 第4层：完整跨会话（写→重启→读到）─────────────────────

async def test_layer4_cross_session():
    """模拟两次会话：第1次记住，第2次（新 engine）能想起。"""
    cleanup()

    # 第1次会话：模型调 write_memory 记住尺码
    fake1 = FakeClient(scripted=[
        ConversationMessage(role="assistant", content=[
            ToolUseBlock(name="write_memory", input={"content": "用户穿42码鞋"})
        ]),
        ConversationMessage(role="assistant", content=[TextBlock(text="记下了")]),
    ])
    engine1 = QueryEngine(fake1, build_tools(MemoryStore(), TEST_USER), user_id=TEST_USER)
    _ = [e async for e in engine1.submit_message("我穿42码")]

    # 第2次会话：全新 engine（模拟重启），看 system_prompt 是否带上了上次的记忆
    fake2 = FakeClient(scripted=[
        ConversationMessage(role="assistant", content=[TextBlock(text="您之前说穿42码")])
    ])
    engine2 = QueryEngine(fake2, build_tools(MemoryStore(), TEST_USER), user_id=TEST_USER)
    _ = [e async for e in engine2.submit_message("推荐个鞋码")]

    # 第2次会话的 system_prompt 应该包含第1次记的尺码
    assert "42码" in fake2.last_system_prompt, "新会话应能读到上次记的尺码"
    print("✅ 第4层 跨会话：第1次记住 → 新会话（重启）能想起")


def main():
    test_layer1_memory_rw()
    asyncio.run(test_layer2_memory_in_system_prompt())
    asyncio.run(test_layer3_write_memory_tool())
    asyncio.run(test_layer4_cross_session())
    cleanup()  # 收尾清理
    print("\n🎉 阶段6 记忆 全部验证通过")


if __name__ == "__main__":
    main()