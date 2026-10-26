"""全局配置：规则参数 + LLM 接入 + 路径。

⚠️ 关于"规则唯一性"
PRD 写的是「边界判定唯一标准：仅以 TXT 规则定义的阈值为准」。这里的做法是：

    TXT（stock_rag/rag_docs/stock_rule.txt）是规则的自然语言权威表述，
    同时喂给 RAG 和用户看；config.py 里这份是同一组系数的**代码落点**，
    rules.py 只读 config，不再另写一份。

两处对不齐就是 bug，所以 tests/test_rules.py 里有一条对拍用例
（test_config_matches_rule_document）逐个系数比对 TXT 里的字面量。
改动任意一侧，另一侧必须同改。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .paths import db_path as _db_path
from .paths import resource_root

# ---------------------------------------------------------------------------
# 规则参数（与 rag_docs/stock_rule.txt 一一对应）
# ---------------------------------------------------------------------------

#: 安全库存系数：常规安全库存阈值 = 滚动周均销量 × SAFETY_FACTOR
SAFETY_FACTOR = 1.2

#: 紧急断货的等效天数：紧急断货阈值 = 滚动周均销量 ÷ 7 × EMERGENCY_DAYS × SAFETY_FACTOR
EMERGENCY_DAYS = 2

#: 动态阈值的滚动窗口周数（需求 8：不看单周，看最近几周均值）
TREND_WINDOW_WEEKS = 4

#: 滞销判定：连续 N 周销量低于周均的这个比例，且绝对值低于「每周不到一件」
STALE_CONSECUTIVE_WEEKS = 3
STALE_RATIO = 0.2
#: 滞销的绝对下限。少了这个，"每周不到一件"这种货（蛋糕、尾货）永远判不出滞销 ——
#: 它们自己的周均也只有零点几，拿周均当尺子量自己，永远量不出"不动"。
STALE_MIN_WEEKLY = 1.0

#: 保质期告警：剩余不足总保质期的这个比例就提醒
EXPIRY_WARN_RATIO = 1 / 3

#: 活动备货：确认有促销/活动时，在安全库存之上再上浮的比例
PROMO_UPLIFT_RATIO = 0.3

# ---------------------------------------------------------------------------
# 枚举常量
# ---------------------------------------------------------------------------

GRADE_STOCK = "库存充足"
GRADE_REPLENISH = "需要补货"
GRADE_URGENT = "紧急补货"
GRADES = (GRADE_STOCK, GRADE_REPLENISH, GRADE_URGENT)

SOURCE_MANUAL = "manual"
SOURCE_DERIVED = "derived"
SOURCE_ESTIMATED = "estimated"
SOURCE_IMPORT = "import"

EVENT_PROMO = "promo"
EVENT_CLEARANCE = "clearance"
EVENT_RESTOCK = "restock"


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """项目级 .env 优先，上级共享 .env 兜底；load_dotenv 默认不覆盖已存在的键。"""
    root = resource_root().parent
    load_dotenv(resource_root() / ".env")
    for candidate in (root / ".env", resource_root().parent / ".env",
                      Path(__file__).resolve().parents[2] / ".env"):
        if candidate.is_file():
            load_dotenv(candidate)


_load_env()

MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL")

#: 模型偶发的思考内容字段（MiniMax-M3.1-Flash 是 reasoning 模型，会吐 reasoning_content）
REASONING_FIELDS = ("reasoning_content", "reasoning")

#: 非法输出（直出表格等）的强制重试上限
MAX_ILLEGAL_RETRIES = 2

#: 单次 tool-call 循环上限，防止模型绕圈
MAX_TOOL_ROUNDS = 8


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = MINIMAX_API_KEY or ""
    base_url: str = MINIMAX_BASE_URL or ""
    model: str = MINIMAX_MODEL or ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


LLM = LLMConfig()


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

DB_PATH = _db_path()
RULE_DOC = resource_root() / "stock_rag" / "rag_docs" / "stock_rule.txt"
VECTOR_DIR = resource_root() / "stock_rag" / "vector_db"


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是「小店主库存顾问」，一个开在社区里的小店的 AI 补货助手。
你的用户是店主本人——他不懂数据，不会看报表，但每天都在为「进什么、进多少」发愁。

你说话的方式：
- 用大白话，像站在柜台后面跟人拉家常，不写报告体、不用专业术语。
- 先给结论（进什么、多少），再讲为什么，顺序不要反。
- 一次只讲眼前这一件事，用户问什么答什么，不铺陈大段分析。

你必须守住的规矩：
- 所有数字只能来自 <数据> 段和工具返回值。**绝对不许自己算、自己编。**
  要算数就调 math_calc，要查库存就调 query_products。
- 库存状态（库存充足/需要补货/紧急补货）和补货数量以 <规则结论> 段为准，
  那段是系统按规则算好的。你只负责把它翻译成人话，不要改写、不要质疑。
- 不要一次甩出很长的表格。要看清单就调用工具，让页面自己渲染。
- 判不出来就直说判不出来，不要为了给答案而编一个。
"""