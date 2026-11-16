"""Gemini embeddings via the google-genai SDK."""

import math
from functools import lru_cache

from google import genai
from google.genai import types

from app.config import get_settings

BATCH_SIZE = 100


@lru_cache
def _client() -> genai.Client:
    return genai.Client(api_key=get_settings().google_api_key)


def _normalize(v: list[float]) -> list[float]:
    # Gemini only pre-normalizes full-size (3072-dim) vectors; truncated ones need it.
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _embed(texts: list[str], task_type: str) -> list[list[float]]:
    s = get_settings()
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        resp = _client().models.embed_content(
            model=s.embedding_model,
            contents=texts[i : i + BATCH_SIZE],
            config=types.EmbedContentConfig(
                task_type=task_type, output_dimensionality=s.embedding_dim
            ),
        )
        out.extend(_normalize(list(e.values)) for e in resp.embeddings)
    return out


def embed_documents(texts: list[str]) -> list[list[float]]:
    return _embed(texts, "RETRIEVAL_DOCUMENT")


def embed_query(text: str) -> list[float]:
    return _embed([text], "RETRIEVAL_QUERY")[0]
