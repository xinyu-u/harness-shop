"""测 SqliteStore.close() 后临时 db 文件可被删除（Windows 上连接没关无法 unlink）。"""

import os
import tempfile

from business.store import SqliteStore, MemoryStore


def test_sqlite_close_allows_unlink():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = SqliteStore(path)
    assert store.get_product("airmax")["price"] == 899   # 自动 seed 生效
    store.close()
    os.unlink(path)              # 连接已关 → 删除成功，不抛 PermissionError
    assert not os.path.exists(path)
    print("✅ test_sqlite_close_allows_unlink 通过")


def test_memory_close_is_noop():
    MemoryStore().close()        # 不抛异常即可
    print("✅ test_memory_close_is_noop 通过")


if __name__ == "__main__":
    test_sqlite_close_allows_unlink()
    test_memory_close_is_noop()
