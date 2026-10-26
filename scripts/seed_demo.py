"""造 3 周演示数据。

刻意埋了四种典型情况，好让每种建议都能当场演示出来：

  可乐      销量上升、库存压线偏低 + 本周促销 → 紧急补货（活动备货上浮）
  薯片      销量稳定、库存充足                → 不用进（"不打扰"也是功能）
  老式蛋糕  连续三周几乎没动 + 已过期          → 滞销告警
  酸奶      正常动销但快到期                  → 过期告警
  洗衣液    普通需要补货                      → 常规档

每件商品显式写死「每周实际卖多少」，库存阶梯由进货量倒推，
而不是反过来 —— 反过来推的话滞销品会被算成正常动销，演示当场穿帮。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from advisor import repository as repo  # noqa: E402
from advisor.config import EVENT_PROMO  # noqa: E402
from advisor.db import init_db, reset_db  # noqa: E402
from advisor.rules import previous_week_key, week_key_of  # noqa: E402

# (商品名, 单位, 三周库存阶梯[由早到晚], 三周销量[由早到晚],
#  最早一周的期初库存, 估算日均, 保质期天数, 最近进货日距今天数)
PRODUCTS = [
    ("可口可乐", "瓶", [30, 35, 40, 12], [44, 50, 57], 26, 7.0, 180, 20),
    ("薯片", "袋", [30, 33, 36, 40], [26, 28, 27], 28, 4.0, 90, 25),
    ("老式蛋糕", "个", [25, 24, 23, 22], [0.2, 0.1, 0.05], 25, 0.03, 30, 90),
    ("酸奶", "杯", [20, 16, 13, 10], [15, 14, 13], 20, 3.0, 21, 14),
    ("洗衣液", "瓶", [12, 11, 9, 8], [11, 10, 9], 13, 1.5, 1095, 60),
]


def seed(db_path=None, weeks: int = 3) -> None:
    """写入 weeks 周历史。重跑会先清库，保证演示可重复。"""
    reset_db(db_path)
    init_db(db_path)

    this_week = week_key_of()
    week_keys = [previous_week_key(this_week, offset) for offset in range(weeks, 0, -1)]

    for name, unit, stocks, sales, begin, daily, shelf_life, batch_offset in PRODUCTS:
        batch_date = (date.today() - timedelta(days=batch_offset)).isoformat()
        current = stocks[-1]

        product = repo.upsert_product(
            name, stock=current, unit=unit, estimated_daily_sales=daily,
            shelf_life_days=shelf_life, batch_date=batch_date, db_path=db_path,
        )
        pid = product["product_id"]

        # 进货量倒推：进多少 = 期初 + 卖掉的 - 剩下的。
        # 每格库存和销量都是写死的，倒推出的进货量才是自洽的。
        open_stock = begin
        for index, week in enumerate(week_keys):
            sold = sales[index]
            purchase = round(open_stock + sold - stocks[index], 2)
            repo.save_snapshot(pid, week, stocks[index], purchase, "manual",
                               db_path=db_path)
            repo.save_sales(pid, week, sold, open_stock, stocks[index], purchase,
                            "derived", db_path=db_path)
            open_stock = stocks[index]

        # 本周：只录了库存、进货和销量留给演示时点卡片提交
        repo.save_snapshot(pid, this_week, current, 0, "manual", db_path=db_path)

    coke = repo.get_product_by_name("可口可乐", db_path=db_path)
    if coke:
        repo.add_event(coke["product_id"], EVENT_PROMO, "下周三门口搞促销",
                       this_week, db_path=db_path)

    print(f"已写入 {len(PRODUCTS)} 个商品 × {weeks} 周历史 → {db_path or '默认路径'}")
    print(f"周次：{'、'.join(week_keys)}（本周 {this_week} 等你点卡片提交）")
    print("下一步：python main.py")


if __name__ == "__main__":
    seed()