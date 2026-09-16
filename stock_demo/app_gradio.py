import gradio as gr
import pandas as pd
from datetime import datetime
import logging
import os
import json
import sys
from pathlib import Path

try:
    from volcenginesdkarkruntime import Ark
except ImportError:  # pragma: no cover
    Ark = None

try:
    from volcenginesdkarkruntime.exceptions import APIStatusError
except ImportError:  # pragma: no cover
    class APIStatusError(Exception):
        def __init__(self, message: str = "", status_code: int = 500):
            super().__init__(message)
            self.status_code = status_code

from stock_rag.rag_service import answer_query
from dotenv import load_dotenv

# ========== 配置区 ==========
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
load_dotenv(APP_ROOT / ".env")
ARK_API_KEY = os.getenv("ARK_API_KEY")
ARK_ENDPOINT_ID = os.getenv("ARK_ENDPOINT_ID")
REPORT_FILE = APP_ROOT / "stock_report.md"
LOG_FILE = APP_ROOT / "local_demo.log"
RAG_DOCS_PATH = APP_ROOT / "stock_rag" / "rag_docs"
RAG_VECTOR_DB_PATH = APP_ROOT / "stock_rag" / "vector_db"


def _read_fallback_rules() -> str:
    candidate_paths = [
        RAG_DOCS_PATH / "stock_rule.txt",
        Path(__file__).resolve().parent / "stock_rag" / "rag_docs" / "stock_rule.txt",
    ]
    for path in candidate_paths:
        if path.exists():
            try:
                return path.read_text(encoding="utf-8").strip() or "未配置库存规则文档。"
            except OSError:
                continue
    return (
        "# 企业库存校验核心规则\n\n"
        "## 1. 阈值说明\n"
        "常规安全库存阈值 = 近7天总销量 × 1.2\n"
        "紧急断货阈值 = 近两天总销量 × 1.2\n"
        "补货量 = 常规安全库存阈值 - 当前库存\n"
    )


def _build_ark_client():
    if not ARK_API_KEY or not ARK_ENDPOINT_ID:
        logger = logging.getLogger(__name__)
        logger.warning("ARK_API_KEY / ARK_ENDPOINT_ID 缺失，已启用本地兜底模式")
        return None
    if Ark is None:
        logger = logging.getLogger(__name__)
        logger.warning("volcenginesdkarkruntime 未安装，已启用本地兜底模式")
        return None
    return Ark(api_key=ARK_API_KEY, base_url="https://ark.cn-beijing.volces.com/api/v3")

# ----------------------日志初始化----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========= 初始化火山方舟客户端（DeepSeek V4 Flash）=========
client = _build_ark_client()

# ---------------- Function‑Call工具定义 ----------------
tools = [
    {
        "type": "function",
        "function": {
            "name": "math_calc",
            "description": "库存业务数值计算工具，仅接收数学表达式字符串，支持数字、加减乘除、小数点、括号。安全库存、紧急断货阈值、补货量全部必须调用本工具，模型禁止自行演算数字。",
            "parameters": {
                "type": "object",
                "required": ["expr"],
                "properties": {
                    "expr": {"type": "string", "description": "数学表达式，示例：'180 / 7 * 1.2'"}
                }
            }
        }
    }
]

# 本地Python计算器，增加白名单防护，修复eval注入漏洞
ALLOWED_EXPR_CHARS = set("0123456789.+-*/() ")
def math_calc(expr: str):
    # 白名单拦截非法字符
    if not set(expr).issubset(ALLOWED_EXPR_CHARS):
        err_msg = "非法表达式：仅允许数字、加减乘除、小数点、括号"
        logger.error(f"math_calc非法字符拦截 expr={expr}")
        return {"error": err_msg}
    try:
        res = eval(expr)
        if not isinstance(res, (int, float)):
            raise ValueError("计算结果不是有效数字")
        ret = {"result": round(res, 2)}
        return ret
    except Exception as e:
        err_msg = str(e)
        logger.error(f"表达式计算异常 expr={expr} err={err_msg}")
        return {"error": err_msg}

def looks_like_table_output(text: str) -> bool:
    """判断是否是直接表格输出"""
    if not text:
        return False
    stripped = text.strip()
    if "|" not in stripped:
        return False
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    header_lines = [line for line in lines if line.startswith("|") and "|" in line]
    if not header_lines:
        return False
    return True

