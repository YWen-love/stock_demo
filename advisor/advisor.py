"""顾问编排：把一句话变成一条可以直接说给店主听的建议。

一轮对话走五步：

    意图路由(确定性) → 取数(repository) → 规则决策(rules.py)
        → 讲人话(llm) → 校验(判定闸 + 文本闸)

**意图路由是确定性关键词匹配，不交给模型。**
看起来像是在为难模型，其实相反：意图判断错一次，后面取数就全错，
而模型判断意图的准确率并不比关键词高，还多一个说不清的失败点。
真正该让模型干的只有最后一步——把结论讲成人话。

每一步的产出都能单独验证。模型挂了，第 5 步退回第 3 步的结论，
对话照样能用；这是演示时最要紧的稳定性。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import onboarding, rag, repository as repo
from .config import SYSTEM_PROMPT, TREND_WINDOW_WEEKS
from .llm import AgentResult, run_agent, strip_reasoning
from .rules import (
    GRADE_STOCK,
    ProductAdvice,
    advise,
    enforce_grades,
    name_matches,
    render_advice_line,
    week_key_of,
)
from .tools import ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 意图
# ---------------------------------------------------------------------------

INTENT_STOCKTAKE = "stocktake"
INTENT_STALE = "stale"
INTENT_EXPIRY = "expiry"
INTENT_REPLENISH = "replenish"
INTENT_CHITCHAT = "chitchat"

_STOCKTAKE_WORDS = ("盘点", "盘货", "数货", "数一下", "清点", "对一下数", "对账", "还剩多少")
_STALE_WORDS = ("卖不动", "滞销", "清仓", "压货", "不动货", "不好卖", "囤了")
_EXPIRY_WORDS = ("过期", "临期", "保质期", "快到期", "变质", "生产日期")
_REPLENISH_WORDS = ("进多少", "补多少", "补货", "进点", "要进", "订多少", "拿货")


@dataclass
class AdvisorReply:
    text: str
    ok: bool = True
    intent: str = INTENT_REPLENISH
    advices: List[ProductAdvice] = field(default_factory=list)
    used_tools: List[str] = field(default_factory=list)
    fixes: List[Dict[str, str]] = field(default_factory=list)
    retried: bool = False
    fallback: bool = False
    open_card: bool = False

    def as_payload(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "text": self.text, "intent": self.intent,
            "advices": [a.to_grounding() for a in self.advices],
            "used_tools": self.used_tools, "fixes": self.fixes,
            "retried": self.retried, "fallback": self.fallback,
            "open_card": self.open_card,
        }


def detect_intent(text: str) -> str:
    """关键词优先：谁先出现算谁。"""
    content = text or ""
    for keywords, intent in (
        (_STOCKTAKE_WORDS, INTENT_STOCKTAKE),
        (_STALE_WORDS, INTENT_STALE),
        (_EXPIRY_WORDS, INTENT_EXPIRY),
        (_REPLENISH_WORDS, INTENT_REPLENISH),
    ):
        if any(word in content for word in keywords):
            return intent
    return INTENT_REPLENISH


def _mentioned_products(text: str, products: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """认出句子里提到了哪些商品。认不出就返回空列表，交给默认逻辑。"""
    return [p for p in products if name_matches(str(p.get("name") or ""), text)]


# ---------------------------------------------------------------------------
# 顾问
# ---------------------------------------------------------------------------

class InventoryAdvisor:
    def __init__(self, db_path: Any = None, client: Optional[Any] = None,
                 conv_id: Optional[str] = None):
        self.db_path = db_path
        self.client = client
        self.conv_id = repo.ensure_conversation(conv_id, db_path=db_path)

    # ---------------- 取数 + 规则决策 ----------------

    def build_advice(self, week_key: Optional[str] = None) -> List[ProductAdvice]:
        """对全店每个商品跑一次规则。这是唯一出结论的地方。"""
        week = week_key or week_key_of()
        events = repo.list_events(week, db_path=self.db_path)
        events_by_product: Dict[Optional[int], List[Dict[str, Any]]] = {}
        for event in events:
            events_by_product.setdefault(event.get("product_id"), []).append(event)

        results: List[ProductAdvice] = []
        for product in repo.list_products(db_path=self.db_path):
            product_id = product["product_id"]
            results.append(advise(
                product_id=product_id,
                name=product["name"],
                current_stock=float(product["current_stock"] or 0),
                unit=product["unit"] or "件",
                sales=repo.recent_sales(product_id, TREND_WINDOW_WEEKS,
                                        db_path=self.db_path),
                estimated_daily_sales=product.get("estimated_daily_sales"),
                sales_source=repo.latest_sales_source(product_id, db_path=self.db_path),
                events=events_by_product.get(product_id, []),
                shelf_life_days=product.get("shelf_life_days"),
                batch_date=product.get("batch_date"),
            ))
        return results

    def _focus(self, text: str, advices: List[ProductAdvice]) -> List[ProductAdvice]:
        """按意图收窄到相关商品，让 grounding 别塞一整店。"""
        intent = detect_intent(text)
        products = repo.list_products(db_path=self.db_path)
        mentioned = {p["product_id"] for p in _mentioned_products(text, products)}

        if mentioned:
            return [a for a in advices if a.product_id in mentioned]

        if intent == INTENT_STALE:
            subset = [a for a in advices if a.stale]
        elif intent == INTENT_EXPIRY:
            subset = [a for a in advices if a.expiry_warning]
        elif intent == INTENT_REPLENISH:
            subset = [a for a in advices if a.needs_replenish or a.stale]
        else:
            subset = list(advices)

        # 一个都没命中就退回全店：总比答"没找到相关商品"强。
        return subset or list(advices)

    # ---------------- 对话 ----------------

    def reply(self, user_text: str, history: Optional[List[Dict[str, Any]]] = None) -> AdvisorReply:
        user_text = (user_text or "").strip()
        if not user_text:
            return AdvisorReply(text="你想问什么？", ok=False)

        if onboarding.needs_onboarding(db_path=self.db_path):
            ctx = ToolContext(db_path=self.db_path)
            result = onboarding.run_onboarding(user_text, ctx, client=self.client)
            self._log("user", user_text)
            self._log("assistant", result.text)
            return AdvisorReply(text=result.text, ok=result.ok,
                                intent="onboarding", retried=result.retried)

        intent = detect_intent(user_text)

        if intent == INTENT_STOCKTAKE:
            return self._stocktake_reply(user_text)

        advices = self.build_advice()
        focus = self._focus(user_text, advices)

        # 工具上下文里带缓存：模型调 get_advice 时直接拿到 rules 算好的结论，
        # 不给它重算的机会（重算就是产生第二个判定点的开始）。
        ctx = ToolContext(db_path=self.db_path,
                          advice_cache={a.name: a for a in advices})

        grounding = {
            "周次": ctx.week_key,
            "商品数": len(focus),
            "规则结论": [a.to_grounding() for a in focus],
        }

        prompt = self._build_prompt(user_text, grounding, history or [])
        result = run_agent(prompt, ctx, client=self.client)

        if not result.ok:
            return self._fallback_reply(user_text, intent, focus, result)

        return self._finalize(user_text, intent, focus, result)

    def _build_prompt(self, user_text: str, grounding: Dict[str, Any],
                      history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for item in history[-8:]:
            role = item.get("role")
            if role in ("user", "assistant") and item.get("content"):
                messages.append({"role": role, "content": str(item["content"])[:800]})
        messages.append({
            "role": "user",
            "content": (
                f"<数据>\n{json.dumps(grounding, ensure_ascii=False, indent=2)}\n</数据>\n\n"
                f"店主问：{user_text}\n\n"
                "请照 <数据> 里的规则结论回答，用一两句话。"
                "要动数字就调 math_calc，要下结论就调 submit_advice。"
            ),
        })
        return messages

    def _finalize(self, user_text: str, intent: str, focus: List[ProductAdvice],
                  result: AgentResult) -> AdvisorReply:
        """判定闸 + 文本闸：模型的话可以留，分级和数字不许留。"""
        graded = enforce_grades(result.text, focus)
        text = graded["text"]
        fixes = graded["fixes"]

        if fixes:
            logger.info("判定闸改写分级：%s", fixes)
            text += ("\n\n（补一句更正：上面的分级已按规则口径校正过，"
                     "以标注的为准。）")

        if intent == INTENT_STALE and not any(a.stale for a in focus):
            text += "\n\n你这周盘出来的数据里，暂时没有明显卖不动的货。"

        self._log("user", user_text)
        self._log("assistant", text)

        return AdvisorReply(text=text, ok=True, intent=intent, advices=focus,
                            used_tools=result.used_tools, fixes=fixes,
                            retried=result.retried)

    def _fallback_reply(self, user_text: str, intent: str, focus: List[ProductAdvice],
                        result: AgentResult) -> AdvisorReply:
        """兜底闸：模型不可用，直接把 rules 的结论讲成人话。

        这不是"凑合的降级"——数字和分级本来就是 rules 算的，模型不在场反而更稳。
        缺的只是"为什么"那部分流畅度。
        """
        if not focus:
            text = "店里的商品档案还是空的，先聊两样你在卖的货，我给你建个档。"
        else:
            lines = [render_advice_line(a) for a in focus]
            alerts = [alert for a in focus for alert in a.alerts]
            text = "\n".join(lines)
            if alerts:
                unique = list(dict.fromkeys(alerts))
                text += "\n\n另外有几件事要提一下：\n" + "\n".join(
                    f"· {item}" for item in unique[:4]
                )
        text += "\n\n（模型这会儿没接上，上面是纯规则算出来的，数字一样准。）"

        self._log("user", user_text)
        self._log("assistant", text)

        return AdvisorReply(text=text, ok=True, intent=intent, advices=focus,
                            fallback=True)

    def _stocktake_reply(self, user_text: str) -> AdvisorReply:
        rows = self.stocktake_rows()
        names = "、".join(r["商品"] for r in rows[:6]) or "（还没有商品档案）"
        text = (f"行，盘点卡片给你打开了。\n\n"
                f"卡片里给你列了 {len(rows)} 个商品：{names}。"
                f"「本次库存」已经按上次数填好了，**只改跟上次不一样的那几格**，"
                f"没动的数字不用碰。\n\n"
                f"这周要是进了货，「本周进货」那一格记得填，不然算出来的销量会偏大。")
        self._log("user", user_text)
        self._log("assistant", text)
        return AdvisorReply(text=text, ok=True, intent=INTENT_STOCKTAKE,
                            open_card=True)

    # ---------------- 盘点卡片 ----------------

    def stocktake_rows(self) -> List[Dict[str, Any]]:
        """卡片预填：本次库存 = 上次盘点。

        店主一周只盘一次，绝大多数格子跟上次数一样。预填成上次数，
        他就只需要改变化的那几格 —— 这是这个交互能落地的前提。
        """
        latest = {s["product_id"]: s for s in repo.latest_snapshots(db_path=self.db_path)}
        rows = []
        for product in repo.list_products(db_path=self.db_path):
            previous = latest.get(product["product_id"])
            rows.append({
                "product_id": product["product_id"],
                "商品": product["name"],
                "单位": product["unit"] or "件",
                "上次盘点": float(previous["stock"]) if previous else 0.0,
                "本次库存": float(previous["stock"]) if previous
                else float(product["current_stock"] or 0),
                "本周进货": 0.0,
                "备注": "",
            })
        return rows

    def prefill_by_average(self) -> List[Dict[str, Any]]:
        """「按近4周均值预填」：本次库存 = 上次盘点 − 周均销量。

        是个便利按钮，不是必需项。店主上周没盘但记得卖得差不多了，点一下就有个起点，
        再往回改。比让人对着一堆空格子发愁强。
        """
        rows = self.stocktake_rows()
        advices = {a.name: a for a in self.build_advice()}
        for row in rows:
            advice = advices.get(row["商品"])
            if not advice or advice.avg_weekly_sales <= 0:
                continue
            row["本次库存"] = round(max(0.0, row["上次盘点"] - advice.avg_weekly_sales), 2)
        return rows

    def submit_stocktake(self, rows: Sequence[Dict[str, Any]],
                         week_key: Optional[str] = None) -> AdvisorReply:
        """提交盘点 → 写库 → 收起卡片 → 摘要回写对话 → 给新建议。"""
        result = repo.commit_stocktake(rows, week_key or week_key_of(),
                                       db_path=self.db_path)

        lines = [f"{c['name']} {c['begin_stock'] if c['begin_stock'] is not None else '?'}"
                 f"→{c['end_stock']:g}"
                 + (f"（进 {c['purchase_qty']:g}，卖 {c['sales_qty']:g}）"
                    if c["sales_qty"] is not None else "")
                 for c in result["changes"]]
        text = f"本周盘点记好了（{result['week_key']}）：\n" + "、".join(lines) + "。"

        if result["warnings"]:
            text += "\n\n有两件事要跟你对一下：\n" + "\n".join(
                f"· {w}" for w in result["warnings"][:4]
            )

        advices = self.build_advice()
        needs = [a for a in advices if a.needs_replenish or a.stale or a.expiry_warning]
        if needs:
            text += "\n\n盘点完就能算了 —— 这周要补的有：\n"
            text += "\n".join(f"· {render_advice_line(a)}" for a in needs[:5])

        self._log("assistant", text)
        return AdvisorReply(text=text, ok=True, intent=INTENT_STOCKTAKE,
                            advices=needs, open_card=False)

    # ---------------- 规则原文 ----------------

    def rules_text(self) -> str:
        """规则全文，塞进 system prompt。检索那套留给「问法不确定」的情况。"""
        from .config import RULE_DOC, VECTOR_DIR

        try:
            rag.ensure_index(RULE_DOC.parent, VECTOR_DIR)
        except rag.RuleDocumentMissing as exc:
            logger.warning("规则文档不可用：%s", exc)
            raise
        return rag.rule_document_text(RULE_DOC.parent)

    # ---------------- 历史 ----------------

    def _log(self, role: str, content: str) -> None:
        if not content:
            return
        repo.add_message(self.conv_id, role, content, db_path=self.db_path)

    def history(self, limit: int = 10) -> List[Dict[str, Any]]:
        return repo.load_messages(self.conv_id, limit, db_path=self.db_path)

    def reset(self) -> None:
        """清空档案重来（演示第 5 步用）。"""
        from .db import reset_db

        reset_db(self.db_path)
        self.conv_id = repo.ensure_conversation(None, db_path=self.db_path)