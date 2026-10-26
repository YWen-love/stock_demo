"""RAG 封装：给模型查规则，顺手修掉"索引陈旧"这个静默错误。

原来的行为：向量库建好就不管了。改了 stock_rule.txt 之后索引还是老的，
`answer_query` 会安安静静返回旧规则 —— 不报错、不告警，店主拿到一个按过期口径算的建议。
这种 bug 最贵，因为它看起来是在正常工作。

现在每次检索前比对源文档指纹，不一致就重建。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import List

from stock_rag.vector_store import LocalVectorDB

logger = logging.getLogger(__name__)


class RuleDocumentMissing(RuntimeError):
    """规则文件读不到。

    PRD 5.2.1/5.2.3：规则缺失就终止计算，**不许用兜底规则顶上**。
    所以这里抛异常而不是返回一句「未检索到相关资料」——那等于让模型自己编。
    """


def _iter_source_files(doc_dir: Path):
    for path in sorted(doc_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".txt", ".md", ".json"}:
            yield path


def source_signature(doc_dir: Path) -> str:
    """源文档指纹：文件名 + 内容哈希。

    只算内容不算文件名的话，删一个文件重建另一个会算出同一个指纹。
    """
    digest = hashlib.sha256()
    for path in _iter_source_files(doc_dir):
        digest.update(path.name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()


def load_documents(doc_dir: Path) -> List[str]:
    documents = []
    for path in _iter_source_files(doc_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if text.strip():
            documents.append(text)
    return documents


def rule_document_text(doc_dir: Path) -> str:
    """规则全文。给 prompt 用 —— 模型要看到原始规则，而不是检索出来的三段碎片。

    规则总共几千字，直接全量给；检索那套留给「问法不确定」的场景。
    """
    if not doc_dir.exists():
        raise RuleDocumentMissing(f"规则文档目录不存在：{doc_dir}")
    documents = load_documents(doc_dir)
    if not documents:
        raise RuleDocumentMissing(f"规则文档目录为空：{doc_dir}")
    return "\n\n".join(documents)


def ensure_index(doc_dir: Path, vector_dir: Path, chunk_size: int = 500,
                 overlap: int = 100) -> bool:
    """索引不存在或源文件变了就重建。返回 True 表示这次重建过。"""
    current = source_signature(doc_dir)
    metadata_path = vector_dir / "metadata.json"
    vectors_path = vector_dir / "vectors.npy"

    if metadata_path.exists() and vectors_path.exists():
        try:
            stored = json.loads(metadata_path.read_text(encoding="utf-8"))
            if stored.get("source_signature") == current and stored.get("chunks"):
                return False
            logger.info("规则文档已变更或索引无指纹，正在重建向量库")
        except (json.JSONDecodeError, OSError):
            logger.info("向量库元数据损坏，正在重建")

    documents = load_documents(doc_dir)
    if not documents:
        raise RuleDocumentMissing(f"规则文档目录为空：{doc_dir}")

    store = LocalVectorDB(vector_dir)
    store.source_signature = current
    store.build_from_documents(documents, chunk_size=chunk_size, overlap=overlap)
    return True


def retrieve(query: str, doc_dir: Path, vector_dir: Path, top_k: int = 3) -> str:
    """检索规则片段。索引缺失/损坏时先重建再检索。"""
    ensure_index(doc_dir, vector_dir)
    store = LocalVectorDB.load(vector_dir)
    hits = store.search(query, top_k=top_k)
    if not hits:
        return "未检索到相关规则片段。"
    return "\n\n".join(f"[相似度 {item['score']}]\n{item['content']}" for item in hits)