"""数据层测试：建表幂等、销量推导、盘点提交闭环。

销量推导是这个项目的命门 —— 店里一周盘一次，中间不记流水，
本周卖了多少全靠 `期初 + 进货 - 期末` 倒推。算错一次，动态阈值就跟着错，
而且要等下周盘点才发现。所以每个分支都单独测，包括算不出来的那条。
"""

from __future__ import annotations

import pytest

from advisor import repository as repo
from advisor.config import SOURCE_DERIVED, SOURCE_ESTIMATED
from advisor.db import init_db, reset_db
from advisor.rules import previous_week_key, week_key_of


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------

def test_init_db_is_idempotent(db):
    init_db(db)
    init_db(db)
    repo.upsert_product("可乐", 10, db_path=db)
    init_db(db)  # 已有数据的库再初始化一次，数据不能丢
    assert len(repo.list_products(db_path=db)) == 1


def test_reset_db_keeps_schema_but_clears_rows(db):
    repo.upsert_product("可乐", 10, db_path=db)
    reset_db(db)
    assert repo.list_products(db_path=db) == []
    repo.upsert_product("薯片", 5, db_path=db)  # 表还在，可以继续写
    assert len(repo.list_products(db_path=db)) == 1


# ---------------------------------------------------------------------------
# 商品
# ---------------------------------------------------------------------------

def test_upsert_product_is_idempotent_by_name(db):
    first = repo.upsert_product("可口可乐", 12, db_path=db)
    second = repo.upsert_product("可口可乐", 10, unit="瓶", db_path=db)
    assert first["product_id"] == second["product_id"]
    assert second["current_stock"] == 10
    assert second["unit"] == "瓶"


def test_upsert_product_rejects_blank_name(db):
    with pytest.raises(ValueError, match="不能为空"):
        repo.upsert_product("   ", 10, db_path=db)


def test_delete_product_is_soft(db):
    """软删除：历史销量要留着，否则上个月的结论会凭空变样。"""
    product = repo.upsert_product("可乐", 10, db_path=db)
    repo.delete_product(product["product_id"], db_path=db)
    assert repo.list_products(db_path=db) == []
    assert repo.get_product(product["product_id"], db_path=db)["status"] == "archived"


# ---------------------------------------------------------------------------
# 销量推导：三种分支
# ---------------------------------------------------------------------------

def test_sales_derived_from_previous_snapshot(db):
    """正常路径：本周销量 = 上期库存 + 本周进货 - 本期库存"""
    product = repo.upsert_product("可乐", db_path=db)
    pid = product["product_id"]
    week = week_key_of()
    prev = previous_week_key(week)

    repo.save_snapshot(pid, prev, 40, purchase_qty=30, db_path=db)
    repo.save_snapshot(pid, week, 22, purchase_qty=30, db_path=db)

    result = repo.commit_stocktake(
        [{"product_id": pid, "stock": 22, "purchase_qty": 30}], week, db_path=db)
    change = result["changes"][0]

    assert change["begin_stock"] == 40
    assert change["sales_qty"] == pytest.approx(48.0)   # 40 + 30 - 22
    assert change["source"] == SOURCE_DERIVED
    assert repo.recent_sales(pid, 4, db_path=db) == [48.0]
    assert repo.get_product(pid, db_path=db)["current_stock"] == 22


def test_sales_uses_latest_earlier_snapshot_when_week_is_skipped(db):
    """店主漏盘一周不该把销量算崩 —— 找的是「最近一次更早的盘点」，不是必须正好上一周。"""
    product = repo.upsert_product("可乐", db_path=db)
    pid = product["product_id"]
    week = week_key_of()

    # W-2 有盘点，上一周（W-1）漏了
    repo.save_snapshot(pid, previous_week_key(week, 2), 60, db_path=db)

    change = repo.commit_stocktake([{"product_id": pid, "stock": 40}],
                                   week, db_path=db)["changes"][0]

    assert change["begin_stock"] == 60
    assert change["sales_qty"] == pytest.approx(20.0)
    assert change["source"] == SOURCE_DERIVED


