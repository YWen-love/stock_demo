"""数据读写层：商品 / 库存快照 / 销量历史 / 事件 / 对话。

**销量推导**是这个项目的核心机制（需求 5）：小店老板一周盘一次货，中间不记流水，
所以每周的销量不是记出来的，是倒推出来的：

    本周销量 = 上期库存 + 本周进货 - 本期库存

三个分支必须都处理，不能只跑通顺的那条：
- 有上期 → 正常推导，source='derived'
- 没上期但填了估算日均 → 按估算周量兜底，source='estimated'，并在告警里说明是估算
- 两者都没有 → **不写销量、判不出补货量**，如实回问店主要数据（不给编出来的数）
- 算出来是负数 → 说明货没少反而多了，多半是进货漏报；销量记 0（别污染均值），
  但把负数原样带出去，让告警去问「这周到底进了多少」
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Sequence

from .config import (
    SOURCE_DERIVED,
    SOURCE_ESTIMATED,
    SOURCE_MANUAL,
    TREND_WINDOW_WEEKS,
)
from .db import dumps, init_db, loads, session
from .rules import week_key_of


def _row(row) -> Dict[str, Any]:
    return dict(row) if row is not None else {}


# ---------------------------------------------------------------------------
# 商品
# ---------------------------------------------------------------------------

def list_products(include_inactive: bool = False, db_path: Any = None) -> List[Dict[str, Any]]:
    init_db(db_path)
    where = "" if include_inactive else " WHERE status = 'active'"
    with session(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM products{where} ORDER BY product_id"
        ).fetchall()
    return [_row(r) for r in rows]


def get_product(product_id: int, db_path: Any = None) -> Optional[Dict[str, Any]]:
    init_db(db_path)
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE product_id = ?", (product_id,)
        ).fetchone()
    return _row(row) if row else None


def get_product_by_name(name: str, db_path: Any = None) -> Optional[Dict[str, Any]]:
    init_db(db_path)
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE name = ? COLLATE NOCASE", (name.strip(),)
        ).fetchone()
    return _row(row) if row else None


def upsert_product(
    name: str,
    stock: float = 0.0,
    *,
    unit: str = "件",
    estimated_daily_sales: Optional[float] = None,
    shelf_life_days: Optional[int] = None,
    batch_date: Optional[str] = None,
    note: Optional[str] = None,
    db_path: Any = None,
) -> Dict[str, Any]:
    """按名字建档或更新。冷启动引导和卡片都走这一个入口，避免两条写库路径。

    同名商品重复调用是更新不是新增 —— 引导时店主可能把「可乐」说了两遍。
    """
    init_db(db_path)
    clean = (name or "").strip()
    if not clean:
        raise ValueError("商品名称不能为空")

    with session(db_path) as conn:
        existing = conn.execute(
            "SELECT product_id FROM products WHERE name = ? COLLATE NOCASE", (clean,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE products
                   SET current_stock = ?, unit = COALESCE(?, unit),
                       estimated_daily_sales = COALESCE(?, estimated_daily_sales),
                       shelf_life_days = COALESCE(?, shelf_life_days),
                       batch_date = COALESCE(?, batch_date),
                       note = COALESCE(?, note),
                       updated_at = datetime('now')
                 WHERE product_id = ?
                """,
                (float(stock), unit, estimated_daily_sales, shelf_life_days,
                 batch_date, note, existing["product_id"]),
            )
            product_id = existing["product_id"]
        else:
            cursor = conn.execute(
                """
                INSERT INTO products (name, unit, current_stock, estimated_daily_sales,
                                      shelf_life_days, batch_date, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (clean, unit, float(stock), estimated_daily_sales,
                 shelf_life_days, batch_date, note),
            )
            product_id = cursor.lastrowid
        row = conn.execute(
            "SELECT * FROM products WHERE product_id = ?", (product_id,)
        ).fetchone()
    return _row(row)


def update_stock(product_id: int, stock: float, db_path: Any = None) -> None:
    init_db(db_path)
    with session(db_path) as conn:
        conn.execute(
            "UPDATE products SET current_stock = ?, updated_at = datetime('now') "
            "WHERE product_id = ?",
            (float(stock), product_id),
        )


def set_shelf_life(product_id: int, shelf_life_days: Optional[int],
                   batch_date: Optional[str], db_path: Any = None) -> None:
    """补保质期 / 进货日，用来做快过期告警。店主是随口说的，来了就存。"""
    init_db(db_path)
    with session(db_path) as conn:
        conn.execute(
            "UPDATE products SET shelf_life_days = COALESCE(?, shelf_life_days), "
            "batch_date = COALESCE(?, batch_date), updated_at = datetime('now') "
            "WHERE product_id = ?",
            (shelf_life_days, batch_date, product_id),
        )


def delete_product(product_id: int, db_path: Any = None) -> None:
    """软删除。历史销量要留着，否则上个月的结论会凭空变样。"""
    init_db(db_path)
    with session(db_path) as conn:
        conn.execute(
            "UPDATE products SET status = 'archived', updated_at = datetime('now') "
            "WHERE product_id = ?",
            (product_id,),
        )


# ---------------------------------------------------------------------------
# 库存快照
# ---------------------------------------------------------------------------

def save_snapshot(product_id: int, week_key: str, stock: float,
                  purchase_qty: float = 0.0, source: str = SOURCE_MANUAL,
                  note: Optional[str] = None, db_path: Any = None) -> None:
    init_db(db_path)
    with session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO inventory_snapshots
                   (week_key, product_id, stock, purchase_qty, source, note)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id, week_key) DO UPDATE SET
                   stock = excluded.stock,
                   purchase_qty = excluded.purchase_qty,
                   source = excluded.source,
                   note = excluded.note,
                   created_at = datetime('now')
            """,
            (week_key, product_id, float(stock), float(purchase_qty or 0), source, note),
        )


