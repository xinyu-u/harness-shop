"""CLI 入口：命令行客服 agent。

第一期用这个跑（纯命令行）。
第二期会把它换成 FastAPI 服务，第三期再加 Vue 前端——
但 engine/tools/store 这些核心都不用改，只是换"外壳"。
"""

import asyncio

from core.client import OpenAIClient
from core.engine import QueryEngine
from core.events import AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted
from core.messages import TextBlock
from business.store import MemoryStore
from business.cs_tools import build_tools


async def cli_confirm(tool_name, tool_input):
    answer = input(f"⚠️  确认 {tool_name}({tool_input})？(y/n): ")
    return answer.strip().lower() == "y"

async def main():
    store = MemoryStore()
    tools = build_tools(store)
    engine = QueryEngine(OpenAIClient(), tools, confirm=cli_confirm)

    print("客服 Agent（输入 quit 退出）")
    print("可用商品：airmax(Air Max鞋), tshirt(纯棉T恤)")
    while True:
        prompt = input("> ")
        if prompt.strip() in ("quit", "exit"):
            break
        async for event in engine.submit_message(prompt):
            if isinstance(event, ToolExecutionStarted):
                print(f"⏵ {event.tool_name} {event.tool_input}")
            elif isinstance(event, ToolExecutionCompleted):
                mark = "✗" if event.is_error else "→"
                print(f"  {mark} {event.output}")
            elif isinstance(event, AssistantTurnComplete):
                text = "".join(
                    b.text for b in event.message.content if isinstance(b, TextBlock)
                )
                if text:
                    print(f"[客服] {text}")


if __name__ == "__main__":
    asyncio.run(main())
