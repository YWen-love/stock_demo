"""界面层测试：卡片事件链 + 结构。

不起浏览器、不开端口。Gradio 的组件可以在进程内构造，
事件回调直接当普通函数调 —— 这里测的是"点了会发生什么"，
不是"长什么样"，长相得人看。

重点是盘点卡片那条闭环：展开 → 预填 → 提交 → 收起 → 摘要回写。
这个交互是整个产品的核心闭环，坏了就没有"每周盘一次"了。
"""

from __future__ import annotations

import inspect

import gradio as gr
import pandas as pd
import pytest

from advisor import repository as repo
from advisor import ui
from advisor.advisor import InventoryAdvisor
from advisor.db import init_db
from advisor.rules import previous_week_key, week_key_of
from advisor.ui import (
    CARD_COLUMNS,
    build_demo,
    frame_to_payload,
    rows_to_frame,
)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "ui.db"
    init_db(path)
    coke = repo.upsert_product("可口可乐", 12, unit="瓶", estimated_daily_sales=7,
                               db_path=path)
    repo.upsert_product("薯片", 40, unit="袋", estimated_daily_sales=4, db_path=path)

    week = week_key_of()
    repo.save_snapshot(coke["product_id"], previous_week_key(week), 25, 55, db_path=path)
    repo.save_sales(coke["product_id"], previous_week_key(week), 50, 25, 25, 55,
                    "derived", db_path=path)
    return path


# ---------------------------------------------------------------------------
# Dataframe 转换
# ---------------------------------------------------------------------------

def test_rows_to_frame_has_expected_columns(db):
    frame = rows_to_frame(InventoryAdvisor(db_path=db).stocktake_rows())
    assert list(frame.columns) == CARD_COLUMNS
    assert len(frame) == 2


def test_frame_to_payload_round_trip(db):
    advisor = InventoryAdvisor(db_path=db)
    frame = rows_to_frame(advisor.stocktake_rows())
    frame.loc[0, "本次库存"] = 6
    frame.loc[0, "本周进货"] = 20

    payload, _ = frame_to_payload(frame)

    assert payload[0]["name"] == "可口可乐"
    assert payload[0]["stock"] == 6
    assert payload[0]["purchase_qty"] == 20


def test_frame_to_payload_tolerates_blank_cells(db):
    """店主清空一格再提交，不该整张卡报错。"""
    frame = rows_to_frame(InventoryAdvisor(db_path=db).stocktake_rows())
    frame.loc[0, "本次库存"] = None
    payload, _ = frame_to_payload(frame)
    assert payload[0]["stock"] == 0.0


def test_frame_to_payload_handles_empty_input():
    assert frame_to_payload(None) == ([], [])
    assert frame_to_payload(pd.DataFrame()) == ([], [])


# ---------------------------------------------------------------------------
# 卡片事件链
# ---------------------------------------------------------------------------

def test_open_card_prefills_with_last_count(db):
    rows = {r["商品"]: r for r in InventoryAdvisor(db_path=db).stocktake_rows()}
    assert rows["可口可乐"]["上次盘点"] == 25
    assert rows["可口可乐"]["本次库存"] == 25   # 预填 = 上次数，只改改动过的
    assert rows["薯片"]["本次库存"] == 40


def test_submit_card_writes_to_db_and_returns_summary(db):
    """整条闭环：提交 → 写库 → 生成摘要 → 卡片标记为收起。"""
    advisor = InventoryAdvisor(db_path=db)
    frame = rows_to_frame(advisor.stocktake_rows())
    frame.loc[0, "本次库存"] = 6
    frame.loc[0, "本周进货"] = 20

    payload, _ = frame_to_payload(frame)
    rows = []
    for item in payload:
        product = repo.get_product_by_name(item["name"], db_path=db)
        rows.append({"product_id": product["product_id"], "stock": item["stock"],
                     "purchase_qty": item["purchase_qty"]})

    reply = advisor.submit_stocktake(rows)

    assert reply.open_card is False
    assert "本周盘点记好了" in reply.text
    assert repo.get_product_by_name("可口可乐", db_path=db)["current_stock"] == 6


def test_submit_card_reports_unknown_product_names(db):
    """卡片里被手改成不认识的字时要如实说，不猜。"""
    advisor = InventoryAdvisor(db_path=db)
    coke = repo.get_product_by_name("可口可乐", db_path=db)
    from advisor.ui import advisor_product

    assert advisor_product(advisor, "不存在的商品") is None
    assert advisor_product(advisor, "可乐")["product_id"] == coke["product_id"]


# ---------------------------------------------------------------------------
# 组件结构
# ---------------------------------------------------------------------------

def test_build_demo_constructs(tmp_path):
    """能构造出来就说明组件链没写错（事件 outputs 数量对不上会在这一步炸）。"""
    assert build_demo(db_path=tmp_path / "ui.db") is not None