def last_snapshot_before(product_id: int, week_key: str,
                         db_path: Any = None) -> Optional[Dict[str, Any]]:
    """最近一次**早于**本周的盘点。不用「正好上周」——店主漏盘一周不该把销量算崩。"""
    init_db(db_path)
    with session(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM inventory_snapshots
             WHERE product_id = ? AND week_key < ?
             ORDER BY week_key DESC LIMIT 1
            """,
            (product_id, week_key),
        ).fetchone()
    return _row(row) if row else None


def latest_snapshot(product_id: int, db_path: Any = None) -> Optional[Dict[str, Any]]:
    init_db(db_path)
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM inventory_snapshots WHERE product_id = ? "
            "ORDER BY week_key DESC LIMIT 1",
            (product_id,),
        ).fetchone()
    return _row(row) if row else None


def latest_snapshots(db_path: Any = None) -> List[Dict[str, Any]]:
    """每个商品各自最近一次盘点 —— 盘点卡片的预填就靠它。"""
    init_db(db_path)
    with session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM inventory_snapshots s
            JOIN (SELECT product_id, MAX(week_key) AS wk
                    FROM inventory_snapshots GROUP BY product_id) latest
              ON latest.product_id = s.product_id AND latest.wk = s.week_key
             ORDER BY s.product_id
            """
        ).fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# 销量
# ---------------------------------------------------------------------------

