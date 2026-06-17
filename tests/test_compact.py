"""验证阶段4：上下文压缩。

测三件事：
1. estimate_message_tokens 能算出 token
2. should_compact 门卫能判断超阈值
3. microcompact 清掉旧工具结果、保留最近 keep_recent 个、token 下降
"""

import asyncio
from core.compact import estimate_message_tokens, microcompact, should_compact
from core.messages import ConversationMessage, TextBlock, ToolResultBlock

CLEARED = "[Old tool result content cleared]"


def build_long_conversation(n: int = 8) -> list[ConversationMessage]:
    """造 n 轮"调工具 + 工具结果"的对话，每个结果内容很长。"""
    msgs = []
    for i in range(n):
        # assistant 调工具
        msgs.append(ConversationMessage(role="assistant", content=[TextBlock(text="调工具")]))
        # user 塞回工具结果（内容很长，模拟查了一堆数据）
        msgs.append(ConversationMessage(role="user", content=[
            ToolResultBlock(tool_use_id=f"t{i}", content=f"第{i}次查询的库存数据明细" * 50)
        ]))
    return msgs


def dump(messages, title):
    """把对话里每个工具结果的内容摆出来看（肉眼确认压缩真发生）。"""
    print(f"\n----- {title}（token={estimate_message_tokens(messages)}）-----")
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                preview = block.content[:30] + ("..." if len(block.content) > 30 else "")
                print(f"  [{block.tool_use_id}] {preview}")


def test_estimate_tokens():
    msgs = build_long_conversation(8)
    tokens = estimate_message_tokens(msgs)
    assert tokens > 0, "token 估算应大于 0"
    print(f"✅ estimate_message_tokens: {tokens} tokens")


def test_should_compact():
    short = [ConversationMessage(role="user", content=[TextBlock(text="你好")])]
    long = build_long_conversation(8)
    assert should_compact(short) is False, "短对话不该触发压缩"
    assert should_compact(long) is True, "长对话应触发压缩"
    print("✅ should_compact: 短对话不压、长对话压")


def test_microcompact():
    msgs = build_long_conversation(8)
    before = estimate_message_tokens(msgs)

    microcompact(msgs, keep_recent=3)   # 保留最近3个，其余清掉
    after = estimate_message_tokens(msgs)

    # token 应下降
    assert after < before, f"压缩后 token 应下降（前{before} 后{after}）"

    # 数被清掉的 和 保留的
    cleared = sum(
        1 for m in msgs for b in m.content
        if isinstance(b, ToolResultBlock) and b.content == CLEARED
    )
    kept = sum(
        1 for m in msgs for b in m.content
        if isinstance(b, ToolResultBlock) and b.content != CLEARED
    )
    assert cleared == 5, f"应清掉5个（8-3），实际{cleared}"
    assert kept == 3, f"应保留3个，实际{kept}"

    # 验证配对没被破坏：所有 ToolResultBlock 的 tool_use_id 还在
    ids = [b.tool_use_id for m in msgs for b in m.content if isinstance(b, ToolResultBlock)]
    assert len(ids) == 8, "工具结果块数量不该变（只清内容、不删块）"

    print(f"✅ microcompact: token {before} → {after}，清掉{cleared}个、保留{kept}个、块数不变(配对守住)")


def test_keep_recent_boundary():
    """工具结果还没超过 keep_recent 时，不该清任何东西。"""
    msgs = build_long_conversation(2)   # 只有2个工具结果
    microcompact(msgs, keep_recent=3)   # keep_recent=3 > 2
    cleared = sum(
        1 for m in msgs for b in m.content
        if isinstance(b, ToolResultBlock) and b.content == CLEARED
    )
    assert cleared == 0, "结果数没超过 keep_recent，不该清"
    print("✅ keep_recent 边界：结果不足时不清")


def test_visual():
    """肉眼对比：压缩前后每个工具结果的内容。"""
    msgs = build_long_conversation(8)
    dump(msgs, "压缩前")
    microcompact(msgs, keep_recent=3)
    dump(msgs, "压缩后（保留最近3个）")


if __name__ == "__main__":
    test_estimate_tokens()
    test_should_compact()
    test_microcompact()
    test_keep_recent_boundary()
    test_visual()
    print("\n🎉 阶段4 压缩全部验证通过")