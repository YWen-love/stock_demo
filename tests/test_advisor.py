"""编排层测试：意图路由 → 规则决策 → 讲人话 → 校验。

全部用假 client，不打网络。假 client 顺带当 LLM 桩复用：
它能精确控制模型说什么，才能验证"模型说错了系统会不会纠正"这类问题 ——
那正是这个项目最要紧的行为，光测成功路径是测不出来的。
"""

from __future__ import annotations

import json

import pytest

from advisor import repository as repo
from advisor.advisor import (
    INTENT_EXPIRY,
    INTENT_REPLENISH,
    INTENT_STALE,
    INTENT_STOCKTAKE,
    InventoryAdvisor,
    detect_intent,
)
from advisor.db import init_db
from advisor.rules import GRADE_STOCK, GRADE_URGENT, previous_week_key, week_key_of


# ---------------------------------------------------------------------------
# 假 client
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class FakeResponse:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message, "finish_reason": "stop"})()]


class FakeCompletions:
    """按剧本逐轮返回。剧本用完就重复最后一条。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[index] if self.script else FakeResponse(FakeMessage(""))


class FakeClient:
    def __init__(self, script=None, error=None):
        self.completions = FakeCompletions(script or [FakeResponse(FakeMessage("好的"))])
        self.chat = type("Chat", (), {"completions": self.completions})()
        self.error = error

    def fail_with(self, exc):
        def _raise(**kwargs):
            raise exc
        self.completions.create = _raise
        return self


class FakeAPIStatusError(Exception):
    def __init__(self, message="boom", status_code=500):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "advisor.db"
    init_db(path)
    return path


@pytest.fixture()
def stocked(db):
    """三周历史：急缺的 coke、够用的薯片、滞销的蛋糕。

    库存阶梯和销量都写死，进货量倒推 —— 和 scripts/seed_demo.py 同一套做法，
    免得"库存算成负数"这种自己编出来的脏数据混进测试。
    """
    coke = repo.upsert_product("可口可乐", 12, unit="瓶", estimated_daily_sales=7,
                               db_path=db)
    chips = repo.upsert_product("薯片", 40, unit="袋", estimated_daily_sales=4, db_path=db)
    cake = repo.upsert_product("老式蛋糕", 22, unit="个", db_path=db)

    week = week_key_of()
    histories = (
        (coke["product_id"], [44, 50, 57], [26, 16, 6], 30),
        (chips["product_id"], [26, 28, 27], [36, 34, 33], 28),
        (cake["product_id"], [0.2, 0.1, 0.05], [25, 24, 23], 25),
    )
    for pid, sales, ends, first_begin in histories:
        open_stock = first_begin
        for index, (sold, end) in enumerate(zip(sales, ends)):   # 由早到晚
            past_week = previous_week_key(week, len(sales) - index)
            purchase = round(open_stock + sold - end, 2)
            repo.save_snapshot(pid, past_week, end, purchase, db_path=db)
            repo.save_sales(pid, past_week, sold, open_stock, end, purchase,
                            "derived", db_path=db)
            open_stock = end
    return db


# ---------------------------------------------------------------------------
# 意图路由
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,intent", [
    ("该盘点了", INTENT_STOCKTAKE),
    ("帮我数一下货", INTENT_STOCKTAKE),
    ("有什么卖不动了", INTENT_STALE),
    ("老式蛋糕该清仓了吧", INTENT_STALE),
    ("酸奶快过期了吗", INTENT_EXPIRY),
    ("可乐该进多少", INTENT_REPLENISH),
    ("随便聊聊", INTENT_REPLENISH),
])
def test_detect_intent(text, intent):
    assert detect_intent(text) == intent


def test_focus_narrows_to_mentioned_product(stocked):
    advisor = InventoryAdvisor(db_path=stocked, client=FakeClient())
    advices = advisor.build_advice()
    focus = advisor._focus("可乐该进多少", advices)
    assert [a.name for a in focus] == ["可口可乐"]


def test_focus_matches_shorthand_name(stocked):
    """「可乐」要能认出「可口可乐」—— 店主不会照着档案名打字。"""
    advisor = InventoryAdvisor(db_path=stocked, client=FakeClient())
    focus = advisor._focus("可乐", advisor.build_advice())
    assert [a.name for a in focus] == ["可口可乐"]


def test_focus_stale_intent(stocked):
    advisor = InventoryAdvisor(db_path=stocked, client=FakeClient())
    focus = advisor._focus("有什么滞销的", advisor.build_advice())
    assert [a.name for a in focus] == ["老式蛋糕"]
    assert focus[0].stale is True


# ---------------------------------------------------------------------------
# 规则结论（不经模型）
# ---------------------------------------------------------------------------

def test_build_advice_grades_from_rules(stocked):
    advisor = InventoryAdvisor(db_path=stocked, client=FakeClient())
    by_name = {a.name: a for a in advisor.build_advice()}
    assert by_name["可口可乐"].grade == GRADE_URGENT
    assert by_name["薯片"].grade == GRADE_STOCK
    assert by_name["可口可乐"].trend_label == "略有上升"


# ---------------------------------------------------------------------------
# 判定闸（端到端）
# ---------------------------------------------------------------------------

def test_reply_rewrites_model_wrong_grade(stocked):
    """模型说「需要补货」，规则判「紧急补货」→ 出口必须是紧急补货。"""
    client = FakeClient([
        FakeResponse(FakeMessage("可乐现在 12 瓶，属于需要补货，建议再进 48 瓶。")),
    ])
    advisor = InventoryAdvisor(db_path=stocked, client=client)

    reply = advisor.reply("可乐该进多少")

    assert "需要补货" not in reply.text
    assert GRADE_URGENT in reply.text
    assert reply.fixes[0]["model"] == "需要补货"
    assert reply.fixes[0]["rules"] == GRADE_URGENT


def test_reply_keeps_model_text_when_grade_is_right(stocked):
    client = FakeClient([
        FakeResponse(FakeMessage("可乐撑不到两天，紧急补货，赶紧进 48 瓶。")),
    ])
    reply = InventoryAdvisor(db_path=stocked, client=client).reply("可乐该进多少")
    assert reply.fixes == []
    assert "紧急补货" in reply.text


def test_retry_when_model_outputs_table(stocked):
    """模型滑回旧 Excel 报表模式 → 拦下来让它重写。"""
    client = FakeClient([
        FakeResponse(FakeMessage("| 商品 | 库存 | 备注 |\n| --- | --- | --- |\n| 可乐 | 12 | 紧急补货 |")),
        FakeResponse(FakeMessage("可乐只剩 12 瓶了，紧急补货，建议进 48 瓶。")),
    ])
    reply = InventoryAdvisor(db_path=stocked, client=client).reply("可乐该进多少")

    assert reply.retried is True
    assert "|" not in reply.text


def test_illegal_table_repeatedly_falls_back_to_rules(stocked):
    """连着直出表格 → 降级到纯规则输出，而不是把表格甩给店主。"""
    client = FakeClient([
        FakeResponse(FakeMessage("| 商品 | 备注 |\n| --- | --- |\n| 可乐 | 紧急补货 |")),
    ])
    reply = InventoryAdvisor(db_path=stocked, client=client).reply("可乐该进多少")

    assert reply.text
    assert "|" not in reply.text


# ---------------------------------------------------------------------------
# 兜底闸
# ---------------------------------------------------------------------------

def test_api_failure_falls_back_to_pure_rules(stocked):
    """模型挂了对话照样能用 —— 数字本来就是 rules 算的。"""
    client = FakeClient().fail_with(RuntimeError("network down"))
    reply = InventoryAdvisor(db_path=stocked, client=client).reply("这周该进什么")

    assert reply.fallback is True
    assert "可口可乐" in reply.text
    assert "紧急补货" in reply.text


def test_fallback_mentions_stale_item(stocked):
    client = FakeClient().fail_with(RuntimeError("boom"))
    reply = InventoryAdvisor(db_path=stocked, client=client).reply("有什么卖不动了")
    assert "老式蛋糕" in reply.text
    assert "清仓" in reply.text or "先清" in reply.text


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def test_math_calc_tool_rejects_injection():
    from advisor.tools import ToolContext, execute_tool

    result = json.loads(execute_tool("math_calc", {"expression": "__import__('os')"},
                                      ToolContext()))
    assert result["ok"] is False


def test_math_calc_tool_computes():
    from advisor.tools import ToolContext, execute_tool

    result = json.loads(execute_tool("math_calc", {"expression": "50.33*1.2"},
                                      ToolContext()))
    assert result == {"ok": True, "expression": "50.33*1.2", "result": 60.4}


def test_submit_advice_tool_overrides_claim(stocked):
    """模型报上来的分级要和规则口径对拍，不一致当场纠正。"""
    from advisor.tools import ToolContext, execute_tool

    advisor = InventoryAdvisor(db_path=stocked, client=FakeClient())
    advices = advisor.build_advice()
    ctx = ToolContext(db_path=stocked, advice_cache={a.name: a for a in advices})

    result = json.loads(execute_tool(
        "submit_advice",
        {"name": "可口可乐", "grade": "需要补货", "replenish_qty": 48, "reason": "不够卖"},
        ctx))

    assert result["最终口径"]["grade"] == GRADE_URGENT
    assert "不符" in result["说明"]
    assert ctx.submissions[0]["disagreement"]["model"] == "需要补货"


def test_lookup_advice_rejects_one_char_names(stocked):
    """「水」不能匹配上「矿泉水」——认错商品等于把别的商品的补货量交给模型。"""
    from advisor.tools import ToolContext, _lookup_advice

    repo.upsert_product("矿泉水", 30, unit="瓶", estimated_daily_sales=2,
                        db_path=stocked)
    advisor = InventoryAdvisor(db_path=stocked, client=FakeClient())
    advices = advisor.build_advice()
    ctx = ToolContext(db_path=stocked, advice_cache={a.name: a for a in advices})

    assert _lookup_advice("可乐", ctx).name == "可口可乐"
    assert _lookup_advice("水", ctx) is None


def test_record_event_tool_writes_to_db(stocked):
    from advisor.tools import ToolContext, execute_tool

    week = week_key_of()
    ctx = ToolContext(db_path=stocked, week_key=week)
    execute_tool("record_event",
                 {"product_name": "可口可乐", "event_type": "promo",
                  "content": "下周三促销"},
                 ctx)

    events = repo.list_events(week, product_id=None, db_path=stocked)
    assert [e["content"] for e in events] == ["下周三促销"]


# ---------------------------------------------------------------------------
# 盘点卡片闭环
# ---------------------------------------------------------------------------

def test_stocktake_rows_prefill_with_last_count(stocked):
    advisor = InventoryAdvisor(db_path=stocked, client=FakeClient())
    rows = {r["商品"]: r for r in advisor.stocktake_rows()}

    assert rows["可口可乐"]["本次库存"] == rows["可口可乐"]["上次盘点"]
    assert rows["可口可乐"]["本周进货"] == 0.0


def test_stocktake_reply_asks_to_open_card(stocked):
    reply = InventoryAdvisor(db_path=stocked, client=FakeClient()).reply("该盘点了")
    assert reply.open_card is True
    assert reply.intent == INTENT_STOCKTAKE


def test_submit_stocktake_closes_loop(stocked):
    """卡片提交 → 写库 → 摘要回写 → 卡片收起。"""
    advisor = InventoryAdvisor(db_path=stocked, client=FakeClient())
    coke = repo.get_product_by_name("可口可乐", db_path=stocked)

    reply = advisor.submit_stocktake([
        {"product_id": coke["product_id"], "stock": 8, "purchase_qty": 20},
    ])

    assert reply.open_card is False
    assert "本周盘点记好了" in reply.text
    assert repo.get_product(coke["product_id"], db_path=stocked)["current_stock"] == 8
    # 上期 6 + 本周进 20 - 本期 8
    assert repo.recent_sales(coke["product_id"], 1, db_path=stocked) == [18.0]


def test_submit_stocktake_warns_about_unreported_purchase(stocked):
    advisor = InventoryAdvisor(db_path=stocked, client=FakeClient())
    coke = repo.get_product_by_name("可口可乐", db_path=stocked)

    reply = advisor.submit_stocktake([{"product_id": coke["product_id"], "stock": 60}])

    assert "漏填" in reply.text


def test_prefill_by_average(stocked):
    rows = {r["商品"]: r for r in
            InventoryAdvisor(db_path=stocked, client=FakeClient()).prefill_by_average()}
    coke = rows["可口可乐"]
    assert coke["本次库存"] < coke["上次盘点"]


# ---------------------------------------------------------------------------
# 活动备货
# ---------------------------------------------------------------------------

def test_promo_event_raises_recommended_quantity(stocked):
    week = week_key_of()
    coke = repo.get_product_by_name("可口可乐", db_path=stocked)
    plain = {a.name: a for a in InventoryAdvisor(db_path=stocked, client=FakeClient()).build_advice()}

    repo.add_event(coke["product_id"], "promo", "促销", week, db_path=stocked)
    boosted = {a.name: a for a in InventoryAdvisor(db_path=stocked, client=FakeClient()).build_advice()}

    assert boosted["可口可乐"].replenish_qty > plain["可口可乐"].replenish_qty
    assert boosted["可口可乐"].safety_threshold == plain["可口可乐"].safety_threshold


# ---------------------------------------------------------------------------
# 冷启动引导
# ---------------------------------------------------------------------------

def test_onboarding_creates_products_from_chat(db):
    """建档全程靠对话，不碰表格。"""
    client = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[
            FakeToolCall("c1", "upsert_product",
                         json.dumps({"name": "可口可乐", "stock": 12})),
            FakeToolCall("c2", "upsert_product",
                         json.dumps({"name": "薯片", "stock": 30})),
        ])),
        FakeResponse(FakeMessage("记下了，可乐 12 瓶、薯片 30 袋。下周点盘点按钮就行。")),
    ])
    reply = InventoryAdvisor(db_path=db, client=client).reply("卖可乐和薯片，可乐还有12瓶")

    names = [p["name"] for p in repo.list_products(db_path=db)]
    assert names == ["可口可乐", "薯片"]
    assert "记下了" in reply.text


def test_onboarding_greeting_when_user_confirms(db):
    advisor = InventoryAdvisor(db_path=db, client=FakeClient())
    reply = advisor.reply("就这两样")
    assert reply.intent == "onboarding"