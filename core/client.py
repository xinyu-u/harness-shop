"""模型客户端：ModelClient 接口 + OpenAIClient + FakeClient。"""

import json
import os
from typing import Any, Protocol
from openai import AsyncOpenAI
from dotenv import load_dotenv

from core.messages import ConversationMessage, TextBlock, ToolUseBlock

load_dotenv()
os.environ["OPENAI_API_KEY"] = os.getenv("api_key", "")
os.environ["OPENAI_BASE_URL"] = os.getenv("base_url", "")


class ModelClient(Protocol):
    async def stream_message(
        self,
        messages: list[ConversationMessage],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> ConversationMessage:
        ...


class FakeClient:
    """假 client：脚本化回复，测试用。可注入脚本。"""

    def __init__(self, scripted: list[ConversationMessage] | None = None):
        self.call_count = 0
        self.scripted = scripted
        self.last_system_prompt = None   #  记录收到的 system_prompt，方便测试断言

    async def stream_message(self, messages, tools=None, system_prompt=None):
        self.last_system_prompt = system_prompt
        self.call_count += 1
        if self.scripted is not None:
            idx = min(self.call_count - 1, len(self.scripted) - 1)
            return self.scripted[idx]
        # 默认脚本：第1次调 check_stock，之后纯文字
        if self.call_count == 1:
            return ConversationMessage(role="assistant", content=[
                ToolUseBlock(name="check_stock", input={"product_id": "airmax", "size": "42"})
            ])
        return ConversationMessage(role="assistant", content=[
            TextBlock(text="42码有货，库存5件")
        ])


class OpenAIClient:
    def __init__(self, model: str = "gpt-4o-mini"):
        self._client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
        )
        self._model = model

    async def stream_message(self, messages, tools=None, system_prompt=None):
        openai_messages = self._to_openai(messages)
        if system_prompt:
            openai_messages.insert(0, {"role": "system", "content": system_prompt})
        kwargs: dict[str, Any] = {"model": self._model, "messages": openai_messages}
        if tools:
            kwargs["tools"] = self._convert_tools_to_openai(tools)
        response = await self._client.chat.completions.create(**kwargs)
        return self._from_openai(response)

    def _to_openai(self, messages):
        openai_messages = []
        for msg in messages:
            if msg.role == "assistant":
                openai_msg = {"role": "assistant"}
                text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                openai_msg["content"] = text if text else None
                if msg.tool_uses:
                    openai_msg["tool_calls"] = [
                        {"id": tu.id, "type": "function",
                         "function": {"name": tu.name, "arguments": json.dumps(tu.input)}}
                        for tu in msg.tool_uses
                    ]
                openai_messages.append(openai_msg)
            elif msg.role == "user":
                for tr in msg.tool_results:
                    openai_messages.append({"role": "tool", "tool_call_id": tr.tool_use_id, "content": tr.content})
                text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                if text:
                    openai_messages.append({"role": "user", "content": text})
        return openai_messages

    def _from_openai(self, response):
        message = response.choices[0].message
        content = []
        if message.content:
            content.append(TextBlock(text=message.content))
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                content.append(ToolUseBlock(id=tc.id, name=tc.function.name, input=args))
        return ConversationMessage(role="assistant", content=content)

    def _convert_tools_to_openai(self, tools):
        return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})}} for t in tools]