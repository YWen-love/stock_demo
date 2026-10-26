"""规则引擎测试。

重点在三类：
1. 公式口径（阈值/分级/补货量）
2. 边界（等于阈值归哪一档 —— 反了会让店主在压线时被反复催货）
3. **判定闸**：模型说需要补货、规则判紧急补货 → 最终必须是紧急补货
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from advisor import config
from advisor.rules import (
    GRADE_REPLENISH,
    GRADE_STOCK,
    GRADE_URGENT,
    QUALITY_ESTIMATED,
    QUALITY_HISTORY,
    QUALITY_NONE,
    TREND_DOWN_STRONG,
    TREND_UP_STRONG,
    advise,
    emergency_threshold,
    enforce_grades,
    expiry_days_left,
    expiry_warning,
    grade_of,
    is_stale,
    reconcile,
    replenish_qty,
    rolling_avg,
    safety_threshold,
    trend_of,
    week_key_of,
)

RULE_DOC = Path(__file__).resolve().parents[1] / "stock_rag" / "rag_docs" / "stock_rule.txt"


# ---------------------------------------------------------------------------
# 阈值与分级
# ---------------------------------------------------------------------------

def test_safety_threshold_matches_rule_formula():
    assert safety_threshold(50) == pytest.approx(60.0)
    assert safety_threshold(0) == 0.0


def test_emergency_threshold_matches_rule_formula():
    # 近两天总销量 × 1.2 = 周均 ÷ 7 × 2 × 1.2
    assert emergency_threshold(50) == pytest.approx(50 / 7 * 2 * 1.2, abs=0.01)


def test_replenish_never_negative():
    assert replenish_qty(current_stock=100, safety=60) == 0.0
    assert replenish_qty(current_stock=12, safety=60) == pytest.approx(48.0)


def test_grade_three_tiers():
    safety, emergency = safety_threshold(50), emergency_threshold(50)
    assert grade_of(60, safety, emergency) == GRADE_STOCK
    assert grade_of(20, safety, emergency) == GRADE_REPLENISH
    assert grade_of(5, safety, emergency) == GRADE_URGENT


@pytest.mark.parametrize("stock,expected", [
    (60.0, GRADE_STOCK),      # 正好等于安全线 → 充足
    (17.14, GRADE_REPLENISH),  # 正好等于紧急线 → 需要补货，不是紧急
    (17.13, GRADE_URGENT),
])
def test_grade_boundary_belongs_to_upper_tier(stock, expected):
    """阈值取等号归上一档。反了的话库存正好压线时会被反复催货。"""
    safety, emergency = safety_threshold(50), emergency_threshold(50)
    assert grade_of(stock, safety, emergency) == expected


# ---------------------------------------------------------------------------
# 滚动均值 / 趋势 / 滞销 / 过期
# ---------------------------------------------------------------------------

def test_rolling_avg_uses_only_available_weeks():
    """不足 N 周按已有周算，不按 0 摊薄 —— 冷启动期摊薄会把安全线压到接近 0。"""
    assert rolling_avg([40]) == 40.0
    assert rolling_avg([40, 50]) == 45.0
    assert rolling_avg([10, 20, 30, 40, 50]) == pytest.approx(35.0)  # 只取最近 4 周


def test_trend_of_needs_two_weeks():
    assert trend_of([10])["label"] == "数据不足"


def test_trend_labels():
    assert trend_of([10, 12])["label"] == TREND_UP_STRONG
    assert trend_of([10, 8])["label"] == TREND_DOWN_STRONG
    assert trend_of([10, 9])["label"] == "略有下滑"
    assert trend_of([0, 5])["pct"] is None  # 上周为零算不出百分比，如实说从零起量


def test_stale_catches_near_zero_sellers():
    """周均只有零点几的老货也必须判得出滞销 —— 这是最该清的一批。"""
    assert is_stale([0.2, 0.1, 0.05]) is True
    assert is_stale([26, 28, 27]) is False


def test_stale_needs_three_weeks():
    assert is_stale([0.1, 0.1]) is False


def test_expiry_helpers():
    assert expiry_days_left(30, "2026-01-01", today=date(2026, 1, 11)) == 20
    assert expiry_days_left(None, "2026-01-01") is None      # 没填保质期就不猜
    assert expiry_days_left(30, None) is None
    assert expiry_warning(7, 21) is True      # 21 × 1/3 = 7，正好卡线就提醒
    assert expiry_warning(10, 21) is False
    assert expiry_warning(-5, 21) is True     # 已过期


# ---------------------------------------------------------------------------
# advise()
# ---------------------------------------------------------------------------

def _coke(**overrides):
    payload = dict(
        product_id=1, name="可口可乐", current_stock=12,
        sales=[44, 50, 57], sales_source="derived", unit="瓶",
    )
    payload.update(overrides)
    return advise(**payload)


def test_advise_derives_full_conclusion():
    result = _coke()
    assert result.avg_weekly_sales == pytest.approx(50.33, abs=0.01)
    assert result.safety_threshold == pytest.approx(60.4, abs=0.01)
    assert result.grade == GRADE_URGENT
    assert result.trend_label == "略有上升"
    assert result.data_quality == QUALITY_HISTORY


def test_advise_cold_start_uses_estimated_daily_sales():
    result = _coke(sales=[], sales_source=None, estimated_daily_sales=5)
    assert result.avg_weekly_sales == 35.0
    assert result.data_quality == QUALITY_ESTIMATED
    assert any("估算" in alert for alert in result.alerts)


def test_advise_refuses_to_decide_without_any_data():
    """既没历史也没估算 → 判不出，不给编造的数字。"""
    result = _coke(sales=[], sales_source=None, estimated_daily_sales=None)
    assert result.can_decide is False
    assert result.data_quality == QUALITY_NONE
    assert any("判不出" in alert for alert in result.alerts)


def test_advise_never_marks_estimated_product_stale():
    """估算口径不得判滞销 —— 拿猜出来的销量说人家卖不动，是最伤店主的一种错。"""
    result = _coke(sales=[], sales_source=None, estimated_daily_sales=0.0)
    assert result.stale is False


def test_promo_uplifts_replenish_quantity():
    plain = _coke()
    promo = _coke(events=[{"event_type": "promo"}])
    assert promo.safety_threshold == plain.safety_threshold   # 阈值不动
    assert promo.replenish_qty > plain.replenish_qty         # 只抬补货量
    assert promo.uplift_qty == pytest.approx(plain.safety_threshold * 0.3, abs=0.01)


def test_clearance_does_not_uplift():
    result = _coke(events=[{"event_type": "clearance"}])
    assert result.uplift_qty == 0.0
    assert result.has_clearance is True


# ---------------------------------------------------------------------------
# 判定闸（关键回归）
# ---------------------------------------------------------------------------

def test_reconcile_overrides_model_grade():
    """模型说「需要补货」，规则判「紧急补货」→ 最终必须是紧急补货。"""
    result = _coke()
    assert result.grade == GRADE_URGENT

    verdict = reconcile(result, {"grade": GRADE_REPLENISH, "replenish_qty": 5})

    assert verdict["final"]["grade"] == GRADE_URGENT
    assert verdict["final"]["replenish_qty"] == result.replenish_qty
    assert verdict["disagreement"]["model"] == GRADE_REPLENISH
    assert verdict["disagreement"]["rules"] == GRADE_URGENT


def test_reconcile_accepts_matching_grade_without_complaint():
    verdict = reconcile(_coke(), {"grade": GRADE_URGENT})
    assert verdict["disagreement"] is None


def test_enforce_grades_rewrites_wrong_grade_in_text():
    """数字全对、分级说错的建议最难人工复核 —— 文本侧必须兜住。"""
    result = _coke()
    text = "可乐现在 12 瓶，安全线 60 瓶，属于需要补货，建议再进 48 瓶。"

    fixed = enforce_grades(text, [result])

    assert "需要补货" not in fixed["text"]
    assert "紧急补货" in fixed["text"]
    assert fixed["fixes"][0]["model"] == GRADE_REPLENISH
    assert fixed["fixes"][0]["rules"] == GRADE_URGENT


def test_enforce_grades_leaves_ambiguous_sentences_alone():
    """认不出说的是哪个商品就别改 —— 宁可漏改，不乱改。"""
    text = "整体看下来，需要补货的有一批。"
    assert enforce_grades(text, [_coke()])["text"] == text


def test_enforce_grades_leaves_negated_grade_alone():
    """「不需要补货」里那个「需要补货」是模型在正确地否定，不是它把紧急补货说轻了。

    照改会变成「不需要紧急补货」—— 把对的话改成错的话。
    """
    text = "可乐现在不需要补货，先放着。"
    assert enforce_grades(text, [_coke()])["text"] == text


def test_enforce_grades_still_fixes_grade_after_comma():
    """否定词管不到标点后面：前面那句否定的是前半句，后半句该改还得改。"""
    text = "可乐库存不足，需要补货，建议再进 48 瓶。"
    fixed = enforce_grades(text, [_coke()])
    assert "需要补货" not in fixed["text"]
    assert "紧急补货" in fixed["text"]


# ---------------------------------------------------------------------------
# 规则文档与 config 对齐
# ---------------------------------------------------------------------------

def test_config_matches_rule_document():
    """TXT 是规则的自然语言权威表述，config 是它的代码落点。

    两边对不齐就是 bug —— 规则文件写着 1.2、代码跑着 1.3，谁也不知道该信哪个。
    """
    text = RULE_DOC.read_text(encoding="utf-8")
    for literal in (
        f"× {config.SAFETY_FACTOR}",
        f"× {config.EMERGENCY_DAYS}",
        f"最近 {config.TREND_WINDOW_WEEKS} 周",
        f"连续 {config.STALE_CONSECUTIVE_WEEKS} 周",
        f"{int(config.STALE_RATIO * 100)}%",
        f"1/{round(1 / config.EXPIRY_WARN_RATIO)}",
        f"{int(config.PROMO_UPLIFT_RATIO * 100)}%",
    ):
        assert literal in text, f"规则文档里找不到 {literal!r}，与 config 不一致"


def test_week_key_is_iso_week():
    key = week_key_of(date(2026, 10, 23))
    assert key == "2026-W43"