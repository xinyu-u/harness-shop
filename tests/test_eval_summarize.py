"""测 summarize：NA 不进分母，rate = passes / (passes+fails)。"""

from eval.harness import CaseResult, summarize


def test_summarize_excludes_na():
    results = [
        CaseResult(label="a", passes=1, total=1, na=0),   # 1/1
        CaseResult(label="b", passes=0, total=1, na=0),   # 0/1
        CaseResult(label="c", passes=0, total=0, na=1),   # 全 NA，不计
    ]
    rate, passes, total, na = summarize(results)
    assert passes == 1 and total == 2 and na == 1
    assert abs(rate - 0.5) < 1e-9
    print("[PASS] test_summarize_excludes_na")


def test_summarize_all_na_is_rate_one():
    rate, passes, total, na = summarize([CaseResult("x", 0, 0, 2)])
    assert total == 0 and na == 2 and rate == 1.0   # 无可计分项 → 视为不拖后腿
    print("[PASS] test_summarize_all_na_is_rate_one")


if __name__ == "__main__":
    test_summarize_excludes_na()
    test_summarize_all_na_is_rate_one()
