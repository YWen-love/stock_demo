from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Dict, Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


def _tokenize_chinese(text: str) -> List[str]:
    cleaned = re.sub(r"\s+", "", text)
    return [char for char in cleaned if char.strip()]


class LocalVectorDB:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.base_dir / "metadata.json"
        self.vectors_path = self.base_dir / "vectors.npy"
        self.chunks: List[str] = []
        self.vectorizer: TfidfVectorizer | None = None
        self.vectors: np.ndarray | None = None
        self.is_loaded = False

    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 100) -> List[str]:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return []

        chunks: List[str] = []
        start = 0
        while start < len(cleaned):
            end = min(len(cleaned), start + chunk_size)
            chunk = cleaned[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end == len(cleaned):
                break
            start = max(0, end - overlap)
        return chunks

    def build_from_documents(self, docs: List[str], chunk_size: int = 500, overlap: int = 100) -> List[str]:
        all_chunks: List[str] = []
        for doc in docs:
            if not doc or not doc.strip():
                continue
            all_chunks.extend(self._chunk_text(doc, chunk_size=chunk_size, overlap=overlap))

        if not all_chunks:
            self.chunks = []
            self.vectors = np.empty((0, 0), dtype=float)
            self.vectorizer = TfidfVectorizer()
            self.is_loaded = True
            self._save_metadata()
            return []

        self.vectorizer = TfidfVectorizer(
            tokenizer=_tokenize_chinese,
            preprocessor=lambda x: x,
            token_pattern=None,
            lowercase=False,
            ngram_range=(1, 2),
        )
        self.vectors = self.vectorizer.fit_transform(all_chunks).toarray()
        self.chunks = all_chunks
        self.is_loaded = True
        self._save_metadata()
        return all_chunks

    def _save_metadata(self) -> None:
        payload = {"chunks": self.chunks}
        with open(self.metadata_path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
        if self.vectors is not None:
            np.save(self.vectors_path, self.vectors)

    @classmethod
    def load(cls, base_dir: str | Path) -> "LocalVectorDB":
        instance = cls(base_dir)
        metadata_path = instance.metadata_path
        vectors_path = instance.vectors_path

        if not metadata_path.exists() or not vectors_path.exists():
            raise FileNotFoundError(f"数据库未初始化：{base_dir}")

        with open(metadata_path, "r", encoding="utf-8") as fp:
            metadata = json.load(fp)
        instance.chunks = metadata.get("chunks", [])
        instance.vectors = np.load(vectors_path)
        instance.vectorizer = TfidfVectorizer(
            tokenizer=_tokenize_chinese,
            preprocessor=lambda x: x,
            token_pattern=None,
            lowercase=False,
            ngram_range=(1, 2),
        )
        instance.vectorizer.fit(instance.chunks)
        instance.is_loaded = True
        return instance

    def search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        if not self.is_loaded or self.vectorizer is None or self.vectors is None:
            raise ValueError("向量库尚未加载，请先 build_from_documents 或 load()")
        if not query.strip():
            return []

        query_vec = self.vectorizer.transform([query]).toarray()
        similarities = cosine_similarity(query_vec, self.vectors)[0]
        ranked_idx = np.argsort(similarities)[::-1][:top_k]

        results: List[Dict[str, Any]] = []
        for idx in ranked_idx:
            score = float(similarities[idx])
            if score <= 0:
                continue
            results.append({"content": self.chunks[int(idx)], "score": round(score, 4)})
        return results
