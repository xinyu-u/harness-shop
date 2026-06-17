"""上下文压缩（阶段4）。"""

from core.messages import TextBlock, ToolResultBlock, ToolUseBlock, ConversationMessage

TOKEN_ESTIMATION_PADDING = 4 / 3
DEFAULT_KEEP_RECENT = 5
TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"


def estimate_tokens(text: str) -> int:
    """粗估一段文字的 token。"""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def estimate_message_tokens(messages: list[ConversationMessage]) -> int:
    """粗估整个对话的 token。"""
    total = 0
    for msg in messages:
        for block in msg.content:
            if isinstance(block, TextBlock):
                total += estimate_tokens(block.text)
            elif isinstance(block, ToolResultBlock):
                total += estimate_tokens(block.content)
            elif isinstance(block, ToolUseBlock):
                total += estimate_tokens(block.name)
                total += estimate_tokens(str(block.input))
    return int(total * TOKEN_ESTIMATION_PADDING)


def should_compact(messages: list[ConversationMessage], threshold: int = 500) -> bool:
    """门卫：超阈值才压。"""
    return estimate_message_tokens(messages) >= threshold


def microcompact(messages: list[ConversationMessage], keep_recent: int = DEFAULT_KEEP_RECENT) -> list[ConversationMessage]:
    """把较旧的 ToolResultBlock 的 content 清成占位符，保留最近 keep_recent 个。"""
    keep_recent = max(1, keep_recent)
    all_ids = [
        block.tool_use_id
        for msg in messages
        for block in msg.content
        if isinstance(block, ToolResultBlock)
    ]
    if len(all_ids) <= keep_recent:
        return messages

    clear_ids = set(all_ids[:-keep_recent])
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolResultBlock) and block.tool_use_id in clear_ids:
                block.content = TIME_BASED_MC_CLEARED_MESSAGE
    return messages