"""Pick a few follow-up questions for the visitor from the chunks retrieved for their question."""

import re

from app.store import Chunk

_STOP = {
    "a", "an", "the", "is", "are", "does", "do", "did", "he", "his", "him", "salil", "salil's", "me", "about",
    "tell", "what", "whats", "what's", "how", "can", "i", "to", "of", "in", "at", "or", "and", "has", "have",
    "give", "with", "for", "on", "you", "your", "it", "be", "would", "where", "quick", "now",
}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9.+#-]+", text.lower()) if w not in _STOP}


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return a.strip().lower() == b.strip().lower()
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def pick_suggestions(chunks: list[Chunk], asked: list[str], n: int = 3) -> list[str]:
    """Follow-ups from retrieved chunks, skipping the best match (it was just answered),
    anything close to a question already asked, and near-duplicates of each other."""
    picked: list[str] = []
    for c in chunks[1:]:
        q = c.question.strip()
        if not q or any(_similar(q, a) for a in asked) or any(_similar(q, p) for p in picked):
            continue
        picked.append(q)
        if len(picked) == n:
            break
    return picked
