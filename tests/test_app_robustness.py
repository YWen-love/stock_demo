import json
from io import BytesIO
from pathlib import Path

import pytest

import app_gradio


class FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.index = 0

    def create(self, **kwargs):
        resp = self._responses[self.index]
        self.index += 1
        return resp


class FakeClient:
    def __init__(self, responses):
        self.chat = type("Chat", (), {"completions": FakeCompletions(responses)})()


def test_answer_query_falls_back_when_vector_db_missing(tmp_path):
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    (doc_dir / "rule.txt").write_text("库存规则：安全库存阈值 = 近7天销量 * 1.2", encoding="utf-8")
    vector_dir = tmp_path / "missing_vector_db"

    result = app_gradio.answer_query("安全库存规则", doc_dir, vector_dir, top_k=2)

    assert "库存规则" in result or "未检索到相关资料" in result


def test_agent_run_handles_invalid_tool_calls(monkeypatch):
    class InvalidToolCall:
        id = "call_1"
        function = type("Function", (), {"name": "math_calc", "arguments": "{not-valid-json}"})()

    # 第1轮返回非法 tool_calls；第2轮返回合法最终表格
    valid_msg = FakeMessage(
        content="| 商品id | 商品名称 | 当前库存 | 近7天销量 | 安全库存阈值 | 补货建议 | 备注 |\n| 1 | A | 80 | 50 | 60 | 0 | 库存充足 |"
    )
    fake_client = FakeClient([
        type("Resp", (), {"choices": [FakeChoice(FakeMessage(tool_calls=[InvalidToolCall()]))]})(),
        type("Resp", (), {"choices": [FakeChoice(valid_msg)]})(),
    ])
    monkeypatch.setattr(app_gradio, "client", fake_client)

    result = app_gradio.agent_run([{"role": "user", "content": "数据"}])

    assert "商品id" in result
    assert "库存充足" in result


def _stock_excel(rows):
    import pandas as pd

    buffer = BytesIO()
    pd.DataFrame(rows).to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer


def test_load_stock_excel_rejects_non_inventory_numeric_data():
    upload = _stock_excel([
        {
            "商品 id": "A-1",
            "商品名称": "客户名单",
            "当前库存": "未知",
            "近 7 天销量": "未知",
        }
    ])

    with pytest.raises(ValueError, match="必须是数字"):
        app_gradio.load_stock_excel_from_upload(upload)


def test_load_stock_excel_rejects_blank_product_fields():
    upload = _stock_excel([
        {
            "商品 id": "",
            "商品名称": "A",
            "当前库存": 10,
            "近 7 天销量": 2,
        }
    ])

    with pytest.raises(ValueError, match="商品 id.*空值"):
        app_gradio.load_stock_excel_from_upload(upload)


def test_load_stock_excel_allows_extra_columns():
    upload = _stock_excel([
        {
            "商品 id": "A-1",
            "商品名称": "商品A",
            "当前库存": 10,
            "近 7 天销量": 2,
            "备注": "仓库一",
        }
    ])

    result = app_gradio.load_stock_excel_from_upload(upload)

    assert "备注" in result.columns
    assert result.loc[0, "当前库存"] == 10


def test_fallback_report_calculates_safety_threshold():
    upload = _stock_excel([
        {
            "商品 id": "A-1",
            "商品名称": "商品A",
            "当前库存": 10,
            "近 7 天销量": 20,
        }
    ])
    stock_df = app_gradio.load_stock_excel_from_upload(upload)

    report = app_gradio.fallback_generate_report(stock_df, "")

    assert "| A-1 | 商品A | 10 | 20 | 24 | 14 |" in report
