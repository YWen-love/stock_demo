"""旧 Excel 流程降级版：上传表格出报告 → CLI 一行命令出报告。

    python scripts/batch_report.py 库存.xlsx
    python scripts/batch_report.py 库存.xlsx --format md

和对话版共用同一套 advisor/rules.py，阈值口径只有一处。
留着是因为总店/加盟商那种"有一堆 Excel 要跑"的场景还在，
但它已经不值得占一个界面入口了。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from advisor.rules import ProductAdvice, advise, grade_of, safety_threshold,  # noqa: E402
    emergency_threshold, replenish_qty

COLUMNS = ["商品id", "商品名称", "当前库存", "近7天销量", "安全库存阈值", "补货建议", "备注"]


def load_stock_excel(path: Path):
    """沿用旧的口径校验：id / 名称不许空，库存和销量必须是数字。

    数据不干净就先拦下来，不带着脏数据往下算 —— 算出来的错建议比没有建议更糟。
    """
    import pandas as pd

    frame = pd.read_excel(path)
    required = ["商品 id", "商品名称", "当前库存", "近 7 天销量"]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"表格缺少必要字段：{missing}")

    blank = frame[required[:2]].isna().any(axis=1)
    if blank.any():
        names = frame.loc[blank, "商品名称"].tolist()
        raise ValueError(f"商品 id / 商品名称存在空值：{names[:5]}")

    numeric_errors = []
    for column in ("当前库存", "近 7 天销量"):
        coerced = pd.to_numeric(frame[column], errors="coerce")
        if coerced.isna().any():
            numeric_errors.append(column)
    if numeric_errors:
        raise ValueError(f"以下字段必须是数字：{'、'.join(numeric_errors)}")

    return frame


def to_advices(frame) -> list[ProductAdvice]:
    advices = []
    for index, (_, row) in enumerate(frame.iterrows(), start=1):
        stock = max(0.0, float(row["当前库存"]))
        sales = max(0.0, float(row["近 7 天销量"]))
        advices.append(advise(
            product_id=int(index),
            name=str(row["商品名称"]),
            current_stock=stock,
            sales=[sales],           # Excel 只有单周数据，滚动均值退化成这一周
            sales_source="manual",
        ))
    return advices


def render_markdown(advices: list[ProductAdvice]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |",
             "| " + " | ".join("---" for _ in COLUMNS) + " |"]
    for advice in advices:
        lines.append("| " + " | ".join([
            str(advice.product_id), advice.name,
            f"{advice.current_stock:g}", f"{advice.avg_weekly_sales:g}",
            f"{advice.safety_threshold:g}", f"{advice.replenish_qty:g}",
            advice.grade,
        ]) + " |")
    return "\n".join(lines)


def render_csv(advices: list[ProductAdvice]) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    for advice in advices:
        writer.writerow([advice.product_id, advice.name, advice.current_stock,
                         advice.avg_weekly_sales, advice.safety_threshold,
                         advice.replenish_qty, advice.grade])
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="批量库存报表（CLI）")
    parser.add_argument("excel", type=Path, help="含 商品id/商品名称/当前库存/近7天销量 的 xlsx")
    parser.add_argument("--format", choices=["md", "csv"], default="md")
    parser.add_argument("--out", type=Path, help="输出文件；不给就打到屏幕")
    args = parser.parse_args()

    try:
        frame = load_stock_excel(args.excel)
    except (ValueError, FileNotFoundError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    advices = to_advices(frame)
    if not advices:
        print("❌ 表格里没有有效数据行", file=sys.stderr)
        return 1

    text = render_markdown(advices) if args.format == "md" else render_csv(advices)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"已写出 {args.out}")
    else:
        print(text)

    urgent = [a for a in advices if a.grade == "紧急补货"]
    if urgent:
        print(f"\n⚠️ {len(urgent)} 个商品紧急补货：" +
              "、".join(a.name for a in urgent), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())