def save_sales(product_id: int, week_key: str, sales_qty: float,
               begin_stock: Optional[float], end_stock: float,
               purchase_qty: float = 0.0, source: str = SOURCE_DERIVED,
               db_path: Any = None) -> None:
    init_db(db_path)
    with session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sales_weekly
                   (product_id, week_key, sales_qty, begin_stock, end_stock,
                    purchase_qty, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id, week_key) DO UPDATE SET
                   sales_qty = excluded.sales_qty,
                   begin_stock = excluded.begin_stock,
                   end_stock = excluded.end_stock,
                   purchase_qty = excluded.purchase_qty,
                   source = excluded.source,
                   created_at = datetime('now')
            """,
            (product_id, week_key, float(sales_qty), begin_stock, float(end_stock),
             float(purchase_qty or 0), source),
        )


def recent_sales(product_id: int, weeks: int = TREND_WINDOW_WEEKS,
                 db_path: Any = None) -> List[float]:
    """最近 N 周销量，由早到晚。给动态阈值用。"""
    init_db(db_path)
    with session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT sales_qty FROM sales_weekly
             WHERE product_id = ?
             ORDER BY week_key DESC LIMIT ?
            """,
            (product_id, weeks),
        ).fetchall()
    return [float(r["sales_qty"]) for r in reversed(rows)]


def latest_sales_source(product_id: int, db_path: Any = None) -> Optional[str]:
    init_db(db_path)
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT source FROM sales_weekly WHERE product_id = ? "
            "ORDER BY week_key DESC LIMIT 1",
            (product_id,),
        ).fetchone()
    return row["source"] if row else None


# ---------------------------------------------------------------------------
# 盘点提交：整个产品的主线闭环
# ---------------------------------------------------------------------------

def commit_stocktake(rows: Sequence[Dict[str, Any]],
                     week_key: Optional[str] = None,
                     db_path: Any = None) -> Dict[str, Any]:
    """提交一次盘点。

    rows 每项：{product_id, stock, purchase_qty?, note?}
    一次事务做完：写快照 → 推导销量 → 更新 products.current_stock → 有进货就刷新进货日
    返回摘要给对话流回写（"可乐 12→10，进 3 件…"）和需要追问的疑点。
    """
    week = week_key or week_key_of()
    init_db(db_path)

    changes: List[Dict[str, Any]] = []
    warnings: List[str] = []

    with session(db_path) as conn:
        for item in rows:
            product_id = item.get("product_id")
            if product_id is None:
                continue
            product = conn.execute(
                "SELECT * FROM products WHERE product_id = ?", (product_id,)
            ).fetchone()
            if product is None:
                warnings.append("有一行商品已经不在档案里，跳过了")
                continue

            end_stock = float(item.get("stock") or 0)
            purchase = float(item.get("purchase_qty") or 0)
            begin = end_stock - purchase

            previous = conn.execute(
                "SELECT * FROM inventory_snapshots WHERE product_id = ? AND week_key < ? "
                "ORDER BY week_key DESC LIMIT 1",
                (product_id, week),
            ).fetchone()

            source = SOURCE_DERIVED
            negative: Optional[float] = None
            if previous is not None:
                begin = float(previous["stock"])
                sales = begin + purchase - end_stock
                if sales < 0:
                    # 库存不降反涨：多半漏报进货。销量记 0 别污染均值，负数另记一份。
                    negative = sales
                    sales = 0.0
            elif product["estimated_daily_sales"]:
                # 冷启动：没盘过，用估算日均推一周当上期库存
                begin = round(float(product["estimated_daily_sales"]) * 7, 2)
                sales = begin + purchase - end_stock
                source = SOURCE_ESTIMATED
                if sales < 0:
                    negative = sales
                    sales = 0.0
            else:
                # 既无上期又无估算 → 判不出，不写销量（PRD：宁可说判不出）
                warnings.append(
                    f"{product['name']}：第一次盘点，没有历史也没有估算日均，"
                    f"这条暂时算不出销量"
                )
                begin = None
                sales = None

            conn.execute(
                """
                INSERT INTO inventory_snapshots
                       (week_key, product_id, stock, purchase_qty, source, note)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, week_key) DO UPDATE SET
                       stock = excluded.stock,
                       purchase_qty = excluded.purchase_qty,
                       source = excluded.source,
                       note = excluded.note,
                       created_at = datetime('now')
                """,
                (week, product_id, end_stock, purchase, SOURCE_MANUAL,
                 item.get("note")),
            )

            if sales is not None:
                conn.execute(
                    """
                    INSERT INTO sales_weekly
                           (product_id, week_key, sales_qty, begin_stock, end_stock,
                            purchase_qty, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(product_id, week_key) DO UPDATE SET
                           sales_qty = excluded.sales_qty,
                           begin_stock = excluded.begin_stock,
                           end_stock = excluded.end_stock,
                           purchase_qty = excluded.purchase_qty,
                           source = excluded.source,
                           created_at = datetime('now')
                    """,
                    (product_id, week, round(sales, 2), begin, end_stock, purchase, source),
                )

            conn.execute(
                "UPDATE products SET current_stock = ?, updated_at = datetime('now') "
                "WHERE product_id = ?",
                (end_stock, product_id),
            )
            if purchase > 0:
                conn.execute(
                    "UPDATE products SET batch_date = date('now') WHERE product_id = ?",
                    (product_id,),
                )

            if negative is not None:
                warnings.append(
                    f"{product['name']}：算出来这周销量是负数（库存不降反涨），"
                    f"「本周进货」那栏是不是漏填了？"
                )
            changes.append({
                "product_id": product_id,
                "name": product["name"],
                "unit": product["unit"] or "件",
                "begin_stock": begin,
                "end_stock": end_stock,
                "purchase_qty": purchase,
                "sales_qty": sales,
                "source": source,
                "negative_sales": negative,
            })

    return {"week_key": week, "changes": changes, "warnings": warnings}


