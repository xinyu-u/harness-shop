"""run_case(store=...) 共享语义：传入的 store 被复用，且 cleanup 不关闭它（由调用方统一关）。"""
import asyncio

from core.messages import ConversationMessage, TextBlock
from eval.harness import run_case, _fresh_sqlite


# 无工具调用的最简脚本：单轮文本回复即结束（避免烧 API）
FAKE = [ConversationMessage(role="assistant", content=[TextBlock(text="你好")])]


def test_injected_store_is_reused_and_not_closed_on_cleanup():
    store, path = _fresh_sqlite()
    try:
        trace = asyncio.run(run_case(
            "在吗", store=store, fake_script=FAKE, force_fake=True,
        ))
        # 复用了同一个 store 实例
        assert trace.store is store
        # cleanup 不应关闭共享 store：cleanup 后仍可查询
        trace.cleanup()
        # 5 是 airmax/42 的 seed 库存；真正要验证的是"连接没被关"（仍能查询），
        # 具体数值只是顺带确认 seed 库可读。
        assert store.check_stock("airmax", "42") == 5
    finally:
        store.close()
        import os
        os.unlink(path)


def test_default_store_still_isolated_and_self_cleaning():
    # 不传 store：行为不变，自建临时库并自行清理
    trace = asyncio.run(run_case("在吗", fake_script=FAKE, force_fake=True))
    assert trace.store is not None
    assert trace._owns_store is True
    trace.cleanup()  # 不抛即可（自己关自己删）


if __name__ == "__main__":
    test_injected_store_is_reused_and_not_closed_on_cleanup()
    test_default_store_still_isolated_and_self_cleaning()
    print("OK")
