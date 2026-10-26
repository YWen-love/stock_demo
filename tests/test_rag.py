"""RAG 测试，重点是索引新鲜度。

老行为：向量库建好就不管了。改了 stock_rule.txt 之后索引还是老的，
检索安安静静返回旧规则 —— 不报错、不告警，只是按过期口径给建议。
这种 bug 最贵，因为它看起来一切正常。现在比对源文档指纹，不一致就重建。
"""

from __future__ import annotations

import json

import pytest

from advisor import rag

RULE_TEXT = "安全库存阈值 = 滚动周均销量 × 1.2\n紧急断货阈值 = 周均 ÷ 7 × 2 × 1.2\n"


@pytest.fixture()
def docs(tmp_path):
    doc_dir = tmp_path / "rag_docs"
    doc_dir.mkdir()
    (doc_dir / "rule.txt").write_text(RULE_TEXT, encoding="utf-8")
    return doc_dir


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------

def test_source_signature_changes_with_content(docs):
    first = rag.source_signature(docs)
    (docs / "rule.txt").write_text(RULE_TEXT + "新增：滞销判定\n", encoding="utf-8")
    assert rag.source_signature(docs) != first


def test_source_signature_changes_when_file_added(docs):
    first = rag.source_signature(docs)
    (docs / "extra.txt").write_text("新文件", encoding="utf-8")
    assert rag.source_signature(docs) != first


# ---------------------------------------------------------------------------
# 索引新鲜度
# ---------------------------------------------------------------------------

def test_ensure_index_builds_when_absent(docs, tmp_path):
    vector_dir = tmp_path / "vector_db"
    assert rag.ensure_index(docs, vector_dir) is True
    metadata = json.loads((vector_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["chunks"]


def test_ensure_index_skips_rebuild_when_unchanged(docs, tmp_path):
    vector_dir = tmp_path / "vector_db"
    rag.ensure_index(docs, vector_dir)
    assert rag.ensure_index(docs, vector_dir) is False


def test_ensure_index_rebuilds_when_source_changes(docs, tmp_path):
    vector_dir = tmp_path / "vector_db"
    rag.ensure_index(docs, vector_dir)
    assert rag.ensure_index(docs, vector_dir) is False

    (docs / "rule.txt").write_text(RULE_TEXT + "滞销：每周不到 1 件\n", encoding="utf-8")

    assert rag.ensure_index(docs, vector_dir) is True
    metadata = json.loads((vector_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source_signature"] == rag.source_signature(docs)


def test_ensure_index_rebuilds_legacy_index_without_signature(docs, tmp_path):
    """老版本建出来的索引没有指纹字段 —— 它天然是不确定的，必须重建一次。"""
    vector_dir = tmp_path / "vector_db"
    rag.ensure_index(docs, vector_dir)
    metadata_path = vector_dir / "metadata.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload.pop("source_signature")
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert rag.ensure_index(docs, vector_dir) is True


def test_retrieve_returns_new_rule_after_source_change(docs, tmp_path):
    """端到端：新规则写进文件，下一次检索就能搜到。"""
    vector_dir = tmp_path / "vector_db"
    rag.ensure_index(docs, vector_dir)

    (docs / "rule.txt").write_text(
        RULE_TEXT + "\n## 滞销判定\n每周销量低于 1 件即判定为滞销商品。\n",
        encoding="utf-8")

    assert "滞销" in rag.retrieve("滞销怎么判定", docs, vector_dir)


# ---------------------------------------------------------------------------
# 规则缺失
# ---------------------------------------------------------------------------

def test_missing_rule_document_raises_instead_of_falling_back(tmp_path):
    """PRD 5.2.1：规则缺失就终止计算，不许用兜底规则顶上。

    返回一句「未检索到」等于把机会让给模型自己编 —— 所以这里必须抛。
    """
    with pytest.raises(rag.RuleDocumentMissing):
        rag.rule_document_text(tmp_path / "does_not_exist")


def test_empty_rule_document_raises(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(rag.RuleDocumentMissing):
        rag.rule_document_text(empty)


def test_rule_document_text_returns_full_text(docs):
    text = rag.rule_document_text(docs)
    assert "安全库存阈值" in text
    assert "紧急断货阈值" in text