# Agent循环：增加calc_completed状态区分计算阶段与报告输出阶段
def fallback_generate_report(df: pd.DataFrame, enterprise_rules: str) -> str:
    """当 LLM/工具调用异常时，使用规则驱动的本地兜底策略生成表格。"""
    if df is None or df.empty:
        logger.warning("库存数据为空，返回空数据兜底提示")
        return "❌ 错误：上传的库存数据为空，请检查 Excel 内容。"

    required_cols = ["商品 id", "商品名称", "当前库存", "近 7 天销量"]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        logger.warning("库存表格字段缺失，无法生成兜底报告：%s", missing)
        return f"❌ 错误：表格缺少必要字段：{missing}"

    rows = []
    for _, row in df.iterrows():
        try:
            item_id = row["商品 id"]
            name = row["商品名称"]
            current_stock = float(row["当前库存"])
            sales_7d = float(row["近 7 天销量"])
        except (KeyError, TypeError, ValueError):
            logger.warning("跳过数据行，字段格式异常：%s", row.to_dict())
            continue

        if current_stock < 0:
            current_stock = 0
        if sales_7d < 0:
            sales_7d = 0

        safety_threshold = sales_7d * 1.2
        emergency_threshold = sales_7d * 2 / 7 * 1.2
        replenish = max(0.0, safety_threshold - current_stock)
        if current_stock >= safety_threshold:
            remark = "库存充足"
        elif current_stock >= emergency_threshold:
            remark = "需要补货"
        else:
            remark = "紧急补货"

        rows.append([
            item_id,
            name,
            int(current_stock) if current_stock.is_integer() else round(current_stock, 2),
            int(sales_7d) if sales_7d.is_integer() else round(sales_7d, 2),
            int(safety_threshold) if safety_threshold.is_integer() else round(safety_threshold, 2),
            int(replenish) if replenish.is_integer() else round(replenish, 2),
            remark,
        ])

    if not rows:
        return "❌ 错误：库存数据清洗后无有效行，无法生成报告。"

    table_lines = [
        "| 商品id | 商品名称 | 当前库存 | 近7天销量 | 安全库存阈值 | 补货建议 | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        table_lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(table_lines)


def agent_run(messages):
    logger.info("启动Agent推理循环")
    if client is None:
        logger.warning("没有可用的 ARK 客户端，直接启动本地兜底逻辑")
        return ""

    no_tool_attempts = 0
    max_retry = 2
    final_content = ""
    calc_completed = False
    while True:
        try:
            resp = client.chat.completions.create(
                model=ARK_ENDPOINT_ID,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.05,
                max_tokens=4000,
            )
        except Exception as exc:
            logger.exception("ARK API 调用失败，启动本地兜底策略: %s", exc)
            return ""

        if not getattr(resp, "choices", None):
            logger.warning("模型响应为空，启动兜底策略")
            return ""

        msg = resp.choices[0].message
        finish_reason = getattr(resp.choices[0], "finish_reason", None)
        content_preview = (msg.content or "")[:120]
        logger.info(
            "模型响应：finish_reason=%s tool_calls=%s content_preview=%s",
            finish_reason,
            len(msg.tool_calls or []),
            content_preview,
        )

        if msg.tool_calls:
            no_tool_attempts = 0
            calc_completed = True
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be an object")
                    expr = args.get("expr")
                    if not expr or not isinstance(expr, str):
                        raise ValueError("缺失 expr 字段")
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    logger.warning("工具调用参数非法，触发降级：%s", exc)
                    messages.append({
                        "role": "user",
                        "content": "上一轮工具调用参数无效，请严格按 {'expr': '数学表达式'} 形式返回。",
                    })
                    no_tool_attempts += 1
                    if no_tool_attempts > max_retry:
                        logger.warning("工具调用参数持续错误，回退到本地兜底")
                        return ""
                    continue

                logger.info("模型请求调用 math_calc：%s", expr)
                calc_out = math_calc(expr)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(calc_out),
                })
            continue

        content = msg.content or ""
        if calc_completed:
            final_content = content
            break
        if looks_like_table_output(content):
            no_tool_attempts += 1
            logger.warning("检测到未调用工具的直接表格输出，视为非法回答；将强制重试（第 %s 次）", no_tool_attempts)
            messages.append({
                "role": "user",
                "content": (
                    "上一轮回答无效：你直接输出了表格，但没有调用 math_calc。"
                    "必须在下一轮先调用 math_calc，不能输出任何最终结果，"
                    "只有在工具返回结果后，才能生成最终的 Markdown 表格。"
                ),
            })
            if no_tool_attempts > max_retry:
                logger.warning("多次重试后仍未调用 math_calc，回退到安全兜底策略")
                final_content = content
                break
            continue
        no_tool_attempts += 1
        if no_tool_attempts <= max_retry:
            logger.warning("模型未调用 math_calc，将重试（第 %s 次）", no_tool_attempts)
            messages.append({
                "role": "user",
                "content": (
                    "你尚未调用 math_calc。现在必须先调用 math_calc，"
                    "为每个商品完成所需计算；在工具调用完成前禁止输出表格。"
                ),
            })
            continue
        logger.warning("多次重试后模型依然未调用 math_calc，存在幻觉风险，直接使用模型输出结果")
        final_content = content
        break
    return final_content