def test_cold_start_uses_estimated_daily_sales(db):
    product = repo.upsert_product("可乐", estimated_daily_sales=5, db_path=db)
    pid = product["product_id"]

    change = repo.commit_stocktake([{"product_id": pid, "stock": 20}],
                                   week_key_of(), db_path=db)["changes"][0]

    assert change["begin_stock"] == 35.0       # 估算日均 5 × 7
    assert change["source"] == SOURCE_ESTIMATED
    assert change["sales_qty"] == pytest.approx(15.0)


def test_no_history_and_no_estimate_refuses_to_derive(db):
    """既没上期又没估算 → 不写销量、不编数字，只如实说缺什么。"""
    product = repo.upsert_product("可乐", db_path=db)
    pid = product["product_id"]

    result = repo.commit_stocktake([{"product_id": pid, "stock": 20}],
                                   week_key_of(), db_path=db)
    change = result["changes"][0]

    assert change["sales_qty"] is None
    assert change["begin_stock"] is None
    assert repo.recent_sales(pid, 4, db_path=db) == []
    assert any("算不出销量" in w for w in result["warnings"])


def test_negative_sales_flags_unreported_purchase(db):
    """库存不降反涨 → 多半漏报进货。销量记 0（别污染均值），负数原样带出去。"""
    product = repo.upsert_product("可乐", db_path=db)
    pid = product["product_id"]
    week = week_key_of()

    repo.save_snapshot(pid, previous_week_key(week), 10, db_path=db)
    result = repo.commit_stocktake([{"product_id": pid, "stock": 30, "purchase_qty": 0}],
                                   week, db_path=db)

    change = result["changes"][0]
    assert change["sales_qty"] == 0.0
    assert change["negative_sales"] == pytest.approx(-20.0)
    assert any("漏填" in w for w in result["warnings"])
    assert repo.recent_sales(pid, 4, db_path=db) == [0.0]


# ---------------------------------------------------------------------------
# 盘点提交
# ---------------------------------------------------------------------------

def test_commit_stocktake_updates_purchase_batch_date(db):
    product = repo.upsert_product("可乐", db_path=db)
    pid = product["product_id"]
    repo.commit_stocktake([{"product_id": pid, "stock": 10, "purchase_qty": 25}],
                          week_key_of(), db_path=db)
    assert repo.get_product(pid, db_path=db)["batch_date"] is not None


def test_commit_stocktake_is_idempotent_within_same_week(db):
    """同一周重复提交要覆盖，不是叠加 —— 否则点两下提交销量就翻倍。"""
    product = repo.upsert_product("可乐", estimated_daily_sales=5, db_path=db)
    pid = product["product_id"]
    week = week_key_of()

    repo.commit_stocktake([{"product_id": pid, "stock": 20, "purchase_qty": 10}],
                          week, db_path=db)
    repo.commit_stocktake([{"product_id": pid, "stock": 15, "purchase_qty": 10}],
                          week, db_path=db)

    assert len(repo.recent_sales(pid, 10, db_path=db)) == 1
    assert repo.get_product(pid, db_path=db)["current_stock"] == 15


def test_commit_stocktake_skips_unknown_product(db):
    result = repo.commit_stocktake([{"product_id": 999, "stock": 5}],
                                   week_key_of(), db_path=db)
    assert result["changes"] == []
    assert any("不在档案里" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# 事件 / 对话
# ---------------------------------------------------------------------------

def test_events_are_scoped_to_current_and_future_weeks(db):
    product = repo.upsert_product("可乐", db_path=db)
    week = week_key_of()
    repo.add_event(product["product_id"], "promo", "上周促销", previous_week_key(week),
                   db_path=db)
    repo.add_event(product["product_id"], "promo", "这周促销", week, db_path=db)

    events = repo.list_events(week, db_path=db)
    assert [e["content"] for e in events] == ["这周促销"]


def test_messages_round_trip(db):
    conv = repo.ensure_conversation(db_path=db)
    repo.add_message(conv, "user", "该进多少可乐", db_path=db)
    repo.add_message(conv, "assistant", "进 48 瓶", {"tools": ["math_calc"]},
                     db_path=db)

    history = repo.load_messages(conv, db_path=db)
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["payload"] == {"tools": ["math_calc"]}
    assert history[0]["payload"] is None