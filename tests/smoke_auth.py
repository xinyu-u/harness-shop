"""快速冒烟：Store 账号 + auth + 商家工具底层调用。
跑：conda run -n llm python tests/smoke_auth.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from business.store import SqliteStore
from core.auth import hash_password, verify_password, create_token, decode_token

DB = "test_auth.db"
if os.path.exists(DB):
    os.remove(DB)

s = SqliteStore(DB)

# 1) 注册 + 登录
s.create_user("Alice", hash_password("pw1234"), role="user")
u = s.get_user("alice")
assert u["user_id"] == "alice" and u["role"] == "user"
assert verify_password("pw1234", u["password_hash"])
assert not verify_password("wrong", u["password_hash"])
print("[1] register+login OK")

# 2) 大小写归一化
assert s.get_user("ALICE")["user_id"] == "alice"
print("[2] case-insensitive lookup OK")

# 3) JWT 签发 + 解
tok = create_token("alice", "user")
p = decode_token(tok)
assert p["sub"] == "alice" and p["role"] == "user" and "exp" in p
print("[3] JWT roundtrip OK")

# 4) 重复注册
try:
    s.create_user("alice", hash_password("x"))
    raise AssertionError("expected ValueError")
except ValueError:
    print("[4] duplicate user blocked OK")

# 5) 商家工具
assert s.set_price("airmax", 799)
assert s.get_product("airmax")["price"] == 799
assert s.add_product("jeans", "牛仔裤", 299, "下衣")
assert s.get_product("jeans")["price"] == 299
assert not s.add_product("jeans", "x", 1, "x")   # 重复 → False
print("[5] merchant tools OK")

# 6) 直接种 merchant
s.create_user("boss", hash_password("boss123"), role="merchant")
assert s.get_user("boss")["role"] == "merchant"
print("[6] merchant seed OK")

# 7) update_user：覆盖密码 + 角色（server 启动种商家走这条，不再伸手进 _conn）
assert s.update_user("alice", hash_password("newpw"), role="merchant")
u = s.get_user("alice")
assert u["role"] == "merchant" and verify_password("newpw", u["password_hash"])
assert s.update_user("ALICE", hash_password("p2"), role="user")   # 大小写归一
assert s.get_user("alice")["role"] == "user"
assert not s.update_user("nobody", hash_password("x"), role="user")   # 不存在 → False，不新建
assert s.get_user("nobody") is None
print("[7] update_user OK")

s._conn.close()
os.remove(DB)
print("\n全部通过。")
