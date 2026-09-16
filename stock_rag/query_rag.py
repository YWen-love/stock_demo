from __future__ import annotations

from pathlib import Path

from rag_service import answer_query


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法：python query_rag.py \"你的问题\"")
        raise SystemExit(1)

    base_dir = Path(__file__).resolve().parent
    question = sys.argv[1]
    result = answer_query(
        query=question,
        doc_dir=base_dir / "rag_docs",
        vector_db_dir=base_dir / "vector_db",
        top_k=3,
    )
    print(result)
