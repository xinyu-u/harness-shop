"""数据层：Store 接口 + MemoryStore（字典实现）。

工具只调 Store 接口，不直接碰数据。
现在用字典实现（第一期）。第二期写 SqliteStore 实现同一接口，工具一行不改。
"""

import sqlite3
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

    @abstractmethod
    def create_order(
        self, product_id: str, size: str, qty: int, user_id: str,
        request_id: str | None = None,
    ) -> dict: ...

    @abstractmethod
    def get_order(self, order_id: int) -> dict | None: ...

    @abstractmethod
    def cancel_order(self, order_id: int) -> bool: ...


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
        return self._inventory.get((product_id, size), 0)

    def recommend_size(self, height, weight, category):
        # 在区间表里找：品类匹配 + 身高体重都落在区间内 → 返回该尺码
        for cat, h_min, h_max, w_min, w_max, size in self._size_chart:
            if cat == category and h_min <= height < h_max and w_min <= weight <= w_max:
                return size
        return None   # 没有匹配的区间（身高体重超出表范围）

    def create_order(self, product_id, size, qty, user_id, request_id=None):
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
        order["status"] = "cancelled"
        self._inventory[(order["product_id"], order["size"])] += order["qty"]  # 退回库存
        return True


class SqliteStore(Store):
    """SQLite 实现：第二期用它。

    步骤1+2：先建 inventory 表 + 实现 check_stock，跑通"建库→查询"。
    其余方法待扩展。
    """

    def __init__(self, db_path: str = "shop.db"):
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
        # 2) 库存表
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                product_id TEXT,
                size TEXT,
                qty INTEGER
            )
        """)
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
                request_id TEXT
            )
        """)
        # 老数据库可能已经存在 orders 表但没有 request_id 列：补加（已存在则忽略）
        # SQLite 的 ALTER TABLE 只支持 ADD COLUMN，不能加 UNIQUE 约束 → 改用唯一索引
        try:
            self._conn.execute("ALTER TABLE orders ADD COLUMN request_id TEXT")
        except sqlite3.OperationalError:
            pass   # duplicate column name → 列已存在
        # 唯一索引：同一个 request_id 只能对应一条订单
        # SQLite 的 UNIQUE 视 NULL 互不相等，所以历史 NULL 行不会冲突
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_request_id ON orders(request_id)"
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
            self._conn.executemany(
                "INSERT INTO inventory VALUES (?, ?, ?)",
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
        row = self._conn.execute(
            "SELECT qty FROM inventory WHERE product_id = ? AND size = ?",
            (product_id, size),
        ).fetchone()
        # fetchone 返回元组 (qty,) 或 None
        return row[0] if row else 0

    def get_product(self, product_id):
        row = self._conn.execute(
            "SELECT id, name, price, category FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if row is None:
            return None
        # 元组 -> dict，字段名对齐 MemoryStore 的返回格式
        return {"id": row[0], "name": row[1], "price": row[2], "category": row[3]}

    def search_products(self, keyword):
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
        order = self.get_order(order_id)
        if order is None:
            return False
        if order["status"] == "cancelled":
            return False   # 已经取消过了，避免重复退库存

        # 改订单状态
        self._conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ?",
            (order_id,),
        )
        # 退回库存
        self._conn.execute(
            "UPDATE inventory SET qty = qty + ? WHERE product_id = ? AND size = ?",
            (order["qty"], order["product_id"], order["size"]),
        )
        self._conn.commit()
        return True
