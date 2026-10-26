"""MiniMax 接入：tool-call 循环 + 非法输出重试 + 兜底信号。

三个要点：
1. **不抛异常**。API 挂了、模型返空、参数非法 —— 一律把 ok=False 带回去，
   由 advisor 决定走纯规则输出。让对话不至于因为外部服务抖一下就整个断掉。
2. **非法输出强制重试**。直出 markdown 表格 = 模型又滑回旧 Excel 报表模式，
   在对话界面里那玩意儿没法看，拦下来让它重写。
3. **tool_calls 里的参数不可信**。模型偶尔会吐不合法的 JSON，
   处理成工具返回值告诉它"参数非法"比抛异常好，它下一轮能自己纠正。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import LLM, MAX_ILLEGAL_RETRIES, MAX_TOOL_ROUNDS
from .tools import TOOL_SCHEMAS, ToolContext, execute_tool

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 缺依赖时降级为纯规则模式
    from openai import APIStatusError, OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

    class APIStatusError(Exception):  # type: ignore[no-redef]
        def __init__(self, message: str = "", status_code: int = 500):
            super().__init__(message)
            self.status_code = status_code


# 模型偶尔会把思考过程写在 content 里（MiniMax-M3.1-Flash 是 reasoning 模型）
_THINKING_TAGS = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE
)


def build_client() -> Optional[Any]:
    if not LLM.enabled:
        logger.warning("未配置 MINIMAX_API_KEY / MINIMAX_BASE_URL / MINIMAX_MODEL，启用纯规则模式")
        return None
    if OpenAI is None:
        logger.warning("openai 包未安装，启用纯规则模式")
        return None
    return OpenAI(api_key=LLM.api_key, base_url=LLM.base_url)


@dataclass
class AgentResult:
    ok: bool = False
    text: str = ""
    retried: bool = False
    used_tools: List[str] = field(default_factory=list)
    submissions: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def as_payload(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "retried": self.retried,
            "used_tools": self.used_tools,
            "submissions": self.submissions,
            "error": self.error,
        }


def strip_reasoning(text: str) -> str:
    """去掉模型的内心独白，别直接甩给店主看。"""
    if not text:
        return ""
    return _THINKING_TAGS.sub("", text).strip()


def looks_like_table_output(text: str) -> bool:
    """模型是不是又滑回"直出 markdown 表格"的旧毛病。

    对话界面里表格没法交互，与其让店主盯着一张死表，不如让它把结论讲成一句话。
    """
    if not text:
        return False
    stripped = text.strip()
    if "|" not in stripped:
        return False
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    return any(line.startswith("|") and line.count("|") >= 2 for line in lines)


def _tool_message(message: Any) -> Dict[str, Any]:
    return {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in (message.tool_calls or [])
        ],
    }


def _parse_arguments(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError(f"工具参数不是合法 JSON：{raw!r}")
    if not isinstance(parsed, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return parsed


def run_agent(
    messages: List[Dict[str, Any]],
    ctx: ToolContext,
    *,
    client: Optional[Any] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> AgentResult:
    """跑一轮 tool-call 循环，直到模型给出最终回复。

    client 参数是给测试用的注入口 —— 传个假 client 就能单测整条回路，不用真调 API。
    """
    active = client if client is not None else build_client()
    if active is None:
        return AgentResult(ok=False, error="未配置模型，已降级为纯规则模式")

    tool_schemas = tools if tools is not None else TOOL_SCHEMAS
    history = list(messages)
    used_tools: List[str] = []
    result = AgentResult()
    illegal_attempts = 0

    for _round in range(MAX_TOOL_ROUNDS):
        try:
            response = active.chat.completions.create(
                model=LLM.model,
                messages=history,
                tools=tool_schemas,
                tool_choice="auto",
                temperature=0.3,
                max_tokens=2000,
            )
        except APIStatusError as exc:
            status = getattr(exc, "status_code", 0)
            result.error = f"模型接口返回 {status}：{exc}"
            logger.warning("模型接口报错 status=%s err=%s", status, exc)
            return result
        except Exception as exc:  # noqa: BLE001 - 网络层什么都可能抛
            result.error = f"调用模型失败：{type(exc).__name__}: {exc}"
            logger.warning("调用模型失败 err=%s", exc)
            return result

        choices = getattr(response, "choices", None)
        if not choices:
            result.error = "模型返回为空"
            return result

        message = choices[0].message
        content = strip_reasoning(getattr(message, "content", "") or "")

        if getattr(message, "tool_calls", None):
            history.append(_tool_message(message))
            for call in message.tool_calls:
                name = call.function.name
                used_tools.append(name)
                try:
                    arguments = _parse_arguments(call.function.arguments)
                    output = execute_tool(name, arguments, ctx)
                except ValueError as exc:
                    output = json.dumps({"ok": False, "error": str(exc)},
                                        ensure_ascii=False)
                except Exception as exc:  # noqa: BLE001 - 工具边界已在内部兜一层
                    output = json.dumps({"ok": False, "error": str(exc)},
                                        ensure_ascii=False)
                history.append({"role": "tool", "tool_call_id": call.id,
                                "name": name, "content": output})
            continue

        # 非法输出闸：直出表格 → 强制重写
        if looks_like_table_output(content):
            illegal_attempts += 1
            if illegal_attempts > MAX_ILLEGAL_RETRIES:
                result.error = "模型连续直出表格，已拦截并降级为纯规则输出"
                return result
            logger.info("拦截模型直出表格，第 %s 次要求重写", illegal_attempts)
            history.append({"role": "assistant", "content": content})
            history.append({
                "role": "user",
                "content": "刚才那段表格在这个界面里没法看。请改成两三句话直接说结论："
                           "先说进什么、多少，再说为什么。要看清单的话我这边会自己列出来，"
                           "你不用画表。",
            })
            result.retried = True
            continue

        result.ok = bool(content)
        result.text = content
        result.used_tools = used_tools
        result.submissions = list(ctx.submissions)
        return result

    result.error = f"工具循环超过 {MAX_TOOL_ROUNDS} 轮仍未收敛"
    result.used_tools = used_tools
    result.submissions = list(ctx.submissions)
    return result