# ---------------------------------------------------------------------------
# 事件（促销 / 清仓 / 补货）
# ---------------------------------------------------------------------------

def add_event(product_id: Optional[int], event_type: str, content: str,
              week_key: Optional[str] = None, db_path: Any = None) -> None:
    init_db(db_path)
    with session(db_path) as conn:
        conn.execute(
            "INSERT INTO product_events (product_id, week_key, event_type, content) "
            "VALUES (?, ?, ?, ?)",
            (product_id, week_key or week_key_of(), event_type, content),
        )


def list_events(week_key: str, product_id: Optional[int] = None,
                db_path: Any = None) -> List[Dict[str, Any]]:
    """本周（或往后还没到的）事件。活动备货就靠这个抬建议量。"""
    init_db(db_path)
    if product_id is None:
        with session(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM product_events WHERE week_key >= ? ORDER BY id",
                (week_key,),
            ).fetchall()
    else:
        with session(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM product_events WHERE week_key >= ? AND product_id = ? "
                "ORDER BY id",
                (week_key, product_id),
            ).fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# 对话历史
# ---------------------------------------------------------------------------

def ensure_conversation(conv_id: Optional[str] = None, db_path: Any = None) -> str:
    init_db(db_path)
    key = conv_id or str(uuid.uuid4())
    with session(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversations (conv_id) VALUES (?)", (key,)
        )
    return key


def add_message(conv_id: str, role: str, content: str,
                payload: Optional[Dict[str, Any]] = None, db_path: Any = None) -> None:
    init_db(db_path)
    with session(db_path) as conn:
        conn.execute(
            "INSERT INTO messages (conv_id, role, content, payload_json) VALUES (?, ?, ?, ?)",
            (conv_id, role, content, dumps(payload) if payload else None),
        )


def load_messages(conv_id: str, limit: int = 20, db_path: Any = None) -> List[Dict[str, Any]]:
    """取最近 limit 条，按时间正序返回给模型当上下文。"""
    init_db(db_path)
    with session(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conv_id = ? ORDER BY id DESC LIMIT ?",
            (conv_id, limit),
        ).fetchall()
    messages = []
    for row in reversed(rows):
        item = _row(row)
        item["payload"] = loads(item.get("payload_json"))
        messages.append(item)
    return messages


def has_products(db_path: Any = None) -> bool:
    return bool(list_products(db_path=db_path))