from __future__ import annotations

import os

_model = None


def embed_text(text: str) -> list[float] | None:
    global _model
    backend = os.getenv("EMBEDDING_BACKEND", "none").strip().lower()
    if backend in {"", "none", "disabled"}:
        return None
    if backend != "sentence_transformers":
        raise RuntimeError(f"unsupported EMBEDDING_BACKEND={backend}")
    if not text.strip():
        raise ValueError("cannot embed empty text")

    from sentence_transformers import SentenceTransformer

    if _model is None:
        _model = SentenceTransformer(os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"))
    vector = _model.encode(text, normalize_embeddings=True).astype(float).tolist()
    dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
    if len(vector) != dimensions:
        raise RuntimeError(f"embedding dimensions mismatch: expected={dimensions} actual={len(vector)}")
    return vector