# =========【System Prompt 增强约束】========
SYSTEM_PROMPT = """
你是B端企业库存校验助手。
你会结合【检索出来的库存业务规则文档】、Excel库存数据完成库存评估。
强制约束：
1. 所有数学计算（近2天折算销量、常规安全库存阈值、紧急断货阈值、补货量）禁止自己心算，必须调用math_calc工具；计算公式以业务规则文档为准，严格按文档公式生成表达式，不允许自行修改、编造公式。
2. ⚠极其重要：绝对不能修改、抄错、混淆math_calc返回的result，A商品的计算结果只能用于A商品，严禁不同商品之间混用数值。
3. 只能基于检索得到的业务规则进行判定，不能自己编造业务逻辑。
4. 当尚未完成全部商品计算，禁止输出步骤、推演文字和最终表格；全部商品通过math_calc计算完毕之后，可以直接输出markdown表格报告。
5. 输出表格字段：商品id、商品名称、当前库存、近7天总销量、常规安全库存阈值、补货建议、备注。
6. 备注只能三选一：库存充足 / 需要补货 / 紧急补货，禁止额外文字。
"""

def retrieve_stock_rules():
    """在调用大模型前，从本地RAG向量库检索库存业务规则。"""
    query = "企业库存校验、安全库存阈值、紧急断货阈值、库存分级和补货建议规则"
    logger.info("开始检索库存业务规则")
    try:
        rules = answer_query(
            query=query,
            doc_dir=RAG_DOCS_PATH,
            vector_db_dir=RAG_VECTOR_DB_PATH,
            top_k=3,
        )
    except Exception as exc:
        logger.exception("RAG 规则检索异常，启用本地规则兜底: %s", exc)
        rules = _read_fallback_rules()
    logger.info("库存业务规则检索完成，检索结果长度：%s", len(rules))
    return rules


def load_stock_excel_from_upload(file_obj):
    logger.info("读取网页上传的库存Excel文件")
    if file_obj is None:
        raise ValueError("未上传库存Excel文件")

    try:
        df = pd.read_excel(file_obj)
    except Exception as exc:
        raise ValueError(f"读取Excel失败：{exc}") from exc

    if df is None or df.empty:
        raise ValueError("上传的库存Excel为空，请检查文件内容")

    required_cols = ["商品 id", "商品名称", "当前库存", "近 7 天销量"]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        logger.error("表格缺少必要字段：%s，实际列名：%s", missing, list(df.columns))
        raise ValueError(f"表格缺少必要字段：{missing}，实际列名：{list(df.columns)}")

    text_cols = ["商品 id", "商品名称"]
    for col in text_cols:
        blank_rows = df[col].isna() | df[col].astype(str).str.strip().eq("")
        if blank_rows.any():
            rows = (df.index[blank_rows] + 2).tolist()
            raise ValueError(f"库存表字段【{col}】存在空值，请检查第 {rows} 行")

    numeric_cols = ["当前库存", "近 7 天销量"]
    for col in numeric_cols:
        numeric_values = pd.to_numeric(df[col], errors="coerce")
        invalid_rows = numeric_values.isna()
        if invalid_rows.any():
            rows = (df.index[invalid_rows] + 2).tolist()
            raise ValueError(f"库存表字段【{col}】必须是数字，请检查第 {rows} 行")
        if numeric_values.lt(0).any():
            rows = (df.index[numeric_values.lt(0)] + 2).tolist()
            raise ValueError(f"库存表字段【{col}】不能为负数，请检查第 {rows} 行")
        df[col] = numeric_values

    extra_cols = [col for col in df.columns if col not in required_cols]
    if extra_cols:
        logger.warning("检测到额外字段，将不参与库存分析：%s", extra_cols)
    logger.info("库存表格解析完成，共 %s 行", len(df))
    return df

REPORT_COLUMNS = [
    "商品id",
    "商品名称",
    "当前库存",
    "近7天销量",
    "安全库存阈值",
    "补货建议",
    "备注",
]

def format_report_table(report_content):
    """统一模型输出的库存 Markdown 表格格式，避免列数和分隔线不一致。"""
    rows = []
    for line in report_content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.count("|") < 2:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        if cells[0].lower().replace(" ", "") in {"商品id", "商品id"}:
            continue
        if len(cells) < len(REPORT_COLUMNS):
            cells.extend([""] * (len(REPORT_COLUMNS) - len(REPORT_COLUMNS)))
        rows.append(cells[:len(REPORT_COLUMNS)])
    if not rows:
        logger.warning("模型未返回可识别的库存 Markdown 表格")
        return report_content.strip()
    table_lines = [
        "| " + " | ".join(REPORT_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in REPORT_COLUMNS) + " |",
    ]
    table_lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(table_lines)

