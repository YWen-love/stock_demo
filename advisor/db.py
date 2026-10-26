"""SQLite 建表与连接。

为什么必须落库：LLM 无状态。对话结束模型就忘了，明天店主再来问「可乐该进多少」，
它答不上来——除非这些数字存在我们自己这边。所以整个产品只有一条主线：**数据是我们的事实，
模型只是读事实的人。**

表结构见 README 的「数据库表定义」。迁移策略沿用项目既有写法：
CREATE TABLE IF NOT EXISTS 建表 + 幂等 ALTER TABLE 补列，老库不删不重建。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DB_PATH

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS products (
        product_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name               TEXT NOT NULL UNIQUE,
        unit               TEXT DEFAULT '件',
        current_stock      REAL NOT NULL DEFAULT 0,
        estimated_daily_sales REAL,
        shelf_life_days    INTEGER,
        batch_date         TEXT,
        note               TEXT,
        status             TEXT DEFAULT 'active',
        created_at         TEXT DEFAULT (datetime('now')),
        updated_at         TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_snapshots (
        snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        week_key     TEXT NOT NULL,
        product_id   INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
        stock        REAL NOT NULL,
        purchase_qty REAL DEFAULT 0,
        source       TEXT DEFAULT 'manual',
        note         TEXT,
        created_at   TEXT DEFAULT (datetime('now')),
        UNIQUE(product_id, week_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sales_weekly (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id   INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
        week_key     TEXT NOT NULL,
        sales_qty    REAL NOT NULL,
        begin_stock  REAL,
        end_stock    REAL,
        purchase_qty REAL DEFAULT 0,
        source       TEXT DEFAULT 'derived',
        created_at   TEXT DEFAULT (datetime('now')),
        UNIQUE(product_id, week_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER REFERENCES products(product_id) ON DELETE CASCADE,
        week_key   TEXT,
        event_type TEXT,
        content    TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        conv_id    TEXT PRIMARY KEY,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id      TEXT NOT NULL REFERENCES conversations(conv_id) ON DELETE CASCADE,
        role         TEXT NOT NULL,
        content      TEXT,
        payload_json TEXT,
        created_at   TEXT DEFAULT (datetime('now'))
    )
    """,
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_snapshots_week ON inventory_snapshots(week_key)",
    "CREATE INDEX IF NOT EXISTS idx_sales_product ON sales_weekly(product_id, week_key)",
    "CREATE INDEX IF NOT EXISTS idx_events_product ON product_events(product_id, week_key)",
    "CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, id)",
]

#: (表, 列, SQL 类型) —— _ensure_columns 按这个清单幂等补列。
#: 加列时只管往这里追加，不要改已有条目。
_MIGRATIONS = {
    "products": [
        ("estimated_daily_sales", "REAL"),
        ("shelf_life_days", "INTEGER"),
        ("batch_date", "TEXT"),
        ("note", "TEXT"),
        ("status", "TEXT DEFAULT 'active'"),
        ("unit", "TEXT DEFAULT '件'"),
    ],
    "inventory_snapshots": [
        ("purchase_qty", "REAL DEFAULT 0"),
        ("source", "TEXT DEFAULT 'manual'"),
        ("note", "TEXT"),
    ],
    "sales_weekly": [
        ("begin_stock", "REAL"),
        ("end_stock", "REAL"),
        ("purchase_qty", "REAL DEFAULT 0"),
        ("source", "TEXT DEFAULT 'derived'"),
    ],
}


def connect(db_path: Any = None) -> sqlite3.Connection:
    """开一个连接。测试传临时路径即可，不必改全局 DB_PATH。"""
    target = str(db_path or DB_PATH)
    if target != ":memory:":
        from pathlib import Path

        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session(db_path: Any = None) -> Iterator[sqlite3.Connection]:
    """事务边界：正常提交，异常回滚。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def init_db(db_path: Any = None) -> None:
    """建表 + 幂等补列 + 建索引。可重复调用。"""
    with session(db_path) as conn:
        for statement in SCHEMA:
            conn.execute(statement)
        _ensure_columns(conn)
        for statement in _INDEXES:
            conn.execute(statement)


def reset_db(db_path: Any = None) -> None:
    """清空全部业务数据，保留表结构。演示脚本「清库重来」用。"""
    init_db(db_path)
    tables = ["messages", "conversations", "product_events",
              "sales_weekly", "inventory_snapshots", "products"]
    with session(db_path) as conn:
        for table in tables:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     f"({','.join('?' * len(tables))})", tables)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: Any, fallback: Any = None) -> Any:
    if value in (None, ""):
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback