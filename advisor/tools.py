"""工具定义与执行。

分三类，边界很硬：
- **算术**：math_calc 白名单 eval。模型要算数必须走它，走完有返回可比对。
- **读库**：query_products / get_advice。模型看到的是 rules.py 算好的结论，不是原始行。
- **写库**：record_event / upsert_product。只有这两条路能改数据，
  杜绝模型"顺手把库存改了"这种越界。

submit_advice 是判定闸的入口：模型必须在这里报出它自己的结论，
代码拿 rules.py 的口径覆盖后回给它。模型嘴上说的和它真算的，对不上就看得见了。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import repository as repo
from .config import GRADES
from .db import dumps
from .rules import ProductAdvice, name_matches, week_key_of

# ---------------------------------------------------------------------------
# math_calc：白名单 eval
# ---------------------------------------------------------------------------

_ALLOWED_CHARS = re.compile(r"^[0-9\.\+\-\*/\(\)\s]+$")


def math_calc(expression: str) -> Dict[str, Any]:
    """只允许数字和四则运算。

    不做 AST 校验是偷懒，但白名单字符集已经掐死了函数名、变量名、下标和属性访问，
    剩下的就是纯算术表达式。这是全项目唯一的 eval，注释里写清楚为什么它安全。
    """
    text = (expression or "").strip()
    if not text:
        return {"ok": False, "error": "算式不能为空"}
    if not _ALLOWED_CHARS.match(text):
        return {"ok": False, "error": f"算式里含有不允许的字符：{text}"}
    try:
        value = eval(text, {"__builtins__": {}}, {})  # noqa: S307 - 字符白名单已限制
    except (ArithmeticError, SyntaxError, ValueError, TypeError) as exc:
        return {"ok": False, "error": f"算式无法计算：{exc}"}
    if isinstance(value, complex) or not isinstance(value, (int, float)):
        return {"ok": False, "error": "算式结果不是数字"}
    return {"ok": True, "expression": text, "result": round(float(value), 2)}


# ---------------------------------------------------------------------------
# 工具 schema
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "math_calc",
            "description": "计算一个算式并返回结果。只支持数字和 + - * / ( )。"
                           "凡是涉及数量、库存、天数、价格的地方都必须用它算，不要自己心算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "算式，例如 (42*1.2) - 12"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_products",
            "description": "查询店里有哪些商品、各自当前库存。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "商品名列表；留空表示查全部",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_advice",
            "description": "取某个商品按规则算好的补货结论（安全库存线、补货量、库存状态）。"
                           "这是唯一权威口径，你要的数字都在这里，不要自己推。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "商品名"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_advice",
            "description": "报出你对某个商品的结论。系统会用规则口径核对，你的分级和补货量"
                           "只作为留档比对，最终以返回结果为准。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "grade": {"type": "string", "enum": list(GRADES)},
                    "replenish_qty": {"type": "number"},
                    "reason": {"type": "string", "description": "给店主的理由，一句话"},
                },
                "required": ["name", "grade", "replenish_qty", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_event",
            "description": "记录活动/促销/清仓。店主说要搞活动、要清货时调用，下次建议会据此调整。",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string", "description": "商品名；留空表示整店活动"},
                    "event_type": {
                        "type": "string",
                        "enum": ["promo", "clearance", "restock"],
                        "description": "promo=促销备货, clearance=清仓, restock=到货",
                    },
                    "content": {"type": "string", "description": "原话记录，如「下周三门口搞促销」"},
                    "week_key": {"type": "string", "description": "ISO周键，如 2026-W43；留空取本周"},
                },
                "required": ["event_type", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_product",
            "description": "新建或更新商品档案。冷启动建档、或店主补充信息时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "stock": {"type": "number", "description": "当前库存"},
                    "unit": {"type": "string", "description": "单位，默认「件」"},
                    "estimated_daily_sales": {"type": "number", "description": "大概每天卖多少；没有就留空"},
                    "shelf_life_days": {"type": "integer", "description": "保质期天数；没有就留空"},
                    "batch_date": {"type": "string", "description": "最近进货日 YYYY-MM-DD"},
                },
                "required": ["name", "stock"],
            },
        },
    },
]


@dataclass
class ToolContext:
    """工具执行上下文。只放真正需要的，db_path 让测试能用临时库。"""

    db_path: Any = None
    week_key: str = field(default_factory=week_key_of)
    #: advisor 预取好的 grounding，get_advice 直接查这里而不是自己重算
    advice_cache: Dict[str, ProductAdvice] = field(default_factory=dict)
    #: submit_advice 的留档结果
    submissions: List[Dict[str, Any]] = field(default_factory=list)


def _lookup_advice(name: str, ctx: ToolContext) -> Optional[ProductAdvice]:
    key = (name or "").strip()
    if not key:
        return None
    if key in ctx.advice_cache:
        return ctx.advice_cache[key]
    # 简称也得认（"可乐" → "可口可乐"），但必须走 rules 那套匹配：
    # 它有最短长度限制，不会让「水」匹配上「矿泉水」。认错商品等于把另一个
    # 商品的分级和补货量交给模型，性质跟算错分级一样严重。
    for cached_name, advice in ctx.advice_cache.items():
        if name_matches(cached_name, key):
            return advice
    return None


def execute_tool(name: str, arguments: Dict[str, Any],
                 ctx: ToolContext) -> str:
    """执行一个工具，返回给模型看的 JSON 字符串。

    工具出错不抛异常 —— 返回 {"ok": false, "error": ...} 让模型自己看到并改。
    抛出去会中断整轮对话，而模型其实有能力看到错误然后纠正。
    """
    from . import rules as rules_module  # 局部导入：rules 不该反向依赖 tools

    try:
        args = arguments or {}
        if name == "math_calc":
            return dumps(math_calc(args.get("expression", "")))

        if name == "query_products":
            names = [str(n).strip() for n in (args.get("names") or []) if str(n).strip()]
            products = repo.list_products(db_path=ctx.db_path)
            if names:
                products = [p for p in products
                            if any(n in p["name"] or p["name"] in n for n in names)]
            return dumps({
                "ok": True,
                "商品": [
                    {"商品名": p["name"], "当前库存": p["current_stock"], "单位": p["unit"]}
                    for p in products
                ],
            })

        if name == "get_advice":
            advice = _lookup_advice(args.get("name", ""), ctx)
            if advice is None:
                return dumps({"ok": False, "error": "没找到这个商品，先用 query_products 查一下"})
            return dumps({"ok": True, "规则结论": advice.to_grounding()})

        if name == "submit_advice":
            name_value = str(args.get("name") or "").strip()
            advice = _lookup_advice(name_value, ctx)
            claim = {
                "name": name_value,
                "grade": str(args.get("grade") or "").strip(),
                "replenish_qty": args.get("replenish_qty"),
                "reason": str(args.get("reason") or "").strip(),
            }
            if advice is None:
                ctx.submissions.append({**claim, "final": None})
                return dumps({"ok": False, "error": "没找到这个商品，无法核对"})
            verdict = rules_module.reconcile(advice, claim)
            ctx.submissions.append({**claim, "final": verdict["final"],
                                    "disagreement": verdict["disagreement"]})
            payload = {"ok": True, "最终口径": verdict["final"]}
            if verdict["disagreement"]:
                payload["说明"] = (
                    f"你报的「{verdict['disagreement']['model']}」与规则口径不符，"
                    f"已按规则改为「{verdict['disagreement']['rules']}」，请照这个说。"
                )
            return dumps(payload)

        if name == "record_event":
            product_name = str(args.get("product_name") or "").strip()
            product_id = None
            if product_name:
                product = repo.get_product_by_name(product_name, db_path=ctx.db_path)
                if product is None:
                    return dumps({"ok": False, "error": f"没找到商品「{product_name}」"})
                product_id = product["product_id"]
            repo.add_event(product_id, str(args.get("event_type") or "restock"),
                           str(args.get("content") or ""),
                           args.get("week_key") or ctx.week_key, db_path=ctx.db_path)
            return dumps({"ok": True, "已记录": args.get("content")})

        if name == "upsert_product":
            product = repo.upsert_product(
                str(args.get("name") or ""),
                float(args.get("stock") or 0),
                unit=str(args.get("unit") or "件"),
                estimated_daily_sales=args.get("estimated_daily_sales"),
                shelf_life_days=args.get("shelf_life_days"),
                batch_date=args.get("batch_date"),
                db_path=ctx.db_path,
            )
            return dumps({"ok": True, "商品": product["name"],
                          "商品ID": product["product_id"]})

        return dumps({"ok": False, "error": f"未知工具：{name}"})
    except Exception as exc:  # noqa: BLE001 - 工具边界必须兜住，否则整轮对话挂掉
        return dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def tool_names() -> List[str]:
    return [schema["function"]["name"] for schema in TOOL_SCHEMAS]


def lookup_tool(name: str) -> Optional[Callable]:
    return {
        "math_calc": math_calc,
    }.get(name)