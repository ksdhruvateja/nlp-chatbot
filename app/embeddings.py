"""Embedding providers. `hash` works fully offline; openai/gemini use their APIs.

All providers return L2-normalized vectors of EMBEDDING_DIM so they can be
stored in the same pgvector column. Changing provider requires re-ingesting.
"""
from __future__ import annotations

import hashlib
import re

import httpx
import numpy as np

from .config import get_settings
from .ai.adapters import _post
from .ai.base import AdapterError
from urllib.parse import quote

_TOKEN = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _normalize(vec: np.ndarray) -> list[float]:
    norm = float(np.linalg.norm(vec))
    return (vec / norm).tolist() if norm else vec.tolist()


def hash_embed(text: str, dim: int) -> list[float]:
    """Signed feature hashing over word unigrams, bigrams and char trigrams."""
    vec = np.zeros(dim, dtype=np.float32)
    tokens = tokenize(text)
    feats: list[tuple[str, float]] = [(t, 1.0) for t in tokens]
    feats += [(f"{a}_{b}", 0.7) for a, b in zip(tokens, tokens[1:])]
    for t in tokens:
        padded = f"#{t}#"
        feats += [(f"c:{padded[i:i + 3]}", 0.25) for i in range(len(padded) - 2)]
    for feat, weight in feats:
        h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
        vec[h % dim] += weight if (h >> 63) & 1 else -weight
    return _normalize(vec)


def _openai_embed(texts: list[str], dim: int) -> list[list[float]]:
    s = get_settings()
    r = httpx.post("https://api.openai.com/v1/embeddings",
                   headers={"Authorization": f"Bearer {s.openai_api_key}"},
                   json={"model": "text-embedding-3-small", "input": texts, "dimensions": dim},
                   timeout=60)
    r.raise_for_status()
    return [_normalize(np.array(d["embedding"])) for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def _gemini_embed(texts: list[str], dim: int, task_type: str) -> list[list[float]]:
    s = get_settings()
    if not s.gemini_api_key:
        raise AdapterError("Gemini key is not configured", retryable=False)
    model = s.gemini_embedding_model.removeprefix("models/")
    data = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='')}:batchEmbedContents",
        "gemini", 60, headers={"x-goog-api-key": s.gemini_api_key},
        json={"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]},
                            "taskType": task_type, "outputDimensionality": dim} for t in texts]})
    try:
        vectors = [np.asarray(e["values"], dtype=np.float32) for e in data["embeddings"]]
        if len(vectors) != len(texts) or any(v.shape != (dim,) or not np.isfinite(v).all()
                                              or not np.linalg.norm(v) for v in vectors):
            raise ValueError("invalid embedding shape or values")
        return [_normalize(v) for v in vectors]
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterError("gemini returned invalid embeddings", retryable=True) from exc


def embed_many(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    s = get_settings()
    if not texts:
        return []
    if s.embedding_provider == "openai":
        return [v for i in range(0, len(texts), 96) for v in _openai_embed(texts[i:i + 96], s.embedding_dim)]
    if s.embedding_provider == "gemini":
        return [v for i in range(0, len(texts), 96)
                for v in _gemini_embed(texts[i:i + 96], s.embedding_dim, task_type)]
    if s.embedding_provider == "hash":
        return [hash_embed(t, s.embedding_dim) for t in texts]
    raise ValueError(f"Unknown embedding provider: {s.embedding_provider}")


def embed(text: str) -> list[float]:
    return embed_many([text], task_type="RETRIEVAL_QUERY")[0]