def write_markdown_report(report_content, output_path):
    """导出库存校验报告为markdown文件，存到项目目录"""
    logger.info(f"开始写入Markdown报告：{output_path}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md_text = f"""# 库存校验补货分析报告
> 报告生成时间：{now}
{report_content}
---
*本报告由AI‑Agent库存分析Demo自动生成*
"""
    with open(output_path, "w", encoding="utf-8") as fp:
        fp.write(md_text)
    logger.info(f"报告文件写入完成 {output_path}")

# 默认加载RAG中的规则，供页面展示和后续分析使用
DEFAULT_RULES = retrieve_stock_rules()

# ====================== Gradio业务入口函数 ======================
def run_stock_analysis(excel_file, rule_text):
    try:
        logger.info("Gradio页面触发库存分析任务")
        if excel_file is None:
            return "❌错误：请上传库存Excel文件！", gr.update(value=None, visible=False)

        stock_df = load_stock_excel_from_upload(excel_file)
        analysis_cols = ["商品 id", "商品名称", "当前库存", "近 7 天销量"]
        stock_data = stock_df[analysis_cols].to_string(index=False)

        enterprise_rules = rule_text.strip() if rule_text and rule_text.strip() else DEFAULT_RULES

        final_system_content = (
            SYSTEM_PROMPT
            + "\n====RAG检索到的企业私有库存规则====\n"
            + enterprise_rules
            + "\n====工具调用要求====\n"
            + "必须先为每个商品调用 math_calc，完成近2天销量、常规安全库存阈值、"
            + "紧急断货阈值和补货建议的计算；所有商品计算完成后才能输出表格。"
        )
        messages = [
            {"role": "system", "content": final_system_content},
            {"role": "user", "content": f"以下是企业库存数据，请完成库存校验、预测和补货建议：\n{stock_data}"},
        ]

        result = agent_run(messages)
        if not result or not looks_like_table_output(result):
            logger.warning("模型输出无效或未生成表格，启动规则驱动兜底策略")
            result = fallback_generate_report(stock_df, enterprise_rules)

        result = result.replace("```markdown", "").replace("```", "").strip()
        result = format_report_table(result)
        write_markdown_report(result, REPORT_FILE)
        return result, gr.update(value=REPORT_FILE, visible=True)

    except FileNotFoundError as e:
        msg = f"文件错误：{str(e)}"
        logger.error(msg)
        return f"❌ {msg}", gr.update(value=None, visible=False)
    except APIStatusError as e:
        if e.status_code == 401:
            msg = "API鉴权失败：ARK_API_KEY错误或已过期，请核对密钥！"
            logger.error(f"{msg}, status_code:{e.status_code}")
            return f"❌ {msg}", gr.update(value=None, visible=False)
        else:
            msg = f"API服务异常 status={e.status_code}：{str(e)}"
            logger.error(msg)
            return f"❌ {msg}", gr.update(value=None, visible=False)
    except ConnectionError as e:
        msg = "API网络错误：无法连接火山方舟API，请检查网络"
        logger.error(msg)
        return f"❌ {msg}", gr.update(value=None, visible=False)
    except ValueError as e:
        msg = str(e)
        logger.error("输入校验失败：%s", msg)
        return f"❌ {msg}", gr.update(value=None, visible=False)
    except Exception as e:
        msg = f"程序处理失败：{str(e)}"
        logger.error(msg, exc_info=True)
        return f"❌ {msg}"

# ====================== Gradio UI界面定义 ======================
with gr.Blocks(title="智能库存预测Agent") as demo:
    gr.Markdown("# 📦智能库存预测Agent Demo")
    gr.Markdown("上传库存Excel，RAG自动读取业务规则，一键执行库存阈值计算并生成标准化Markdown报告")

    with gr.Row():
        excel_input = gr.File(label="上传库存.xlsx", file_types=[".xlsx"])
    rule_input = gr.Textbox(
        label="业务规则预览（RAG自动加载，可修改覆盖）",
        lines=8,
        value=DEFAULT_RULES,
    )

    run_button = gr.Button("🚀 开始库存分析", variant="primary")
    report_output = gr.Markdown(label="📄生成的库存报告")
    report_download = gr.File(label="⬇️ 下载 Markdown 报告", interactive=False, visible=False)

    run_button.click(
        fn=run_stock_analysis,
        inputs=[excel_input, rule_input],
        outputs=[report_output, report_download]
    )

if __name__ == "__main__":
    demo.launch()
