from __future__ import annotations

from pathlib import Path
from typing import List

try:
    from .vector_store import LocalVectorDB
except ImportError:  # pragma: no cover
    from vector_store import LocalVectorDB


def _fallback_rule_text(doc_dir: str | Path) -> str:
    doc_path = Path(doc_dir)
    if not doc_path.exists():
        return "未检索到相关资料，且未发现规则文档。"

    for file_path in sorted(doc_path.rglob("*")):
        if file_path.is_file() and file_path.suffix.lower() in {".txt", ".md", ".json"}:
            try:
                text = file_path.read_text(encoding="utf-8")
            except Exception:
                continue
            if text.strip():
                return text.strip()
    return "未检索到相关资料，且规则文档为空。"


def load_documents_from_dir(doc_dir: str | Path) -> List[str]:
    doc_path = Path(doc_dir)
    if not doc_path.exists():
        raise FileNotFoundError(f"文档目录不存在: {doc_path}")

    documents: List[str] = []
    for file_path in sorted(doc_path.rglob("*")):
        if file_path.is_file() and file_path.suffix.lower() in {".txt", ".md", ".json"}:
            try:
                text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if text.strip():
                documents.append(text)
    return documents


def build_rag(doc_dir: str | Path, vector_db_dir: str | Path, chunk_size: int = 500, overlap: int = 100) -> LocalVectorDB:
    docs = load_documents_from_dir(doc_dir)
    store = LocalVectorDB(vector_db_dir)
    store.build_from_documents(docs, chunk_size=chunk_size, overlap=overlap)
    return store


def answer_query(query: str, doc_dir: str | Path, vector_db_dir: str | Path, top_k: int = 3) -> str:
    doc_path = Path(doc_dir)
    vector_path = Path(vector_db_dir)

    try:
        store = LocalVectorDB.load(vector_path)
        hits = store.search(query, top_k=top_k)
        if not hits:
            return "未检索到相关资料。"

        context = "\n\n".join(f"[相似度 {item['score']}]\n{item['content']}" for item in hits)
        return f"基于知识库检索结果：\n\n{context}"
    except (FileNotFoundError, ValueError):
        fallback_text = _fallback_rule_text(doc_path)
        if not doc_path.exists() or not any(doc_path.rglob("*")):
            return fallback_text

        try:
            docs = load_documents_from_dir(doc_path)
            if docs:
                store = LocalVectorDB(vector_path)
                store.build_from_documents(docs, chunk_size=500, overlap=100)
                hits = store.search(query, top_k=top_k)
                if hits:
                    context = "\n\n".join(f"[相似度 {item['score']}]\n{item['content']}" for item in hits)
                    return f"基于知识库检索结果：\n\n{context}"
        except Exception:
            pass

        return fallback_text
