"""规则引擎 —— **全部数值与分级的唯一实现点**。

一句话原则（从 hr-ai-assessment/rule_engine.py 借来的）：
> 决策逻辑必须只有一个实现点，否则两处各判一次迟早判出两个结果。

所以这个文件：
- 不 import 任何 LLM / SDK / 网络的东西（可以在没有 API 的环境里单测）
- 阈值、补货量、**分级**、滞销、过期、活动备货，全在这里算完
- 模型拿到的是这里算出的结论，只负责翻译成人话；模型说什么都不改这里的数

「紧急补货」和「需要补货」的分级尤其要锁死——数字全对、分级错，是人工复核最难发现的一类错误。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from .config import (
    EMERGENCY_DAYS,
    EVENT_CLEARANCE,
    EVENT_PROMO,
    EXPIRY_WARN_RATIO,
    GRADE_REPLENISH,
    GRADE_STOCK,
    GRADE_URGENT,
    GRADES,
    PROMO_UPLIFT_RATIO,
    SAFETY_FACTOR,
    STALE_CONSECUTIVE_WEEKS,
    STALE_MIN_WEEKLY,
    STALE_RATIO,
    TREND_WINDOW_WEEKS,
)

# 数据可信度：三个档，模型措辞要跟着变
QUALITY_HISTORY = "history"       # 有真实周销量
QUALITY_ESTIMATED = "estimated"   # 冷启动，用估算日均推的
QUALITY_NONE = "none"             # 连估算都没有 —— 按 PRD，不能算，要说判不出


# ---------------------------------------------------------------------------
# 周次
# ---------------------------------------------------------------------------

def week_key_of(when: Optional[date] = None) -> str:
    """ISO 周键，如 2026-W43。销量历史和快照都以它为轴。"""
    day = when or date.today()
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def parse_week_key(key: str) -> date:
    """周键 → 该周周一。解析不了就抛，别猜。"""
    year_str, _, week_str = str(key).partition("-W")
    return date.fromisocalendar(int(year_str), int(week_str), 1)


def previous_week_key(key: str, offset: int = 1) -> str:
    from datetime import timedelta

    return week_key_of(parse_week_key(key) - timedelta(weeks=offset))


# ---------------------------------------------------------------------------
# 基础公式
# ---------------------------------------------------------------------------

def safety_threshold(avg_weekly_sales: float) -> float:
    """常规安全库存阈值 = 滚动周均销量 × 1.2"""
    return round(max(0.0, avg_weekly_sales) * SAFETY_FACTOR, 2)


def emergency_threshold(avg_weekly_sales: float) -> float:
    """紧急断货阈值 = 近两天总销量 × 1.2 = 周均 ÷ 7 × 2 × 1.2"""
    daily = max(0.0, avg_weekly_sales) / 7.0
    return round(daily * EMERGENCY_DAYS * SAFETY_FACTOR, 2)


def replenish_qty(current_stock: float, safety: float, uplift: float = 0.0) -> float:
    """补货量 = 安全阈值(+活动上浮) - 当前库存，不足 0 记 0"""
    return round(max(0.0, safety + uplift - max(0.0, current_stock)), 2)


def grade_of(current_stock: float, safety: float, emergency: float) -> str:
    """三档分级。注意边界：**等于阈值算上面一档**（≥ 安全线就是库存充足）。

    这条不能反：current == emergency 时判成「紧急补货」会让店主在库存正好压线时被反复催货。
    """
    stock = max(0.0, current_stock)
    if stock >= safety:
        return GRADE_STOCK
    if stock >= emergency:
        return GRADE_REPLENISH
    return GRADE_URGENT


# ---------------------------------------------------------------------------
# 滚动均值 / 趋势 / 滞销 / 过期
# ---------------------------------------------------------------------------

def rolling_avg(sales: Sequence[float], window: int = TREND_WINDOW_WEEKS) -> float:
    """最近 N 周销量均值，不足 N 周按已有周算（不是按 0 摊薄）。"""
    values = [float(s) for s in sales if s is not None]
    if not values:
        return 0.0
    recent = values[-window:]
    return round(sum(recent) / len(recent), 2)


TREND_UP_STRONG = "明显上升"
TREND_UP = "略有上升"
TREND_FLAT = "基本持平"
TREND_DOWN = "略有下滑"
TREND_DOWN_STRONG = "明显下滑"
TREND_UNKNOWN = "数据不足"


def trend_of(sales: Sequence[float]) -> Dict[str, Any]:
    """环比趋势。只看最近两周 —— 拿 8 周前的数去判本周涨跌没有意义。"""
    values = [float(s) for s in sales if s is not None]
    if len(values) < 2:
        return {"label": TREND_UNKNOWN, "pct": None, "recent": values}

    previous, latest = values[-2], values[-1]
    if previous <= 0:
        # 上周卖 0 本周卖 5，算不出百分比，如实说"从零起量"
        label = TREND_UP_STRONG if latest > 0 else TREND_FLAT
        return {"label": label, "pct": None, "recent": values}

    pct = (latest - previous) / previous
    if pct >= 0.15:
        label = TREND_UP_STRONG
    elif pct >= 0.05:
        label = TREND_UP
    elif pct <= -0.15:
        label = TREND_DOWN_STRONG
    elif pct <= -0.05:
        label = TREND_DOWN
    else:
        label = TREND_FLAT
    return {"label": label, "pct": round(pct * 100, 1), "recent": values}


def is_stale(sales: Sequence[float], window: int = TREND_WINDOW_WEEKS) -> bool:
    """滞销：连续 N 周卖得极少。

    门槛取 `周均 × 20%` 和「每周不到一件」里更高的那个。后半句是必须的：
    一周只卖零点几件的老货，它自己的周均也只有零点几，拿周均当尺子量自己，
    永远量不出"不动"。滞销判定是拿来抓这种货的，不能要求它先动起来才判得了。
    """
    values = [float(s) for s in sales if s is not None]
    tail = values[-window:]
    if len(tail) < STALE_CONSECUTIVE_WEEKS:
        return False
    baseline = rolling_avg(values, window)
    threshold = max(baseline * STALE_RATIO, STALE_MIN_WEEKLY)
    return all(v < threshold for v in tail[-STALE_CONSECUTIVE_WEEKS:])


def expiry_days_left(shelf_life_days: Optional[int], batch_date: Optional[str],
                      today: Optional[date] = None) -> Optional[int]:
    """剩余保质期天数。没填保质期或进货日就返回 None —— 不做没依据的告警。"""
    if not shelf_life_days or not batch_date:
        return None
    try:
        shipped = datetime.strptime(str(batch_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (shipped + _one_day_timedelta(shelf_life_days) - (today or date.today())).days


def _one_day_timedelta(days: int):
    from datetime import timedelta

    return timedelta(days=int(days))


def expiry_warning(days_left: Optional[int], shelf_life_days: Optional[int]) -> bool:
    """剩余不足总保质期 1/3 就提醒。已经过期的当然也提醒（剩余为负）。"""
    if days_left is None:
        return False
    total = int(shelf_life_days or 0)
    return days_left <= total * EXPIRY_WARN_RATIO


# ---------------------------------------------------------------------------
# 结论对象
# ---------------------------------------------------------------------------

@dataclass
class ProductAdvice:
    """一个商品的完整结论。UI、提示词、测试都只认这一个结构。"""

    product_id: int
    name: str
    unit: str = "件"
    current_stock: float = 0.0

    sales_recent: List[float] = field(default_factory=list)
    avg_weekly_sales: float = 0.0

    safety_threshold: float = 0.0
    emergency_threshold: float = 0.0
    replenish_qty: float = 0.0
    grade: str = GRADE_STOCK

    trend_label: str = TREND_UNKNOWN
    trend_pct: Optional[float] = None

    stale: bool = False
    has_promo: bool = False
    has_clearance: bool = False
    uplift_qty: float = 0.0

    expiry_days_left: Optional[int] = None
    expiry_warning: bool = False

    data_quality: str = QUALITY_NONE
    alerts: List[str] = field(default_factory=list)

    @property
    def needs_replenish(self) -> bool:
        return self.grade != GRADE_STOCK

    @property
    def can_decide(self) -> bool:
        """判不出结论时为 False。按 PRD：宁可说判不出，也不给个编出来的数。"""
        return self.data_quality != QUALITY_NONE

    def to_grounding(self) -> Dict[str, Any]:
        """喂给模型的那份 JSON。

        刻意**只给结论不给公式**：模型要做算术就得调 math_calc，
        这样它算错有迹可循，也不至于自己编一个阈值出来。
        """
        return {
            "商品": self.name,
            "单位": self.unit,
            "当前库存": self.current_stock,
            "最近几周销量": self.sales_recent,
            "周均销量": self.avg_weekly_sales,
            "安全库存线": self.safety_threshold,
            "紧急断货线": self.emergency_threshold,
            "补货量": self.replenish_qty,
            "库存状态": self.grade,
            "趋势": self.trend_label,
            "本周有活动": self.has_promo,
            "滞销": self.stale,
            "剩余保质期天数": self.expiry_days_left,
            "告警": self.alerts,
            "数据可信度": self.data_quality,
        }


def _build_alerts(
    *,
    stale: bool,
    current_stock: float,
    avg_weekly_sales: float,
    expiry_left: Optional[int],
    expiry_warn: bool,
    negative_sales: Optional[float],
    quality: str,
) -> List[str]:
    """告警由规则确定性生成，措辞也归这里管 —— 模型不自己编告警。"""
    alerts: List[str] = []
    if stale:
        alerts.append("连续数周基本没动货，属于滞销，建议先清仓再谈补货")
    if expiry_warn and expiry_left is not None:
        if expiry_left < 0:
            alerts.append(f"已过期 {-expiry_left} 天，必须尽快清掉，别再进了")
        else:
            alerts.append(f"只剩 {expiry_left} 天到期，先进临期的、能卖的先卖")
    if negative_sales is not None and negative_sales < 0:
        alerts.append("库存不降反涨，这周的进货量可能没记全")
    if quality == QUALITY_ESTIMATED:
        alerts.append("还没有真实销量，这条结论是按估算日均推的，仅供参考")
    elif quality == QUALITY_NONE:
        alerts.append("既没有历史销量也没有估算日均，判不出补货量，请先补上数据")
    return alerts


def advise(
    *,
    product_id: int,
    name: str,
    current_stock: float,
    sales: Sequence[float],
    unit: str = "件",
    estimated_daily_sales: Optional[float] = None,
    sales_source: Optional[str] = None,
    events: Sequence[Dict[str, Any]] = (),
    shelf_life_days: Optional[int] = None,
    batch_date: Optional[str] = None,
    negative_sales: Optional[float] = None,
    today: Optional[date] = None,
) -> ProductAdvice:
    """把「一个商品的原始事实」变成「一条可以直接说给人听的结论」。

    sales_source 传 'derived' / 'manual' 表示有真实销量；传 'estimated' 或 None
    且 estimated_daily_sales 为空 → data_quality 降级。
    """
    values = [float(s) for s in sales if s is not None]
    window = values[-TREND_WINDOW_WEEKS:]

    if values:
        avg = rolling_avg(values)
        quality = QUALITY_ESTIMATED if sales_source == "estimated" else QUALITY_HISTORY
    elif estimated_daily_sales:
        # 冷启动（需求 9）：没有历史就拿估算日均按周推一周
        avg = round(float(estimated_daily_sales) * 7, 2)
        quality = QUALITY_ESTIMATED
    else:
        avg = 0.0
        quality = QUALITY_NONE

    safety = safety_threshold(avg)
    emergency = emergency_threshold(avg)

    event_types = {str(e.get("event_type") or "") for e in events}
    has_promo = EVENT_PROMO in event_types
    has_clearance = EVENT_CLEARANCE in event_types

    uplift = round(safety * PROMO_UPLIFT_RATIO, 2) if (has_promo and quality != QUALITY_NONE) else 0.0
    replenish = replenish_qty(current_stock, safety, uplift)

    trend = trend_of(values)
    stale = is_stale(values) if quality == QUALITY_HISTORY else False

    left = expiry_days_left(shelf_life_days, batch_date, today)
    warn = expiry_warning(left, shelf_life_days)

    return ProductAdvice(
        product_id=product_id,
        name=name,
        unit=unit,
        current_stock=round(float(current_stock), 2),
        sales_recent=[round(v, 2) for v in window],
        avg_weekly_sales=avg,
        safety_threshold=safety,
        emergency_threshold=emergency,
        replenish_qty=replenish,
        grade=grade_of(current_stock, safety, emergency),
        trend_label=trend["label"],
        trend_pct=trend["pct"],
        stale=stale,
        has_promo=has_promo,
        has_clearance=has_clearance,
        uplift_qty=uplift,
        expiry_days_left=left,
        expiry_warning=warn,
        data_quality=quality,
        alerts=_build_alerts(
            stale=stale,
            current_stock=float(current_stock),
            avg_weekly_sales=avg,
            expiry_left=left,
            expiry_warn=warn,
            negative_sales=negative_sales,
            quality=quality,
        ),
    )


#: 名字模糊匹配的最短长度。两字是甜点：再短会把「水」匹配上矿泉水、纯净水、苏打水一大片，
#: 再长就认不出「可口可乐」简称的「可乐」。
NAME_MATCH_MIN = 2


def name_matches(product_name: str, text: str) -> bool:
    """一句话里是不是提到了这个商品。

    严格相等不够用：商品档案里叫「可口可乐」，模型顺嘴写「可乐」很常见，
    而判定闸恰恰要在这句话上动手脚 —— 认不出来就等于没设防。

    做法是取商品名里任意一个长度为 NAME_MATCH_MIN 的连续片段去撞。
    比"去掉首字再比"稳：中文商品名常把品类放在后面（可口**可乐**、老式**蛋糕**），
    只砍首字会把真正有区别的那部分砍掉。
    """
    name = (product_name or "").strip()
    if not name or not text:
        return False
    if name in text:
        return True
    if len(name) < NAME_MATCH_MIN:
        return False
    return any(name[i:i + NAME_MATCH_MIN] in text
               for i in range(len(name) - NAME_MATCH_MIN + 1))


# ---------------------------------------------------------------------------
# 判定闸
# ---------------------------------------------------------------------------

def reconcile(advice: ProductAdvice, model_claim: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """**强制覆盖**模型写的数字和分级。

    模型返回的 grade / replenish_qty / safety_threshold 一个都不采信，
    全部换成 rules.py 算的值。model_claim 只作为留档，方便观察模型偏了多少。
    """
    final = {
        "grade": advice.grade,
        "replenish_qty": advice.replenish_qty,
        "safety_threshold": advice.safety_threshold,
        "emergency_threshold": advice.emergency_threshold,
    }
    disagreement = None
    if model_claim:
        claim_grade = str(model_claim.get("grade") or "").strip()
        if claim_grade and claim_grade in (GRADE_STOCK, GRADE_REPLENISH, GRADE_URGENT):
            if claim_grade != advice.grade:
                disagreement = {
                    "field": "grade",
                    "model": claim_grade,
                    "rules": advice.grade,
                    "reason": "模型分级与规则口径不符，以规则为准",
                }
    return {"final": final, "disagreement": disagreement}


def render_advice_line(advice: ProductAdvice) -> str:
    """纯规则口径的一句话结论。模型不可用时的兜底，也用作测试断言。"""
    if not advice.can_decide:
        return f"{advice.name}：{advice.alerts[-1] if advice.alerts else '数据不足，判不出补货量'}"
    if advice.grade == GRADE_STOCK:
        return f"{advice.name}：{advice.grade}，暂时不用进。"
    if advice.stale:
        return f"{advice.name}：{advice.grade}，但这货基本不动，建议先清，别急着进。"
    return f"{advice.name}：{advice.grade}，建议再进 {advice.replenish_qty:g} {advice.unit}。"


#: 分级词前面挂着否定词时，不算「模型说错了分级」。
#: 「不需要补货」里的那个「需要补货」是模型在正确地否定，不是它把「紧急补货」说轻了；
#: 照改会造出「不需要紧急补货」这种自相矛盾的话 —— 模型本来是对的，闸给改错了。
_NEGATIONS = ("不", "没", "无需", "不用", "不必", "别", "甭")

#: 否定词管不到标点后面去 ——「库存不足，需要补货」里的「不」跟后半句没关系。
_NEGATION_BOUNDARY = "，,。；;！？!?、\n"


def _find_all(text: str, word: str) -> List[int]:
    """word 在 text 里所有出现的起始下标。"""
    positions: List[int] = []
    start = 0
    while True:
        index = text.find(word, start)
        if index < 0:
            return positions
        positions.append(index)
        start = index + len(word)


def _is_negated(text: str, index: int) -> bool:
    """text[index] 处的分级词是不是被否定着。"""
    segment = text[:index]
    cut = -1
    for boundary in _NEGATION_BOUNDARY:
        cut = max(cut, segment.rfind(boundary))
    if cut >= 0:
        segment = segment[cut + 1:]
    return any(negation in segment for negation in _NEGATIONS)


def enforce_grades(text: str, advices: Sequence[ProductAdvice]) -> Dict[str, Any]:
    """判定闸的文本侧：把模型说错的分级就地改回来。

    为什么要做这件事：模型算的数经常是对的（阈值、补货量都能对上），
    但**分级会挑一个听起来没那么吓人的说法**——"紧急补货"听上去太严重，
    于是写成"需要补货"。数字对、分级错的建议，店主按它进了货，等到断货才发现不对。

    做法：按句切开，每句认出说的是哪个商品，只在能唯一确定对应关系时替换。
    认不出来（"整体建议"这类总括句）就不动 —— 宁可漏改，不乱改。
    被否定的分级词（"不需要补货"）同样不动 —— 那是模型在说对的反面。
    """
    if not text or not advices:
        return {"text": text, "fixes": []}

    by_name = {a.name: a for a in advices}
    fixes: List[Dict[str, str]] = []
    # 句末标点切分，保留分隔符
    pieces = re.split(r"(?<=[。！？!?\n])", text)
    result: List[str] = []

    for piece in pieces:
        if not piece:
            continue
        mentioned = [a for a in by_name.values() if name_matches(a.name, piece)]
        if len(mentioned) == 1:
            advice = mentioned[0]
            for wrong in GRADES:
                if wrong == advice.grade:
                    continue
                spans = [index for index in _find_all(piece, wrong)
                         if not _is_negated(piece, index)]
                if not spans:
                    continue
                # 从后往前替换，前面的下标才不会因为长度变了而失效
                for index in reversed(spans):
                    piece = piece[:index] + advice.grade + piece[index + len(wrong):]
                fixes.append({
                    "product": advice.name,
                    "model": wrong,
                    "rules": advice.grade,
                })
        result.append(piece)

    return {"text": "".join(result), "fixes": fixes}