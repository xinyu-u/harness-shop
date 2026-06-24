"""数据层：Store 接口 + MemoryStore（字典实现）。

工具只调 Store 接口，不直接碰数据。
现在用字典实现（第一期）。第二期写 SqliteStore 实现同一接口，工具一行不改。
"""

import sqlite3
import threading
import time
from abc import ABC, abstractmethod


class Store(ABC):
    """数据访问接口。"""

    @abstractmethod
    def get_product(self, product_id: str) -> dict | None: ...

    @abstractmethod
    def search_products(self, keyword: str) -> list[dict]: ...

    @abstractmethod
    def check_stock(self, product_id: str, size: str) -> int: ...

    @abstractmethod
    def recommend_size(self, height: int, weight: int, category: str) -> str | None: ...

    # ====== 下单的两条路径（分层，二者并存）======
    #
    #   路径A · 直接下单（旧）：create_order
    #       一步到位：原子扣 qty + 建 status='created' 订单。一个动作即生效、不可逆。
    #       现状：place_order 工具已不再调它（改走路径B），仅 CLI / 历史代码 / 对照保留。
    #
    #   路径B · 草稿确认（新，状态机）：create_draft_order → confirm_draft_order
    #       把"下单"这一个危险动作拆成可校验的多步状态转移：
    #         pending(预占 locked+qty) ──confirm──▶ confirmed(真扣 qty、释放 locked)
    #                                  └─expire──▶ cancelled(释放 locked)
    #       预占（locked）让"建草稿即锁货"，真正扣库存只发生在 confirm。
    #       place_order 只到 create_draft_order（发起），confirm 由独立后端接口触发（见 server.py）。
    #
    #   为什么留着路径A：它是"一步动作"的对照基准，删了会让两种模型的差异看不见；
    #   且不碍事（没人在 web 流里调它）。新功能一律走路径B。
    @abstractmethod
    def create_order(
        self, product_id: str, size: str, qty: int, user_id: str,
        request_id: str | None = None,
    ) -> dict: ...

    @abstractmethod
    def get_order(self, order_id: int) -> dict | None: ...

    @abstractmethod
    def cancel_order(self, order_id: int) -> bool: ...

    # ---- 路径B：草稿订单 / 预占库存 ----
    @abstractmethod
    def create_draft_order(
        self, product_id: str, size: str, qty: int, user_id: str,
        ttl_seconds: int = 900,
    ) -> dict: ...

    @abstractmethod
    def confirm_draft_order(
        self, draft_id: int, user_id: str,
    ) -> tuple[dict | None, str | None]: ...

    @abstractmethod
    def release_expired_orders(self) -> int: ...

    # ---- 用户/账号 ----
    @abstractmethod
    def create_user(self, user_id: str, password_hash: str, role: str = "user") -> None: ...

    @abstractmethod
    def get_user(self, user_id: str) -> dict | None: ...

    # ---- 商家工具 ----
    @abstractmethod
    def set_price(self, product_id: str, price: int) -> bool: ...

    @abstractmethod
    def add_product(self, product_id: str, name: str, price: int, category: str) -> bool: ...

    @abstractmethod
    def restock(self, product_id: str, size: str, add_qty: int) -> int | None: ...

    # ---- 聊天档案（上拉加载式历史）----
    @abstractmethod
    def save_message(self, user_id: str, role: str, content: str) -> None: ...

    @abstractmethod
    def get_messages(
        self, user_id: str, limit: int = 20, before: float | None = None,
    ) -> list[dict]: ...

    @abstractmethod
    def close(self) -> None: ...


