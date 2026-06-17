"""认证：密码哈希 + JWT 签发/校验。

密码：bcrypt（自带盐 + 自适应代价因子）。
令牌：JWT HS256，payload = {sub, role, exp}。
SECRET_KEY 从环境变量读，缺失就启动失败——不偷偷用默认值。
"""

import os
import time
from typing import Any

import bcrypt
import jwt
from dotenv import load_dotenv

load_dotenv()

_SECRET = os.getenv("jwt_secret")
if not _SECRET:
    raise RuntimeError(
        "环境变量 jwt_secret 未设置。请在 .env 里加一行 jwt_secret=<随机长串>。"
    )

_ALGO = "HS256"
TOKEN_TTL_SECONDS = 24 * 3600   # 24h


def hash_password(plain: str) -> str:
    """bcrypt 哈希：自动加盐，存的字符串里已经包含 salt 和代价因子。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # 哈希格式错（比如老数据），当成验证失败
        return False


def create_token(user_id: str, role: str) -> str:
    """签发 JWT。user_id 在传入前已被 lower() 归一化。"""
    payload = {
        "sub": user_id,
        "role": role,
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALGO)


def decode_token(token: str) -> dict[str, Any]:
    """解 JWT。过期/篡改/格式错都抛 jwt.PyJWTError，调用方接住转成 401。"""
    return jwt.decode(token, _SECRET, algorithms=[_ALGO])
