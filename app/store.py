"""Firestore access: the existing portfolio data and the RAG chunk collection."""

from dataclasses import dataclass, field
from functools import lru_cache

from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector
from google.oauth2 import service_account

from app.config import get_settings


@dataclass
class Chunk:
    id: str
    text: str
    source: str  # human-readable label, e.g. "Project: Clush"
    url: str = ""
    metadata: dict = field(default_factory=dict)


@lru_cache
def get_db() -> firestore.Client:
    s = get_settings()
    info = s.service_account_info
    if info:
        creds = service_account.Credentials.from_service_account_info(info)
        return firestore.Client(project=s.firebase_project_id or info.get("project_id"), credentials=creds)
    # Falls back to Application Default Credentials (e.g. `gcloud auth application-default login`).
    return firestore.Client(project=s.firebase_project_id or None)


def search(query_vector: list[float], k: int) -> list[Chunk]:
    s = get_settings()
    q = get_db().collection(s.chunks_collection).find_nearest(
        vector_field="embedding",
        query_vector=Vector(query_vector),
        distance_measure=DistanceMeasure.COSINE,
        limit=k,
    )
    results = []
    for doc in q.get():
        d = doc.to_dict()
        results.append(
            Chunk(id=doc.id, text=d["text"], source=d.get("source", ""), url=d.get("url", ""))
        )
    return results


def replace_chunks(chunks: list[Chunk], vectors: list[list[float]]) -> None:
    """Delete every existing chunk, then write the new set. Content is small, so a full rebuild is simplest."""
    s = get_settings()
    db = get_db()
    col = db.collection(s.chunks_collection)

    for doc in col.list_documents():
        doc.delete()

    batch = db.batch()
    for i, (c, v) in enumerate(zip(chunks, vectors, strict=True)):
        batch.set(
            col.document(c.id),
            {"text": c.text, "source": c.source, "url": c.url, "metadata": c.metadata, "embedding": Vector(v)},
        )
        if (i + 1) % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