class MemoryStore(Store):
    """字典实现：第一期用它。"""

    def __init__(self):
        self._products = {
            "airmax": {"id": "airmax", "name": "Air Max", "price": 899, "category": "鞋"},
            "tshirt": {"id": "tshirt", "name": "纯棉T恤", "price": 99, "category": "上衣"},
        }
        self._inventory = {
            ("airmax", "42"): 5,
            ("airmax", "43"): 0,    # 故意 0：演示"无货→推荐替代"
            ("tshirt", "L"): 10,
        }
        # 预占量：(product_id, size) -> locked，默认 0。available = qty - locked
        self._locked: dict[tuple[str, str], int] = {}
        # 尺码区间表（像淘宝那样：身高体重落在区间内 → 对应尺码）
        # 每条：(品类, 身高下限, 身高上限, 体重下限, 体重上限, 推荐尺码)
        self._size_chart = [
            ("鞋", 150, 170, 40, 65, "41"),
            ("鞋", 170, 178, 55, 75, "42"),
            ("鞋", 178, 195, 65, 95, "43"),
            ("上衣", 150, 168, 40, 60, "M"),
            ("上衣", 168, 195, 60, 100, "L"),
        ]
        self._orders: list[dict] = []
        self._users: dict[str, dict] = {}   # user_id -> {password_hash, role}
        # 聊天档案：按插入顺序（即时间正序）追加，list 顺序天然是 id 兜底排序
        self._messages: list[dict] = []

    def get_product(self, product_id):
        return self._products.get(product_id)

    def search_products(self, keyword):
        # 按关键词匹配商品名或id（极简：包含即命中）
        kw = keyword.lower()
        return [
            p for p in self._products.values()
            if kw in p["name"].lower() or kw in p["id"].lower()
        ]

    def check_stock(self, product_id, size):
        # available = 总库存 - 已预占
        key = (product_id, size)
        return self._inventory.get(key, 0) - self._locked.get(key, 0)

    def recommend_size(self, height, weight, category):
        # 在区间表里找：品类匹配 + 身高体重都落在区间内 → 返回该尺码
        for cat, h_min, h_max, w_min, w_max, size in self._size_chart:
            if cat == category and h_min <= height < h_max and w_min <= weight <= w_max:
                return size
        return None   # 没有匹配的区间（身高体重超出表范围）

    def create_order(self, product_id, size, qty, user_id, request_id=None):
        # 路径A（直接下单，旧）：一步扣 qty + 建 created 订单。新流程走 create_draft_order。
        # 幂等：相同 request_id 已下过单 → 直接返回原订单，不重复扣库存/不重复建单
        if request_id is not None:
            for o in self._orders:
                if o.get("request_id") == request_id:
                    return o
        key = (product_id, size)
        if self._inventory.get(key, 0) < qty:
            raise ValueError(f"库存不足: {product_id} {size}")
        self._inventory[key] -= qty
        order = {
            "id": len(self._orders) + 1,
            "product_id": product_id, "size": size, "qty": qty,
            "user_id": user_id, "status": "created",
            "request_id": request_id,
        }
        self._orders.append(order)
        return order

    def get_order(self, order_id):
        for o in self._orders:
            if o["id"] == order_id:
                return o
        return None

    def cancel_order(self, order_id):
        order = self.get_order(order_id)
        if order is None:
            return False
        if order["status"] == "cancelled":
            return False   # 已经取消过了
        key = (order["product_id"], order["size"])
        # pending 草稿库存在 locked（释放 locked）；created/confirmed 已真扣（退回 qty）
        if order["status"] == "pending":
            self._locked[key] = self._locked.get(key, 0) - order["qty"]
        else:
            self._inventory[key] += order["qty"]
        order["status"] = "cancelled"
        return True

    # ---- 草稿订单 / 预占库存（步骤1）----
    def create_draft_order(self, product_id, size, qty, user_id, ttl_seconds=900):
        key = (product_id, size)
        available = self._inventory.get(key, 0) - self._locked.get(key, 0)
        if available < qty:
            raise ValueError(f"库存不足: {product_id} {size}")
        self._locked[key] = self._locked.get(key, 0) + qty   # 预占
        order = {
            "id": len(self._orders) + 1,
            "product_id": product_id, "size": size, "qty": qty,
            "user_id": user_id, "status": "pending",
            "expires_at": time.time() + ttl_seconds,
            "request_id": None,
        }
        self._orders.append(order)
        return order

    def confirm_draft_order(self, draft_id, user_id):
        self.release_expired_orders()   # 先清过期：过期草稿在此变 cancelled
        order = self.get_order(draft_id)
        if order is None:
            return None, "订单不存在"
        if order["user_id"] != user_id:
            return None, "无权确认此订单"
        if order["status"] == "confirmed":
            return order, None          # 幂等：已确认过，不重复扣
        if order["status"] != "pending":
            return None, "订单已取消或已过期"
        # 预占转真扣：qty 真减、locked 归还（available 不变，因为预占时已减过）
        key = (order["product_id"], order["size"])
        self._inventory[key] -= order["qty"]
        self._locked[key] = self._locked.get(key, 0) - order["qty"]
        order["status"] = "confirmed"
        return order, None

    def release_expired_orders(self):
        now = time.time()
        released = 0
        for order in self._orders:
            if order["status"] == "pending" and order.get("expires_at", 0) < now:
                key = (order["product_id"], order["size"])
                self._locked[key] = self._locked.get(key, 0) - order["qty"]  # 释放预占
                order["status"] = "cancelled"
                released += 1
        return released

    # ---- 账号 ----
    def create_user(self, user_id, password_hash, role="user"):
        uid = user_id.lower()
        if uid in self._users:
            raise ValueError(f"用户已存在: {uid}")
        self._users[uid] = {"password_hash": password_hash, "role": role}

    def get_user(self, user_id):
        u = self._users.get(user_id.lower())
        if u is None:
            return None
        return {"user_id": user_id.lower(), **u}

    # ---- 商家工具 ----
    def set_price(self, product_id, price):
        if product_id not in self._products:
            return False
        self._products[product_id]["price"] = price
        return True

    def add_product(self, product_id, name, price, category):
        if product_id in self._products:
            return False
        self._products[product_id] = {
            "id": product_id, "name": name, "price": price, "category": category,
        }
        return True

    def restock(self, product_id, size, add_qty):
        # 增量补货：qty += add_qty（不覆写绝对值）。商品不存在 → None。
        # 该尺码还没有库存行 → 从 0 起新建。只动 qty，不碰 locked。
        if product_id not in self._products:
            return None
        key = (product_id, size)
        self._inventory[key] = self._inventory.get(key, 0) + add_qty
        return self._inventory[key]

    # ---- 聊天档案 ----
    def save_message(self, user_id, role, content):
        self._messages.append({
            "user_id": user_id.lower(), "role": role,
            "content": content, "created_at": time.time(),
        })

    def get_messages(self, user_id, limit=20, before=None):
        uid = user_id.lower()
        rows = [m for m in self._messages if m["user_id"] == uid]
        if before is not None:
            rows = [m for m in rows if m["created_at"] < before]
        # _messages 按插入顺序追加 = 时间正序；取最近 limit 条即末尾切片，已是正序
        recent = rows[-limit:]
        return [
            {"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
            for m in recent
        ]

    def close(self):
        pass   # 字典实现无连接，空实现满足接口


class SqliteStore(Store):
    """SQLite 实现：第二期用它。

    步骤1+2：先建 inventory 表 + 实现 check_stock，跑通"建库→查询"。
    其余方法待扩展。
    """

    def __init__(self, db_path: str = "shop.db"):
        self._lock = threading.RLock()
        # check_same_thread=False：FastAPI 多线程访问需要（否则 sqlite3 报跨线程错）
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        # 1) 商品表
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT,
                price INTEGER,
                category TEXT
            )
        """)
        # 2) 库存表（qty=总库存，locked=已预占；available=qty-locked）
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                product_id TEXT,
                size TEXT,
                qty INTEGER,
                locked INTEGER NOT NULL DEFAULT 0
            )
        """)
        # 老库可能没有 locked 列：补加（已存在则忽略）
        try:
            self._conn.execute("ALTER TABLE inventory ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass   # duplicate column name → 列已存在
        # 3) 尺码区间表
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS size_chart (
                category TEXT,
                h_min INTEGER,
                h_max INTEGER,
                w_min INTEGER,
                w_max INTEGER,
                size TEXT
            )
        """)
        # 4) 订单表（id 自增；初始为空，不塞数据）
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT,
                size TEXT,
                qty INTEGER,
                user_id TEXT,
                status TEXT,
                request_id TEXT,
                expires_at REAL
            )
        """)
        # 老数据库可能已经存在 orders 表但没有 request_id 列：补加（已存在则忽略）
        # SQLite 的 ALTER TABLE 只支持 ADD COLUMN，不能加 UNIQUE 约束 → 改用唯一索引
        try:
            self._conn.execute("ALTER TABLE orders ADD COLUMN request_id TEXT")
        except sqlite3.OperationalError:
            pass   # duplicate column name → 列已存在
        # 草稿订单的过期时间（Unix 时间戳，秒）：老库补加
        try:
            self._conn.execute("ALTER TABLE orders ADD COLUMN expires_at REAL")
        except sqlite3.OperationalError:
            pass   # duplicate column name → 列已存在
        # 唯一索引：同一个 request_id 只能对应一条订单
        # SQLite 的 UNIQUE 视 NULL 互不相等，所以历史 NULL 行不会冲突
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_request_id ON orders(request_id)"
        )
        # 5) 用户表（账号 + 角色）
        # user_id 在写入前已被 lower()，所以这里不再区分大小写
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user'
            )
        """)
        # 6) 聊天档案表（每条消息一行；id 自增兼作同时间戳的兜底排序键）
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,           -- 'user' / 'assistant'
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        # 按 (user_id, created_at) 取某人最近 N 条 / 游标分页，走这条复合索引
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_user_time "
            "ON chat_messages(user_id, created_at)"
        )

        # 每张表只在空时塞初始数据，避免每次启动重复 INSERT
        if self._conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
            self._conn.executemany(
                "INSERT INTO products VALUES (?, ?, ?, ?)",
                [
                    ("airmax", "Air Max", 899, "鞋"),
                    ("tshirt", "纯棉T恤", 99, "上衣"),
                ],
            )

        if self._conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 0:
            # 显式列名：locked 走默认 0，不受新增列影响
            self._conn.executemany(
                "INSERT INTO inventory (product_id, size, qty) VALUES (?, ?, ?)",
                [("airmax", "42", 5), ("airmax", "43", 0), ("tshirt", "L", 10)],
            )

        if self._conn.execute("SELECT COUNT(*) FROM size_chart").fetchone()[0] == 0:
            self._conn.executemany(
                "INSERT INTO size_chart VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("鞋",   150, 170, 40, 65, "41"),
                    ("鞋",   170, 178, 55, 75, "42"),
                    ("鞋",   178, 195, 65, 95, "43"),
                    ("上衣", 150, 168, 40, 60, "M"),
                    ("上衣", 168, 195, 60, 100, "L"),
                ],
            )

        self._conn.commit()

    def check_stock(self, product_id, size):
        with self._lock:
            # 查可用量前先惰性释放过期预占，保证 available 准确（步骤4）
            self.release_expired_orders()
            row = self._conn.execute(
                "SELECT qty, locked FROM inventory WHERE product_id = ? AND size = ?",
                (product_id, size),
            ).fetchone()
            # available = qty - locked；fetchone 返回 (qty, locked) 或 None
            return (row[0] - row[1]) if row else 0

    def get_product(self, product_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, price, category FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()
            if row is None:
                return None
            # 元组 -> dict，字段名对齐 MemoryStore 的返回格式
            return {"id": row[0], "name": row[1], "price": row[2], "category": row[3]}

    def search_products(self, keyword):
        with self._lock:
            # LIKE 模糊匹配：%keyword% 表示"包含即命中"
            # SQLite 的 LIKE 对 ASCII 默认不分大小写；中文字符无大小写之分，直接传原串即可
            pattern = f"%{keyword}%"
            rows = self._conn.execute(
                "SELECT id, name, price, category FROM products WHERE name LIKE ? OR id LIKE ?",
                (pattern, pattern),
            ).fetchall()
            return [
                {"id": r[0], "name": r[1], "price": r[2], "category": r[3]}
                for r in rows
            ]

    def recommend_size(self, height, weight, category):
        with self._lock:
            # 范围查询：身高在 [h_min, h_max)，体重在 [w_min, w_max]
            # 区间边界和 MemoryStore 完全一致：h 是左闭右开，w 是双闭
            row = self._conn.execute(
                """
                SELECT size FROM size_chart
                WHERE category = ?
                  AND h_min <= ? AND ? < h_max
                  AND w_min <= ? AND ? <= w_max
                """,
                (category, height, height, weight, weight),
            ).fetchone()
            return row[0] if row else None

    def create_order(self, product_id, size, qty, user_id, request_id=None):
        with self._lock:
            # 路径A（直接下单，旧）：一步扣 qty + 建 created 订单。新流程走 create_draft_order。
            # ⓪ 幂等预检：相同 request_id 已下过单 → 直接返回原订单
            # 这能拦截"客户端重发"的绝大多数场景（顺序到达的重复请求）
            if request_id is not None:
                row = self._conn.execute(
                    "SELECT id FROM orders WHERE request_id = ?", (request_id,)
                ).fetchone()
                if row is not None:
                    return self.get_order(row[0])

            try:
                # ① 原子检查 + 扣库存（同 B4①，防超卖）
                cur = self._conn.execute(
                    "UPDATE inventory SET qty = qty - ? "
                    "WHERE product_id = ? AND size = ? AND qty >= ?",
                    (qty, product_id, size, qty),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"库存不足: {product_id} {size}")

                # ② 插订单（带 request_id；唯一索引兜底防并发重复）
                cur2 = self._conn.execute(
                    "INSERT INTO orders (product_id, size, qty, user_id, status, request_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (product_id, size, qty, user_id, "created", request_id),
                )
                new_id = cur2.lastrowid

                self._conn.commit()

                return {
                    "id": new_id,
                    "product_id": product_id, "size": size, "qty": qty,
                    "user_id": user_id, "status": "created",
                }
            except sqlite3.IntegrityError:
                # 并发兜底：两个相同 request_id 几乎同时到达
                #   - 都通过了 ⓪ 预检（彼此还没 commit，互相看不到）
                #   - 都跑 UPDATE 扣库存（数据库锁串行化）
                #   - 都跑 INSERT，但唯一索引只允许一个成功
                #   - 失败那个落到这里：rollback 撤销自己刚才的库存扣减
                #     然后回头查"赢家"那条订单，返回给调用方
                self._conn.rollback()
                row = self._conn.execute(
                    "SELECT id FROM orders WHERE request_id = ?", (request_id,)
                ).fetchone()
                if row is not None:
                    return self.get_order(row[0])
                raise   # 不该走到这——除非 IntegrityError 来自别的约束
            except Exception:
                self._conn.rollback()
                raise

    def get_order(self, order_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT id, product_id, size, qty, user_id, status FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": row[0], "product_id": row[1], "size": row[2],
                "qty": row[3], "user_id": row[4], "status": row[5],
            }

    def cancel_order(self, order_id):
        with self._lock:
            order = self.get_order(order_id)
            if order is None:
                return False
            if order["status"] == "cancelled":
                return False   # 已经取消过了，避免重复退库存

            # 库存在两个不同的"位置"，取消时要退回正确的位置：
            #   pending（草稿）         → 库存还在 locked 预占着，从没真扣 → 释放 locked
            #   created / confirmed    → 库存已从 qty 真扣 → 退回 qty
            if order["status"] == "pending":
                self._conn.execute(
                    "UPDATE inventory SET locked = locked - ? WHERE product_id = ? AND size = ?",
                    (order["qty"], order["product_id"], order["size"]),
                )
            else:
                self._conn.execute(
                    "UPDATE inventory SET qty = qty + ? WHERE product_id = ? AND size = ?",
                    (order["qty"], order["product_id"], order["size"]),
                )
            self._conn.execute(
                "UPDATE orders SET status = 'cancelled' WHERE id = ?",
                (order_id,),
            )
            self._conn.commit()
            return True

    # ---- 草稿订单 / 预占库存（步骤1）----
    def create_draft_order(self, product_id, size, qty, user_id, ttl_seconds=900):
        with self._lock:
            try:
                # ① 原子预占：available(qty-locked) 够才 locked+qty，防超卖
                cur = self._conn.execute(
                    "UPDATE inventory SET locked = locked + ? "
                    "WHERE product_id = ? AND size = ? AND qty - locked >= ?",
                    (qty, product_id, size, qty),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"库存不足: {product_id} {size}")

                # ② 建 pending 草稿，记过期时间
                expires_at = time.time() + ttl_seconds
                cur2 = self._conn.execute(
                    "INSERT INTO orders (product_id, size, qty, user_id, status, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (product_id, size, qty, user_id, "pending", expires_at),
                )
                new_id = cur2.lastrowid
                self._conn.commit()
                return {
                    "id": new_id,
                    "product_id": product_id, "size": size, "qty": qty,
                    "user_id": user_id, "status": "pending",
                    "expires_at": expires_at,
                }
            except ValueError:
                self._conn.rollback()
                raise
            except Exception:
                self._conn.rollback()
                raise

    def confirm_draft_order(self, draft_id, user_id):
        with self._lock:
            # 先惰性释放过期草稿：若本草稿已过期，会在此被置为 cancelled（步骤4）
            self.release_expired_orders()
            order = self.get_order(draft_id)
            if order is None:
                return None, "订单不存在"
            if order["user_id"] != user_id:
                return None, "无权确认此订单"
            if order["status"] == "confirmed":
                return order, None          # 幂等：已确认过，不重复扣
            if order["status"] != "pending":
                return None, "订单已取消或已过期"
            try:
                # 预占转真扣：qty 真减 + locked 归还（available 不变，预占时已扣过）
                self._conn.execute(
                    "UPDATE inventory SET qty = qty - ?, locked = locked - ? "
                    "WHERE product_id = ? AND size = ?",
                    (order["qty"], order["qty"], order["product_id"], order["size"]),
                )
                self._conn.execute(
                    "UPDATE orders SET status = 'confirmed' WHERE id = ?", (draft_id,)
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            order["status"] = "confirmed"
            return order, None

    def release_expired_orders(self):
        with self._lock:
            now = time.time()
            # 找出所有过期的 pending 草稿
            rows = self._conn.execute(
                "SELECT id, product_id, size, qty FROM orders "
                "WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            ).fetchall()
            for _id, product_id, size, qty in rows:
                # 释放预占 + 取消草稿
                self._conn.execute(
                    "UPDATE inventory SET locked = locked - ? WHERE product_id = ? AND size = ?",
                    (qty, product_id, size),
                )
                self._conn.execute(
                    "UPDATE orders SET status = 'cancelled' WHERE id = ?", (_id,)
                )
            if rows:
                self._conn.commit()
            return len(rows)

    # ---- 账号 ----
    def create_user(self, user_id, password_hash, role="user"):
        with self._lock:
            # user_id 统一小写：注册和登录走同一规则
            uid = user_id.lower()
            try:
                self._conn.execute(
                    "INSERT INTO users (user_id, password_hash, role) VALUES (?, ?, ?)",
                    (uid, password_hash, role),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"用户已存在: {uid}")

    def get_user(self, user_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, password_hash, role FROM users WHERE user_id = ?",
                (user_id.lower(),),
            ).fetchone()
            if row is None:
                return None
            return {"user_id": row[0], "password_hash": row[1], "role": row[2]}

    # ---- 商家工具 ----
    def set_price(self, product_id, price):
        with self._lock:
            cur = self._conn.execute(
                "UPDATE products SET price = ? WHERE id = ?",
                (price, product_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def add_product(self, product_id, name, price, category):
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO products (id, name, price, category) VALUES (?, ?, ?, ?)",
                    (product_id, name, price, category),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False   # 主键冲突 → 商品已存在

    def restock(self, product_id, size, add_qty):
        with self._lock:
            # 增量补货：qty += add_qty（原子自增，不覆写绝对值）。商品不存在 → None。
            # 该 (product_id, size) 还没有库存行 → 新建（从 0 起补）。只动 qty，不碰 locked。
            if self._conn.execute(
                "SELECT 1 FROM products WHERE id = ?", (product_id,)
            ).fetchone() is None:
                return None
            try:
                cur = self._conn.execute(
                    "UPDATE inventory SET qty = qty + ? WHERE product_id = ? AND size = ?",
                    (add_qty, product_id, size),
                )
                if cur.rowcount == 0:
                    # 没有该尺码的库存行：新建（locked 走默认 0）
                    self._conn.execute(
                        "INSERT INTO inventory (product_id, size, qty) VALUES (?, ?, ?)",
                        (product_id, size, add_qty),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            row = self._conn.execute(
                "SELECT qty FROM inventory WHERE product_id = ? AND size = ?",
                (product_id, size),
            ).fetchone()
            return row[0]

    # ---- 聊天档案 ----
    def save_message(self, user_id, role, content):
        with self._lock:
            # user_id 归一化小写，和账号一致（注册/登录同规则）
            self._conn.execute(
                "INSERT INTO chat_messages (user_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id.lower(), role, content, time.time()),
            )
            self._conn.commit()

    def get_messages(self, user_id, limit=20, before=None):
        """取最近 limit 条（倒序取），反转成正序返回（前端按对话顺序显示）。

        before=游标（上一页最早一条的 created_at），取更早的，用于上拉加载。
        ORDER BY 加 id DESC 兜底：同一时间戳的多条也有稳定顺序。
        """
        with self._lock:
            uid = user_id.lower()
            if before is None:
                rows = self._conn.execute(
                    "SELECT role, content, created_at FROM chat_messages "
                    "WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (uid, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT role, content, created_at FROM chat_messages "
                    "WHERE user_id = ? AND created_at < ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (uid, before, limit),
                ).fetchall()
            # 倒序取最近，reversed 成正序
            return [
                {"role": r[0], "content": r[1], "created_at": r[2]}
                for r in reversed(rows)
            ]

    def close(self):
        self._conn.close()