def test_build_demo_default_hides_card(tmp_path):
    """卡片默认隐藏 —— 平时就是纯对话，店主不该一进来就面对一张表。"""
    demo = build_demo(db_path=tmp_path / "ui.db")
    card = next(b for b in demo.blocks.values()
                if getattr(b, "elem_id", None) == "stocktake_card")
    assert card.visible is False


def test_build_demo_registers_card_events(tmp_path):
    """卡片必须有事件链，否则就是一张只能看的死表。"""
    demo = build_demo(db_path=tmp_path / "ui.db")
    card = next(b for b in demo.blocks.values()
                if getattr(b, "elem_id", None) == "stocktake_card")
    triggers = [t for bf in demo.fns.values() for _, t in bf.targets]
    # 展开/预填/提交/取消/重置 都挂在卡片或常驻按钮上，至少要有若干条
    assert len([t for t in triggers if t is not None]) >= 5
    # 卡片那几条闭环一个都不能少：点开的、发送的、载入的
    assert {"click", "submit", "load"} <= set(triggers)


# ---------------------------------------------------------------------------
# 对话历史
# ---------------------------------------------------------------------------

def _chat_submit_fn(demo):
    """「发消息」那个事件的处理函数。触发器名在 Gradio 6 的 bf.targets 里。"""
    return next(bf for bf in demo.fns.values()
                if any(trigger == "submit" for _, trigger in bf.targets))


def _state_index(block_fn):
    """State 在 outputs 里的下标。不写死位置，接线一改就飘。"""
    return next(i for i, out in enumerate(block_fn.outputs)
                if isinstance(out, gr.State))


def _card_indexes(block_fn):
    """卡片容器和盘点表在 outputs 里的下标。"""
    card = next(i for i, out in enumerate(block_fn.outputs)
                if getattr(out, "elem_id", None) == "stocktake_card")
    table = next(i for i, out in enumerate(block_fn.outputs)
                 if isinstance(out, gr.Dataframe))
    return card, table


class _StubClient:
    """只会说一句话的假 client。界面测试不该碰网络。"""

    def __init__(self, text="可乐还剩 12 瓶，紧急补货，建议进 48 瓶。"):
        self.text = text
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        message = type("M", (), {"content": self.text, "tool_calls": []})()
        return type("R", (), {"choices": [type("C", (), {"message": message})()]})()


def test_normal_question_does_not_touch_the_card(db):
    """问「可乐该进多少」不该弹盘点卡，也不该把已经开着的卡收走。

    卡片里的数是店主手填的半成品，问一句就被收走等于白填；
    每条消息都弹一张表出来，界面就跟报表软件没区别了。
    """
    demo = build_demo(db_path=db, client=_StubClient())
    submit = _chat_submit_fn(demo)
    card_index, table_index = _card_indexes(submit)

    out = submit.fn("可乐该进多少", [])

    assert out[card_index] == ui.KEEP_CARD
    assert out[table_index] == ui.KEEP_CARD


def test_stocktake_question_opens_the_card(db):
    """「该盘点了」要把卡片打开，并带上预填好的表。"""
    demo = build_demo(db_path=db, client=_StubClient())
    submit = _chat_submit_fn(demo)
    card_index, table_index = _card_indexes(submit)

    out = submit.fn("该盘点了", [])

    assert out[card_index] == ui.OPEN_CARD
    assert isinstance(out[table_index], pd.DataFrame)
    assert list(out[table_index].columns) == CARD_COLUMNS


def test_chat_keeps_previous_turns(db):
    """多轮对话的历史不能每轮被冲掉。

    gr.State 只有作为事件的 output 才会被更新。之前它只挂在 inputs 上，
    于是 history 永远是 []：模型每轮都失忆，界面也只剩最新那两条。
    """
    demo = build_demo(db_path=db)
    submit = _chat_submit_fn(demo)
    history = [{"role": "user", "content": "该盘点了"}]

    # 走「该盘点了」这条 stocktake 分支不调模型，所以这条测试不碰网络。
    out = submit.fn("该盘点了", history)

    state = out[_state_index(submit)]
    assert len(state) == len(history) + 2
    assert state[0] == history[0]
    assert state[-2]["role"] == "user"
    assert state[-1]["role"] == "assistant"


def test_launch_kwargs_match_gradio_signature(monkeypatch):
    """Gradio 6 删过 launch() 的 show_api —— 这类参数删除测试是拦不住的。

    这里把 launch() 实际传给 .launch() 的参数抓出来，用真实签名 bind 一遍。
    """
    captured = {}

    class FakeDemo:
        def launch(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(ui, "build_demo", lambda **kwargs: FakeDemo())
    ui.launch(server_name="127.0.0.1", server_port=7860)

    assert captured, "launch() 一个参数都没传出去，测试等于没测"
    inspect.signature(gr.Blocks.launch).bind(None, **captured)