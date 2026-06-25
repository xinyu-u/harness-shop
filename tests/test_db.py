"""第二期 SqliteStore 雏形（自带一份精简 Store，不依赖 business/store.py）的数据层验证。

历史背景：这是「字典实现 → SQLite 实现同一接口」学习阶段的最早 sqlite 实验，
不含 locked/草稿/账号等后续字段，仅验证 下单/库存/取消/持久化 的基本闭环。

每个用例用隔离的库（:memory: 或临时文件），不碰真实 shop.db、互不污染。

跑法（项目根目录）：python -m tests.test_db
"""

import os
import sqlite3
import tempfile
from abc import ABC, abstractmethod

class Store(ABC):
    @abstractmethod
    def get_product(self,p): ...
    @abstractmethod
    def search_products(self,k): ...
    @abstractmethod
    def check_stock(self,p,s): ...
    @abstractmethod
    def recommend_size(self,h,w,c): ...
    @abstractmethod
    def create_order(self,p,s,q,u): ...
    @abstractmethod
    def get_order(self,o): ...
    @abstractmethod
    def cancel_order(self,o): ...

class SqliteStore(Store):
    def __init__(self, db_path="shop.db"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
    def _init_db(self):
        self._conn.execute("CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, name TEXT, price INTEGER, category TEXT)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS inventory (product_id TEXT, size TEXT, qty INTEGER)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS size_chart (category TEXT, h_min INTEGER, h_max INTEGER, w_min INTEGER, w_max INTEGER, size TEXT)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT, size TEXT, qty INTEGER, user_id TEXT, status TEXT)")
        if self._conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]==0:
            self._conn.executemany("INSERT INTO products VALUES (?,?,?,?)", [("airmax","Air Max",899,"鞋"),("tshirt","纯棉T恤",99,"上衣")])
        if self._conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]==0:
            self._conn.executemany("INSERT INTO inventory VALUES (?,?,?)", [("airmax","42",5),("airmax","43",0),("tshirt","L",10)])
        if self._conn.execute("SELECT COUNT(*) FROM size_chart").fetchone()[0]==0:
            self._conn.executemany("INSERT INTO size_chart VALUES (?,?,?,?,?,?)", [("鞋",150,170,40,65,"41"),("鞋",170,178,55,75,"42"),("鞋",178,195,65,95,"43"),("上衣",150,168,40,60,"M"),("上衣",168,195,60,100,"L")])
        self._conn.commit()
    def check_stock(self,p,s):
        r=self._conn.execute("SELECT qty FROM inventory WHERE product_id=? AND size=?",(p,s)).fetchone()
        return r[0] if r else 0
    def get_product(self,p):
        r=self._conn.execute("SELECT id,name,price,category FROM products WHERE id=?",(p,)).fetchone()
        return {"id":r[0],"name":r[1],"price":r[2],"category":r[3]} if r else None
    def search_products(self,k):
        pat=f"%{k}%"
        rs=self._conn.execute("SELECT id,name,price,category FROM products WHERE name LIKE ? OR id LIKE ?",(pat,pat)).fetchall()
        return [{"id":r[0],"name":r[1],"price":r[2],"category":r[3]} for r in rs]
    def recommend_size(self,h,w,c):
        r=self._conn.execute("SELECT size FROM size_chart WHERE category=? AND h_min<=? AND ?<h_max AND w_min<=? AND ?<=w_max",(c,h,h,w,w)).fetchone()
        return r[0] if r else None
    def create_order(self,p,s,q,u):
        if self.check_stock(p,s)<q: raise ValueError(f"库存不足: {p} {s}")
        self._conn.execute("UPDATE inventory SET qty=qty-? WHERE product_id=? AND size=?",(q,p,s))
        cur=self._conn.execute("INSERT INTO orders (product_id,size,qty,user_id,status) VALUES (?,?,?,?,?)",(p,s,q,u,"created"))
        nid=cur.lastrowid
        self._conn.commit()
        return {"id":nid,"product_id":p,"size":s,"qty":q,"user_id":u,"status":"created"}
    def get_order(self,o):
        r=self._conn.execute("SELECT id,product_id,size,qty,user_id,status FROM orders WHERE id=?",(o,)).fetchone()
        return {"id":r[0],"product_id":r[1],"size":r[2],"qty":r[3],"user_id":r[4],"status":r[5]} if r else None
    def cancel_order(self,o):
        order=self.get_order(o)
        if order is None: return False
        if order["status"]=="cancelled": return False
        self._conn.execute("UPDATE orders SET status='cancelled' WHERE id=?",(o,))
        self._conn.execute("UPDATE inventory SET qty=qty+? WHERE product_id=? AND size=?",(order["qty"],order["product_id"],order["size"]))
        self._conn.commit()
        return True

# ───────── 下单流程：扣库存 + 建 created 订单 ─────────

def test_order_flow_deducts_and_creates():
    s = SqliteStore(":memory:")
    assert s.check_stock("airmax", "42") == 5      # 下单前 5
    o = s.create_order("airmax", "42", 1, "u1")
    assert o["status"] == "created"
    assert s.check_stock("airmax", "42") == 4      # 扣了 1
    assert s.get_order(o["id"]) is not None


# ───────── 库存不足：下单被拒 ─────────

def test_insufficient_stock_rejected():
    s = SqliteStore(":memory:")
    try:
        s.create_order("airmax", "43", 1, "u1")    # 43码库存 0
        assert False, "库存 0 应抛 ValueError"
    except ValueError:
        pass


# ───────── 取消订单：退回库存 + 置 cancelled + 幂等 ─────────

def test_cancel_restores_stock_and_is_idempotent():
    s = SqliteStore(":memory:")
    o = s.create_order("airmax", "42", 1, "u1")
    assert s.cancel_order(o["id"]) is True
    assert s.check_stock("airmax", "42") == 5      # 退回
    assert s.get_order(o["id"])["status"] == "cancelled"
    assert s.cancel_order(o["id"]) is False        # 已取消，不重复退
    assert s.cancel_order(99) is False             # 不存在


# ───────── 持久化：重连同一文件，订单与扣减都还在 ─────────

def test_persists_across_reconnect():
    db = os.path.join(tempfile.mkdtemp(), "shop.db")
    s = SqliteStore(db)
    o = s.create_order("airmax", "42", 1, "u1")
    s2 = SqliteStore(db)                            # 重连同一文件
    assert s2.get_order(o["id"]) is not None        # 订单持久化
    assert s2.check_stock("airmax", "42") == 4      # 扣减也持久化


if __name__ == "__main__":
    test_order_flow_deducts_and_creates()
    test_insufficient_stock_rejected()
    test_cancel_restores_stock_and_is_idempotent()
    test_persists_across_reconnect()
    print("全部通过。")

