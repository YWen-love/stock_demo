from __future__ import annotations

from pathlib import Path

from rag_service import build_rag


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    doc_dir = root / "rag_docs"
    db_dir = root / "vector_db"

    store = build_rag(doc_dir, db_dir)
    print(f"RAG 构建完成，文档块数：{len(store.chunks)}")
    print(f"数据库目录：{db_dir}")
