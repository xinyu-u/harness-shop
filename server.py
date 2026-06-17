"""HTTP 入口：把 QueryEngine 包成带认证的 /chat 接口。

跑：uvicorn server:app --reload
测：打开 http://localhost:8000/  或  http://localhost:8000/docs

认证流程：
  1. POST /register {user_id, password}      → 永远建普通用户 (role='user')
  2. POST /login    {user_id, password}      → 返回 JWT
  3. POST /chat     Authorization: Bearer <token>  {message}

商家账号不能自助注册，由 seed_merchant.py 在数据库里直接种（防越权）。
"""

import json
import os
from pathlib import Path

import jwt
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.auth import hash_password, verify_password, create_token, decode_token
from core.client import OpenAIClient
from core.engine import QueryEngine
from core.events import AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted
from core.messages import TextBlock
from business.store import SqliteStore
from business.cs_tools import build_tools


app = FastAPI()

# 本地开发：放开 CORS。生产要收紧到具体 origins。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# store 全局只建一次：商品/库存/订单/账号都在这里
store = SqliteStore()


def _seed_merchant_from_env() -> None:
    """启动时按 .env 把商家账号种到 users 表。

    .env 是真相源：每次启动都把 users 表里这条记录对齐到 env 声明的密码 + role=merchant。
    这样改 .env 重启就生效，不需要手动跑脚本。

    `merchant_user` / `merchant_password` 任一缺失就跳过——等于关掉此功能，
    所有商家都得走 SQL 手种或后续的管理端。
    """
    uid = (os.getenv("merchant_user") or "").strip().lower()
    pw = os.getenv("merchant_password") or ""
    if not uid or not pw:
        return
    pw_hash = hash_password(pw)
    if store.get_user(uid) is None:
        store.create_user(uid, pw_hash, role="merchant")
        print(f"[seed] 新建商家账号 user_id={uid}")
    else:
        # 已存在 → 按 env 覆盖密码 + 强制 role=merchant（跟 seed_merchant.py --force 同语义）
        store._conn.execute(
            "UPDATE users SET password_hash = ?, role = 'merchant' WHERE user_id = ?",
            (pw_hash, uid),
        )
        store._conn.commit()
        print(f"[seed] 刷新商家账号 user_id={uid}")


_seed_merchant_from_env()

# sessions 用 (user_id, role) 做键：
# 同一用户切换 role 重登（演示用脚本提权后）会拿到不同 engine，
# 避免缓存里 RunConfig.role 与新 token 不一致。
sessions: dict[tuple[str, str], QueryEngine] = {}


def get_engine(user_id: str, role: str) -> QueryEngine:
    key = (user_id, role)
    if key not in sessions:
        sessions[key] = QueryEngine(
            OpenAIClient(),
            build_tools(store, user_id),
            user_id=user_id,
            role=role,
        )
    return sessions[key]


# ------------- 模型 -------------

class AuthRequest(BaseModel):
    user_id: str
    password: str


class AuthResponse(BaseModel):
    token: str
    user_id: str
    role: str


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


TOOL_PROGRESS_TEXT: dict[str, tuple[str, str]] = {
    "search_products": ("正在找商品…", "找到了相关商品"),
    "check_stock": ("正在查库存…", "查到了库存"),
    "recommend_size": ("正在推荐尺码…", "尺码建议已生成"),
    "get_order_status": ("正在查订单…", "订单状态查到了"),
    "place_order": ("正在提交订单…", "订单处理完成"),
    "cancel_order": ("正在取消订单…", "取消结果已确认"),
    "update_price": ("正在改价…", "价格已更新"),
    "add_product": ("正在上架商品…", "上架结果已确认"),
    "write_memory": ("正在记录偏好…", "已记住偏好"),
}


def _assistant_text(event: AssistantTurnComplete) -> str:
    return "".join(
        b.text for b in event.message.content if isinstance(b, TextBlock)
    )


def _tool_status(tool_name: str, phase: str, is_error: bool = False) -> str:
    if is_error:
        return "这一步遇到问题，正在整理结果…"
    started, completed = TOOL_PROGRESS_TEXT.get(tool_name, ("正在处理…", "处理完成"))
    return started if phase == "started" else completed


def _event_line(event_type: str, **payload) -> str:
    payload["type"] = event_type
    return json.dumps(payload, ensure_ascii=False) + "\n"


# ------------- 认证依赖：从 Authorization 头里解出 (user_id, role) -------------

def _auth(authorization: str | None) -> tuple[str, str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"token 无效或已过期: {e}")
    user_id = payload.get("sub")
    role = payload.get("role", "user")
    if not user_id:
        raise HTTPException(status_code=401, detail="token 缺少 sub")
    return user_id, role


# ------------- 端点 -------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/register", response_model=AuthResponse)
def register(req: AuthRequest):
    """自助注册：永远建 role='user'。
    商家账号不通过这条路——避免任何人 curl 一下就提权。"""
    uid = req.user_id.strip().lower()
    if not uid or not req.password:
        raise HTTPException(status_code=400, detail="user_id 和 password 都是必填")
    if len(req.password) < 4:
        raise HTTPException(status_code=400, detail="密码至少 4 位")
    if store.get_user(uid) is not None:
        raise HTTPException(status_code=409, detail=f"用户 {uid} 已存在")
    try:
        store.create_user(uid, hash_password(req.password), role="user")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    token = create_token(uid, "user")
    return AuthResponse(token=token, user_id=uid, role="user")


@app.post("/login", response_model=AuthResponse)
def login(req: AuthRequest):
    uid = req.user_id.strip().lower()
    u = store.get_user(uid)
    # 不区分"用户不存在"和"密码错"——避免账号枚举
    if u is None or not verify_password(req.password, u["password_hash"]):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    token = create_token(uid, u["role"])
    return AuthResponse(token=token, user_id=uid, role=u["role"])


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, authorization: str | None = Header(default=None)):
    user_id, role = _auth(authorization)
    engine = get_engine(user_id, role)
    reply_text = ""
    async for event in engine.submit_message(req.message):
        if isinstance(event, AssistantTurnComplete):
            reply_text = "".join(
                b.text for b in event.message.content if isinstance(b, TextBlock)
            )
    return ChatResponse(reply=reply_text)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, authorization: str | None = Header(default=None)):
    user_id, role = _auth(authorization)
    engine = get_engine(user_id, role)

    async def generate():
        yield _event_line("status", text="正在理解你的需求…", busy=True)
        try:
            async for event in engine.submit_message(req.message):
                if isinstance(event, ToolExecutionStarted):
                    yield _event_line(
                        "status",
                        text=_tool_status(event.tool_name, "started"),
                        tool_name=event.tool_name,
                        phase="started",
                        busy=True,
                    )
                elif isinstance(event, ToolExecutionCompleted):
                    yield _event_line(
                        "status",
                        text=_tool_status(event.tool_name, "completed", event.is_error),
                        tool_name=event.tool_name,
                        phase="completed",
                        is_error=event.is_error,
                        busy=False,
                    )
                elif isinstance(event, AssistantTurnComplete):
                    yield _event_line("final", reply=_assistant_text(event))
        except Exception as e:
            yield _event_line("error", message=str(e))

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ------------- 前端静态文件 -------------
FRONTEND_DIR = Path(__file__).parent / "frontend"


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
