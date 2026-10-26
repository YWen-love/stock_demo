"""冷启动引导：第一周没有历史数据时，用对话把店主的货问出来。

为什么值得单独写一个模块：这是整个产品里唯一"没有表格也要出结论"的场景，
也是最容易让人劝退的地方 —— 一上来让人填 Excel 的工具，店主第二次就不会再打开了。
所以这里的原则是：**一次只问一件事，问完立刻给反馈**，
让建档本身成为对话的一部分，而不是一段表单。

同时遵守 PRD：没数据的时候不许编。引导只采集，不推断。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import repository as repo
from .config import SYSTEM_PROMPT
from .llm import AgentResult, run_agent
from .tools import ToolContext

logger = logging.getLogger(__name__)

GREETING = (
    "你好，我是你店里的补货助手。\n\n"
    "我先搞清楚你卖什么、现在手上还有多少货，之后每周你点一下盘点，"
    "剩下的进货量我来算。\n\n"
    "先从简单的开始：你店里主要卖哪几样？想到几样说几样，不用一次说完。"
)

_NO_DATA_HINT = (
    "现在一共有 {count} 个商品了。下一步我们盘一次货——"
    "点下面的「🧾 本周盘点」，把你手上实际剩的数填进去就行，"
    "和上次一样的一格都不用改。"
)

_INT = re.compile(r"-?\d+(?:\.\d+)?")


def needs_onboarding(db_path: Any = None) -> bool:
    """档案为空 → 走引导。有档案 → 正常对话。"""
    return not repo.has_products(db_path=db_path)


def progress_hint(db_path: Any = None) -> str:
    products = repo.list_products(db_path=db_path)
    return _NO_DATA_HINT.format(count=len(products))


@dataclass
class OnboardingResult:
    ok: bool = True
    text: str = GREETING
    created: List[str] = field(default_factory=list)
    retried: bool = False

    def as_payload(self) -> Dict[str, Any]:
        return {"ok": self.ok, "text": self.text, "created": self.created,
                "retried": self.retried}


_ONBOARDING_PROMPT = SYSTEM_PROMPT + """

【当前阶段：建档】
店里还没有任何商品档案，所以你现在唯一要做的事是**把店主说的商品记下来**：

1. 每店主提到一个商品，就调一次 upsert_product 把名字和他说的大概库存写进去。
2. 店主一次说好几样，就调好几次 —— 一次调用只写一个商品，不要合并。
3. 店主的说法尽量照原样记，别自作主张改名字（「可口可乐 330ml」和「可乐」以后可能都对不上，
   先按他说的记，改名的事等他自己纠正）。
4. 如果他顺便说了「大概一天卖几瓶」，就填进 estimated_daily_sales；他没说就留空，不要替他估。
5. 全部记完之后，回他一句家常话，告诉他记了哪几样、下一步点盘点按钮。不要列表格。

**不要说**「数据不足」「无法计算」这类话 —— 现在还在建档阶段，还没到算的时候。
"""


def run_onboarding(user_text: str, ctx: ToolContext, *,
                   client: Optional[Any] = None) -> OnboardingResult:
    """处理一句建档期的用户输入。

    工具只开放 upsert_product（和 math_calc）：建档阶段不该让模型去查库存或者记活动，
   那些数据还不存在，开放了只会让它编。
    """
    from .tools import TOOL_SCHEMAS

    allowed = [s for s in TOOL_SCHEMAS
               if s["function"]["name"] in ("upsert_product", "math_calc")]
    ctx.submissions.clear()

    result: AgentResult = run_agent(
        [
            {"role": "system", "content": _ONBOARDING_PROMPT},
            {"role": "user", "content": user_text},
        ],
        ctx,
        client=client,
        tools=allowed,
    )

    created = [c for c in (ctx.submissions or [])]
    if not result.ok:
        # 模型没给出可用回复也不能冷场：已经写进去的商品照样汇报，没写进去就问一句
        written = _names_written(ctx)
        if written:
            return OnboardingResult(
                ok=True,
                text=f"记下了：{'、'.join(written)}。还有什么要补的吗？"
                     + progress_hint(ctx.db_path),
                created=written,
                retried=result.retried,
            )
        return OnboardingResult(
            ok=False,
            text="刚才没太听清，你能再说一遍店里主要卖什么吗？"
                 "（比如「可乐、薯片、洗衣液」）",
        )

    return OnboardingResult(ok=True, text=result.text, created=_names_written(ctx),
                            retried=result.retried)


def _names_written(ctx: ToolContext) -> List[str]:
    """从 upsert_product 的执行结果里回捞商品名。

    工具返回值本身不留在 ctx.submissions（那是判定闸的留档位），
    所以这里直接查一次库：建档这一轮写了谁，库里就有谁，最省事也最不会漏。
    """
    names = [p["name"] for p in repo.list_products(db_path=ctx.db_path)]
    return names


def extract_stock_and_daily(text: str) -> Dict[str, Optional[float]]:
    """从一句口语里抠数字：「可乐还有12瓶，一天卖个三五瓶」→ {stock: 12, daily: None}

    只作为降级路径 —— 主路径靠 upsert_product。刻意保守：
    抠不准就返回 None，宁可少填也不填错，错了的库存会一路污染后面所有周的销售推导。
    """
    numbers = _INT.findall(text or "")
    values = [float(n) for n in numbers]
    stock: Optional[float] = None
    daily: Optional[float] = None
    if "一天" in text or "每天" in text:
        for value in values:
            if 0 <= value <= 1000:
                daily = value
                break
    else:
        for value in values:
            if 0 <= value <= 100000:
                stock = value
                break
    return {"stock": stock, "daily": daily}