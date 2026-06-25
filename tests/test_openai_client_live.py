"""OpenAIClient 真实 API 冒烟（需 .env 里配置 api_key / base_url）。"""

import asyncio
import os
import time

import pytest

from core.client import OpenAIClient, _api_timeout
from core.messages import ConversationMessage, TextBlock


async def _plain_reply():
    client = OpenAIClient()
    t0 = time.time()
    reply = await client.stream_message(
        [ConversationMessage(role="user", content=[TextBlock(text="用一句话介绍你自己")])]
    )
    elapsed = time.time() - t0
    text = "".join(b.text for b in reply.content if hasattr(b, "text"))
    assert text.strip(), "empty reply"
    print(f"[PASS] test_plain_reply ({elapsed:.1f}s) -> {text[:80]}")
    return elapsed


async def _tool_call_intent():
    tools = [{
        "name": "check_stock",
        "description": "查库存",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "size": {"type": "string"},
            },
            "required": ["product_id", "size"],
        },
    }]
    client = OpenAIClient()
    t0 = time.time()
    reply = await client.stream_message(
        [ConversationMessage(role="user", content=[TextBlock(text="airmax 42码还有几件")])],
        tools=tools,
        system_prompt="你是电商客服，需要查库存时必须调用工具。",
    )
    elapsed = time.time() - t0
    names = [tu.name for tu in reply.tool_uses]
    assert names == ["check_stock"], f"unexpected tools: {names}"
    print(f"[PASS] test_tool_call_intent ({elapsed:.1f}s) tools={names}")
    return elapsed


_NO_KEY = not os.getenv("api_key")
_SKIP_REASON = "live API 测试：需 .env 配 api_key，离线/CI 默认跳过"


@pytest.mark.skipif(_NO_KEY, reason=_SKIP_REASON)
def test_plain_reply():
    asyncio.run(_plain_reply())


@pytest.mark.skipif(_NO_KEY, reason=_SKIP_REASON)
def test_tool_call_intent():
    asyncio.run(_tool_call_intent())


def main():
    print(f"timeout={_api_timeout()}s base_url={os.getenv('OPENAI_BASE_URL')}")
    test_plain_reply()
    test_tool_call_intent()


if __name__ == "__main__":
    main()
