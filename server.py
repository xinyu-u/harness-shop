"""HTTP 入口：把 QueryEngine 包成 /chat 接口。

跑：uvicorn server:app --reload
测：打开 http://localhost:8000/docs
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.client import OpenAIClient
from core.engine import QueryEngine
from core.events import AssistantTurnComplete
from core.messages import TextBlock
from business.store import SqliteStore
from business.cs_tools import build_tools


app = FastAPI()

# 允许前端 file:// 或 localhost 直接调用 /chat（本地开发用，生产要收紧 origins）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# store 全局只建一次：商品/库存/订单是公共数据，所有用户共享同一个数据库
store = SqliteStore()

# 每个用户一个独立 engine：会话历史和记忆按 user_id 隔离
sessions: dict[str, QueryEngine] = {}


def get_engine(user_id: str) -> QueryEngine:
    # 归一化为小写：避免 "A" 和 "a" 被当成不同用户。
    # Python dict 是大小写敏感的，但 Windows 文件系统不是——
    # 不归一化会导致 sessions 里两个 engine，却共享同一个 memory_*.md 文件。
    user_id = user_id.lower()
    if user_id not in sessions:
        sessions[user_id] = QueryEngine(
            OpenAIClient(),
            build_tools(store, user_id),   # user_id 传给工具 → 记忆写入 memory_{user_id}.md
            user_id=user_id,
        )
    return sessions[user_id]


class ChatRequest(BaseModel):
    user_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------- 前端静态文件 -------------
# frontend/ 目录下的 index.html / 资源都直接由 FastAPI 提供
# 访问 http://localhost:8000/ → 渲染聊天页
FRONTEND_DIR = Path(__file__).parent / "frontend"


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


# 其余静态资源（如果以后拆 css/js）走 /static/...
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.post("/chat")
async def chat(req: ChatRequest) -> ChatResponse:
    engine = get_engine(req.user_id)
    reply_text = ""
    async for event in engine.submit_message(req.message):
        if isinstance(event, AssistantTurnComplete):
            reply_text = "".join(
                b.text for b in event.message.content if isinstance(b, TextBlock)
            )
    return ChatResponse(reply=reply_text)
