"""聊天档案（步骤1 · 数据层）验证。

两个生产实现都测：SqliteStore（生产用）+ MemoryStore（对称）。
SqliteStore 用 :memory: 库，每次全新、互不污染。

核心验证点（mentor 指定）：
  1. 存 5 条 → get_messages 按对话顺序（正序）返回
  2. get_messages(before=第3条时间) 只返回更早的 2 条（游标分页）

附加：
  3. limit 只取最近 N 条（仍正序）
  4. user_id 归一化小写（写入大写、读取小写都命中）
  5. 两实现行为一致

跑法（项目根目录）：python -m tests.test_chat_messages
"""

from business.store import MemoryStore, SqliteStore


def _fresh_stores():
    """返回两个全新 store，逐个跑同一套断言保证对称。"""
    return [("SqliteStore", SqliteStore(":memory:")), ("MemoryStore", MemoryStore())]


# ───────── 验证1：存 5 条 → 正序返回 ─────────

def test_get_messages_returns_chronological_order():
    for label, store in _fresh_stores():
        for i in range(1, 6):
            store.save_message("alice", "user" if i % 2 else "assistant", f"m{i}")

        msgs = store.get_messages("alice")

        assert len(msgs) == 5, f"{label}: 应返回 5 条，得到 {len(msgs)}"
        assert [m["content"] for m in msgs] == ["m1", "m2", "m3", "m4", "m5"], \
            f"{label}: 应按对话顺序（正序）返回"
        assert msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant", \
            f"{label}: role 应原样保留"
    print("✅ 验证1 存5条：get_messages 正序返回")


# ───────── 验证2：before 游标 → 只取更早的 ─────────

def test_get_messages_before_cursor_returns_only_earlier():
    for label, store in _fresh_stores():
        for i in range(1, 6):
            store.save_message("alice", "user", f"m{i}")

        all_msgs = store.get_messages("alice")
        third_time = all_msgs[2]["created_at"]   # 第3条的时间

        earlier = store.get_messages("alice", before=third_time)

        assert [m["content"] for m in earlier] == ["m1", "m2"], \
            f"{label}: before=第3条时间应只返回更早的 m1、m2，得到 {[m['content'] for m in earlier]}"
    print("✅ 验证2 before 游标：只返回更早的 2 条")


# ───────── 验证3：limit 只取最近 N 条（仍正序）─────────

def test_get_messages_limit_takes_most_recent():
    for label, store in _fresh_stores():
        for i in range(1, 6):
            store.save_message("alice", "user", f"m{i}")

        msgs = store.get_messages("alice", limit=2)

        assert [m["content"] for m in msgs] == ["m4", "m5"], \
            f"{label}: limit=2 应取最近 2 条且正序，得到 {[m['content'] for m in msgs]}"
    print("✅ 验证3 limit：取最近 N 条、仍正序")


# ───────── 验证4：user_id 归一化小写 ─────────

def test_user_id_normalized_lowercase():
    for label, store in _fresh_stores():
        store.save_message("Alice", "user", "hi")     # 大写写入
        store.save_message("alice", "assistant", "yo")  # 小写写入

        assert len(store.get_messages("ALICE")) == 2, f"{label}: 大写读取应命中同一用户"
        assert len(store.get_messages("alice")) == 2, f"{label}: 小写读取应命中同一用户"
        assert store.get_messages("bob") == [], f"{label}: 别的用户读不到 alice 的消息"
    print("✅ 验证4 归一化：大小写写入/读取命中同一账号")


def main():
    test_get_messages_returns_chronological_order()
    test_get_messages_before_cursor_returns_only_earlier()
    test_get_messages_limit_takes_most_recent()
    test_user_id_normalized_lowercase()
    print("\n🎉 聊天档案 步骤1 数据层 全部验证通过")


if __name__ == "__main__":
    main()
